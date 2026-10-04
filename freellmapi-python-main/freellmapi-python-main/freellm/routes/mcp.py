from __future__ import annotations
import json
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_db
from .middleware import extract_api_token, validate_unified_key
from ..services.model_listing import build_model_listing

router = APIRouter()

PROTOCOL_VERSION="2025-06-18"

TOOLS=[
    {"name":"list_models","description":"List usable free models","inputSchema":{"type":"object","properties":{"available_only":{"type":"boolean","default": True}},"required":[]}},
    {"name":"provider_health","description":"Per-platform key health and cooldowns","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"usage_summary","description":"Usage summary for a time range","inputSchema":{"type":"object","properties":{"range":{"type":"string","enum":["24h","7d","30d"]}}}},
    {"name":"routing_info","description":"Top routing scores","inputSchema":{"type":"object","properties":{}}},
    {"name":"get_routing_strategy","description":"Get current routing strategy","inputSchema":{"type":"object","properties":{}}},
    {"name":"set_routing_strategy","description":"Set routing strategy","inputSchema":{"type":"object","properties":{"strategy":{"type":"string","enum":["balanced","smartest","fastest","reliable","priority","custom"]}}}},
    {"name":"get_cache_stats","description":"Get response cache stats","inputSchema":{"type":"object","properties":{}}},
    {"name":"get_compression_stats","description":"Get compression stats","inputSchema":{"type":"object","properties":{}}},
]

def _rpc_error(id_, code, message):
    return {"jsonrpc":"2.0","id": id_, "error":{"code": code, "message": message}}

def _rpc_result(id_, result):
    return {"jsonrpc":"2.0","id": id_, "result": result}

@router.post("/mcp")
async def mcp_post(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        body=await request.json() if request.headers.get("content-type","").startswith("application/json") else {}
        bid=body.get("id") if isinstance(body, dict) else None
        return JSONResponse(content=_rpc_error(bid, -32001, "Invalid API key. Authenticate with the unified key as a Bearer token."), status_code=401)
    try:
        body=await request.json()
    except Exception:
        return JSONResponse(content=_rpc_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(body, list):
        return JSONResponse(content=_rpc_error(None, -32600, "Batch not supported"), status_code=400)
    if body.get("id") is None:
        # notification
        return JSONResponse(content="", status_code=202)
    m=body.get("method")
    mid=body.get("id")
    params=body.get("params") or {}
    if m=="initialize":
        return JSONResponse(content=_rpc_result(mid, {"protocolVersion": PROTOCOL_VERSION,"capabilities":{"tools":{}},"serverInfo":{"name":"freellmapi","version":"1.0.0"}}))
    if m=="ping":
        return JSONResponse(content=_rpc_result(mid, {}))
    if m=="tools/list":
        return JSONResponse(content=_rpc_result(mid, {"tools": TOOLS}))
    if m=="tools/call":
        name=params.get("name")
        args=params.get("arguments") or {}
        try:
            result=await _call_tool(name, args, conn)
            return JSONResponse(content=_rpc_result(mid, {"content":[{"type":"text","text": json.dumps(result, ensure_ascii=False, indent=2)}]}))
        except Exception as e:
            return JSONResponse(content=_rpc_result(mid, {"content":[{"type":"text","text": f"error: {e}"}],"isError": True}))
    return JSONResponse(content=_rpc_error(mid, -32601, f"Method not found: {m}"), status_code=404)

@router.get("/mcp")
async def mcp_get(request: Request):
    return JSONResponse(content=_rpc_error(None, -32000, "GET not allowed on /mcp (POST with JSON-RPC)"), status_code=405)

async def _call_tool(name: str, args: dict, conn):
    if name=="list_models":
        avail=args.get("available_only", True)
        objs,_=build_model_listing(conn, available_only=bool(avail))
        return {"models": [{"id": o["id"],"platform": o["platform"],"display_name": o.get("display_name"),"available": o["available"]} for o in objs[:50]]}
    if name=="provider_health":
        keys=conn.execute("SELECT platform, status, COUNT(*) FROM api_keys GROUP BY platform, status").fetchall()
        cds=conn.execute("SELECT platform, model_id, key_id, expires_at_ms FROM rate_limit_cooldowns").fetchall()
        return {"keys": [{"platform": r[0],"status": r[1],"count": r[2]} for r in keys], "cooldowns": [{"platform": r[0],"model": r[1],"key_id": r[2],"expires_at": r[3]} for r in cds]}
    if name=="usage_summary":
        rng=args.get("range","7d")
        days={"24h":1,"7d":7,"30d":30}.get(rng,7)
        since=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time()-days*86400))
        rows=conn.execute("SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM requests WHERE created_at>=?", (since,)).fetchone()
        total, succ, inp, outp=rows
        top=conn.execute("SELECT platform, model_id, COUNT(*) as cnt FROM requests WHERE created_at>=? GROUP BY platform, model_id ORDER BY cnt DESC LIMIT 5", (since,)).fetchall()
        rate= round((succ/(total or 1))*1000)/10 if total else 0
        return {"range": rng, "total_requests": total, "success_rate": rate, "input_tokens": inp, "output_tokens": outp, "top_models": [{"platform": r[0],"model": r[1],"count": r[2]} for r in top]}
    if name=="routing_info":
        rows=conn.execute("SELECT platform, model_id, intelligence_rank, speed_rank FROM models WHERE enabled=1 ORDER BY intelligence_rank ASC LIMIT 10").fetchall()
        return {"top_models": [{"platform": r[0],"model": r[1],"intel": r[2],"speed": r[3]} for r in rows]}
    if name=="get_routing_strategy":
        row=conn.execute("SELECT value FROM settings WHERE key='routing_strategy'").fetchone()
        return {"strategy": row[0] if row else "balanced"}
    if name=="set_routing_strategy":
        strat=args.get("strategy")
        if strat not in ("balanced","smartest","fastest","reliable","priority","custom"):
            raise ValueError("invalid strategy")
        conn.execute("INSERT INTO settings(key,value) VALUES('routing_strategy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (strat,))
        conn.commit()
        return {"strategy": strat}
    if name=="get_cache_stats":
        from ..services.cache import get_stats
        return get_stats()
    if name=="get_compression_stats":
        return {"enabled": False, "engines": []}
    raise ValueError(f"unknown tool {name}")

@router.api_route("/mcp", methods=["DELETE"])
async def mcp_delete(request: Request):
    return JSONResponse(content=_rpc_error(None, -32000, "DELETE not allowed"), status_code=405)
