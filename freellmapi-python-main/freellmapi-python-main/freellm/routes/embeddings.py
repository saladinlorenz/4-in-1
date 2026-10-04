from __future__ import annotations
import time
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_db
from .middleware import extract_api_token, validate_unified_key

router = APIRouter()

def _error(s, m, t="invalid_request_error"):
    return JSONResponse(status_code=s, content={"error":{"message": m, "type": t}})

@router.post("/v1/embeddings")
async def create_embeddings(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _error(401, "Invalid API key", "authentication_error")
    try:
        body=await request.json()
    except Exception:
        return _error(400, "Invalid JSON")
    model=body.get("model") or "text-embedding-3-small"
    inp=body.get("input")
    if inp is None:
        return _error(400, "input is required")
    inputs=[inp] if isinstance(inp, str) else inp if isinstance(inp, list) else [str(inp)]
    # find embedding model
    row=conn.execute("SELECT platform, model_id, display_name FROM embedding_models WHERE family=? OR model_id=? LIMIT 1", (model, model)).fetchone()
    if not row:
        # try any enabled
        row=conn.execute("SELECT platform, model_id, display_name FROM embedding_models WHERE enabled=1 LIMIT 1").fetchone()
    if not row:
        # fallback to openai compatible provider directly
        # try to find api key for openai platform or any
        from ..providers.registry import get_provider
        # attempt via first openai key
        krow=conn.execute("SELECT platform, encrypted_key, iv, auth_tag FROM api_keys WHERE enabled=1 LIMIT 1").fetchone()
        if not krow:
            return _error(429, "No embedding provider configured")
        plat, enc, iv, tag=krow
        from ..crypto import decrypt
        try:
            key=decrypt(enc, iv, tag)
        except Exception:
            return _error(500, "decrypt failed")
        prov=get_provider(plat)
        if not prov:
            return _error(404, f"provider {plat} not found")
        # generic openai embeddings call
        try:
            base=getattr(prov, "base_url", "")
            if not base:
                return _error(500, "provider has no base URL for embeddings")
            headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
            async with httpx.AsyncClient(timeout=60) as client:
                resp=await client.post(f"{base}/embeddings", headers=headers, json={"model": model, "input": inp})
                if not resp.is_success:
                    return _error(resp.status_code, resp.text[:500])
                data=resp.json()
                # log request
                conn.execute("INSERT INTO requests(platform, model_id, status, input_tokens, output_tokens, latency_ms, request_type) VALUES(?,?, 'success', ?,0,0,'embedding')", (plat, model, len(str(inp))//4))
                conn.commit()
                return JSONResponse(content=data)
        except Exception as e:
            return _error(502, str(e)[:500])
    plat, mid, disp=row
    # need key for that platform
    krow=conn.execute("SELECT encrypted_key, iv, auth_tag FROM api_keys WHERE platform=? AND enabled=1 LIMIT 1", (plat,)).fetchone()
    if not krow:
        return _error(429, f"No key for embedding provider {plat}")
    from ..crypto import decrypt
    from ..providers.registry import get_provider
    try:
        key=decrypt(krow[0], krow[1], krow[2])
    except Exception:
        return _error(500, "decrypt failed")
    prov=get_provider(plat)
    base=getattr(prov, "base_url", None)
    if not base:
        return _error(500, "no base URL")
    headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
    # many providers support openai embeddings at base/embeddings
    async with httpx.AsyncClient(timeout=60) as client:
        resp=await client.post(f"{base}/embeddings", headers=headers, json={"model": mid, "input": inp})
        if not resp.is_success:
            return _error(resp.status_code, resp.text[:500], "server_error" if resp.status_code>=500 else "invalid_request_error")
        data=resp.json()
        # normalize to openai shape if needed
        if "data" not in data and "embeddings" in data:
            # cohere style?
            data={"object":"list","data":[{"object":"embedding","index":i,"embedding": e} for i,e in enumerate(data["embeddings"])],"model": mid, "usage":{"prompt_tokens":0,"total_tokens":0}}
        # inject provider field
        data.setdefault("model", mid)
        conn.execute("INSERT INTO requests(platform, model_id, status, input_tokens, output_tokens, latency_ms, request_type) VALUES(?,?, 'success', ?,0,0,'embedding')", (plat, mid, sum(len(s)//4 for s in inputs)))
        conn.commit()
        return JSONResponse(content=data)
