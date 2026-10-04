from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_db
from ..crypto import encrypt, decrypt, mask_key, generate_unified_key
from .middleware import validate_dashboard_session

router = APIRouter()

def _err(s,m):
    return JSONResponse(status_code=s, content={"error": m})

def _require_auth(request: Request, conn):
    user=validate_dashboard_session(request, conn)
    if not user:
        return None, JSONResponse(status_code=401, content={"error":{"message":"Authentication required","type":"authentication_error"}})
    return user, None

# ---- AUTH ----
def _hash_pwd(pwd: str) -> str:
    import hashlib, os
    salt=os.urandom(16)
    dk=hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 100000)
    return f"pbkdf2:{salt.hex()}:{dk.hex()}"

def _verify_pwd(pwd: str, h: str) -> bool:
    try:
        _, salt_hex, dk_hex = h.split(":")
        salt=bytes.fromhex(salt_hex)
        dk=hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 100000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

@router.get("/api/auth/status")
async def auth_status(request: Request):
    conn=get_db()
    has_users=conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]>0
    user=validate_dashboard_session(request, conn)
    return JSONResponse(content={"hasUsers": has_users, "authenticated": bool(user), "user": {"email": user["email"]} if user else None})

@router.post("/api/auth/setup")
async def auth_setup(request: Request):
    conn=get_db()
    body=await request.json() if request.headers.get("content-type","").startswith("application/json") else {}
    email=(body.get("email") or "").strip().lower()
    pwd=body.get("password") or ""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]>0:
        return _err(400, "Setup already completed")
    if not email or "@" not in email or len(pwd)<6:
        return _err(400, "email and password (min 6) required")
    h=_hash_pwd(pwd)
    conn.execute("INSERT INTO users(email, password_hash) VALUES(?,?)", (email, h))
    conn.commit()
    uid=conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    tok=secrets.token_hex(32)
    th=hashlib.sha256(tok.encode()).hexdigest()
    conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at_ms) VALUES(?,?,?)", (th, uid, int(time.time()*1000)+30*24*3600*1000))
    conn.commit()
    # ensure unified key exists
    if not conn.execute("SELECT value FROM settings WHERE key='unified_api_key'").fetchone() or not conn.execute("SELECT value FROM settings WHERE key='unified_api_key'").fetchone()[0]:
        uk=generate_unified_key()
        conn.execute("INSERT INTO settings(key,value) VALUES('unified_api_key',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (uk,))
        conn.commit()
    return JSONResponse(content={"token": tok, "email": email})

@router.post("/api/auth/login")
async def auth_login(request: Request):
    conn=get_db()
    body=await request.json()
    email=(body.get("email") or "").strip().lower()
    pwd=body.get("password") or ""
    row=conn.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row or not _verify_pwd(pwd, row[1]):
        return _err(401, "Invalid email or password")
    tok=secrets.token_hex(32)
    th=hashlib.sha256(tok.encode()).hexdigest()
    conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at_ms) VALUES(?,?,?)", (th, row[0], int(time.time()*1000)+30*24*3600*1000))
    conn.commit()
    return JSONResponse(content={"token": tok, "email": email})

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    conn=get_db()
    tok=(request.headers.get("authorization") or "").replace("Bearer ","").strip() or request.headers.get("x-dashboard-token","")
    if tok:
        th=hashlib.sha256(tok.encode()).hexdigest()
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        conn.commit()
    return JSONResponse(content={"ok": True})

# ---- KEYS ----
@router.get("/api/keys")
async def list_keys(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("SELECT id, platform, label, status, enabled, base_url, last_checked_at, last_error FROM api_keys ORDER BY platform").fetchall()
    data=[]
    for r in rows:
        data.append({"id": r[0],"platform": r[1],"label": r[2],"status": r[3],"enabled": bool(r[4]),"base_url": r[5],"last_checked_at": r[6],"last_error": r[7],"maskedKey": "****"})
    uk=conn.execute("SELECT value FROM settings WHERE key='unified_api_key'").fetchone()
    return JSONResponse(content={"keys": data, "unifiedKey": uk[0] if uk else ""})

@router.post("/api/keys")
async def add_key(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    platform=body.get("platform")
    key=body.get("key") or ""
    label=body.get("label") or ""
    base_url=body.get("base_url")
    if not platform:
        return _err(400, "platform required")
    # keyless platforms store sentinel
    if not key and platform in ("kilo","ovh","aihorde"):
        key="no-key"
    if not key:
        return _err(400, "key required")
    enc=encrypt(key)
    conn.execute("INSERT INTO api_keys(platform, label, encrypted_key, iv, auth_tag, base_url) VALUES(?,?,?,?,?,?)",
                 (platform, label, enc["encrypted"], enc["iv"], enc["authTag"], base_url))
    conn.commit()
    return JSONResponse(content={"ok": True, "id": conn.execute("SELECT last_insert_rowid()").fetchone()[0]})

@router.patch("/api/keys/{key_id}")
async def update_key(key_id: int, request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    sets=[]
    params=[]
    if "enabled" in body:
        sets.append("enabled=?"); params.append(1 if body["enabled"] else 0)
    if "label" in body:
        sets.append("label=?"); params.append(body["label"])
    if "base_url" in body:
        sets.append("base_url=?"); params.append(body["base_url"])
    if not sets:
        return _err(400, "no fields to update")
    params.append(key_id)
    conn.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return JSONResponse(content={"ok": True})

@router.delete("/api/keys/{key_id}")
async def delete_key(key_id: int, request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    conn.commit()
    return JSONResponse(content={"ok": True})

@router.get("/api/keys/export")
async def export_keys(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("SELECT platform, encrypted_key, iv, auth_tag, label, base_url FROM api_keys").fetchall()
    exported=[]
    for r in rows:
        try:
            k=decrypt(r[1], r[2], r[3])
        except Exception:
            k="***decrypt-failed***"
        exported.append({"platform": r[0],"key": k,"label": r[4],"base_url": r[5]})
    fmt=request.query_params.get("format","json")
    if fmt=="env":
        lines=[]
        counts={}
        for e in exported:
            plat=e["platform"].upper()
            counts[plat]=counts.get(plat,0)+1
            name=f"{plat}_KEY" + (f"_{counts[plat]}" if counts[plat]>1 else "")
            lines.append(f"{name}={e['key']}")
            if e["base_url"]:
                lines.append(f"CUSTOM_{counts[plat]}_BASE_URL={e['base_url']}")
        return JSONResponse(content={"content": "\n".join(lines)})
    return JSONResponse(content={"keys": exported})

import hashlib as _hl

@router.post("/api/keys/test")
async def test_key(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    platform=body.get("platform")
    key=body.get("key") or ""
    if not platform or not key:
        return _err(400, "platform and key required")
    from ..providers.registry import get_provider
    prov=get_provider(platform)
    if not prov:
        return _err(400, "unknown provider")
    try:
        res=await prov.validate_key(key)
        if res is True:
            return JSONResponse(content={"valid": True})
        if isinstance(res, dict) and res.get("valid") is False:
            return JSONResponse(content={"valid": False, "error": res.get("error","invalid")})
        if isinstance(res, dict) and res.get("valid") is True:
            return JSONResponse(content={"valid": True})
        return JSONResponse(content={"valid": bool(res), "error": str(res) if not res else ""})
    except Exception as e:
        return JSONResponse(content={"valid": False, "error": str(e)[:300]})

@router.get("/api/models")
async def admin_models(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("""
        SELECT m.id, m.platform, m.model_id, m.display_name, m.intelligence_rank, m.speed_rank, m.size_label, m.rpm_limit, m.rpd_limit, m.tpm_limit, m.tpd_limit, m.monthly_token_budget, m.context_window, m.enabled, m.supports_vision, m.supports_tools, fc.priority, fc.enabled as fc_enabled
        FROM models m LEFT JOIN fallback_config fc ON fc.model_db_id=m.id
        ORDER BY COALESCE(fc.priority, 9999) ASC, m.intelligence_rank ASC
    """).fetchall()
    data=[{"id": r[0],"platform": r[1],"model_id": r[2],"display_name": r[3],"intelligence_rank": r[4],"speed_rank": r[5],"size_label": r[6],"rpm_limit": r[7],"rpd_limit": r[8],"tpm_limit": r[9],"tpd_limit": r[10],"monthly_token_budget": r[11],"context_window": r[12],"enabled": bool(r[13]),"supports_vision": bool(r[14]),"supports_tools": bool(r[15]),"priority": r[16],"fallback_enabled": bool(r[17]) if r[17] is not None else bool(r[13])} for r in rows]
    return JSONResponse(content={"models": data})

@router.patch("/api/models/{model_id}")
async def update_model(model_id: int, request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    # map fields
    col_map={"displayName":"display_name","intelligenceRank":"intelligence_rank","speedRank":"speed_rank","sizeLabel":"size_label","rpmLimit":"rpm_limit","rpdLimit":"rpd_limit","tpmLimit":"tpm_limit","tpdLimit":"tpd_limit","monthlyTokenBudget":"monthly_token_budget","contextWindow":"context_window","enabled":"enabled","supportsVision":"supports_vision","supportsTools":"supports_tools","display_name":"display_name","intelligence_rank":"intelligence_rank","speed_rank":"speed_rank","size_label":"size_label","rpm_limit":"rpm_limit","rpd_limit":"rpd_limit","tpm_limit":"tpm_limit","tpd_limit":"tpd_limit","monthly_token_budget":"monthly_token_budget","context_window":"context_window","supports_vision":"supports_vision","supports_tools":"supports_tools"}
    sets=[]; params=[]
    for k,v in body.items():
        if k in ("fallbackEnabled","priority"):
            continue
        col=col_map.get(k)
        if col:
            sets.append(f"{col}=?"); params.append(int(v) if col in ("intelligence_rank","speed_rank","rpm_limit","rpd_limit","tpm_limit","tpd_limit","context_window","enabled","supports_vision","supports_tools") and v is not None else v)
    if sets:
        params.append(model_id)
        conn.execute(f"UPDATE models SET {', '.join(sets)} WHERE id=?", params)
    if "fallbackEnabled" in body or "priority" in body:
        if "priority" in body:
            conn.execute("UPDATE fallback_config SET priority=? WHERE model_db_id=?", (body["priority"], model_id))
        if "fallbackEnabled" in body:
            conn.execute("UPDATE fallback_config SET enabled=? WHERE model_db_id=?", (1 if body["fallbackEnabled"] else 0, model_id))
    conn.commit()
    return JSONResponse(content={"ok": True})

# ---- ANALYTICS ----
@router.get("/api/analytics/summary")
async def analytics_summary(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rng=request.query_params.get("range","7d")
    days={"24h":1,"7d":7,"30d":30,"90d":90}.get(rng,7)
    import time as _t
    since=_t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(_t.time()-days*86400))
    total=conn.execute("SELECT COUNT(*) FROM requests WHERE created_at>=?", (since,)).fetchone()[0] or 0
    succ=conn.execute("SELECT COUNT(*) FROM requests WHERE created_at>=? AND status='success'", (since,)).fetchone()[0] or 0
    inp=conn.execute("SELECT COALESCE(SUM(input_tokens),0) FROM requests WHERE created_at>=? AND status='success'", (since,)).fetchone()[0] or 0
    outp=conn.execute("SELECT COALESCE(SUM(output_tokens),0) FROM requests WHERE created_at>=? AND status='success'", (since,)).fetchone()[0] or 0
    lat=conn.execute("SELECT AVG(latency_ms) FROM requests WHERE created_at>=? AND latency_ms>0", (since,)).fetchone()[0]
    rate= round((succ/max(1,total))*1000)/10 if total else 0
    # savings estimate
    est_cost=0
    for r in conn.execute("SELECT input_tokens, output_tokens, platform FROM requests WHERE created_at>=? AND status='success'", (since,)).fetchall():
        # fallback pricing $1 per 1M input, $2 per 1M output
        est_cost+= (r[0]*1 + r[1]*2)/1_000_000
    return JSONResponse(content={"range": rng, "totalRequests": total, "successRate": rate, "inputTokens": inp, "outputTokens": outp, "avgLatencyMs": round(lat) if lat else 0, "estimatedSavings": round(est_cost,4)})

@router.get("/api/analytics/by-model")
async def analytics_by_model(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("SELECT platform, model_id, COUNT(*) as cnt, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as succ, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM requests GROUP BY platform, model_id ORDER BY cnt DESC LIMIT 20").fetchall()
    return JSONResponse(content={"data": [{"platform": r[0],"model": r[1],"requests": r[2],"success": r[3],"input_tokens": r[4],"output_tokens": r[5]} for r in rows]})

@router.get("/api/settings")
async def get_settings(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("SELECT key, value FROM settings").fetchall()
    return JSONResponse(content={k:v for k,v in rows})

@router.put("/api/settings")
async def put_settings(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    for k,v in body.items():
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    conn.commit()
    return JSONResponse(content={"ok": True})

@router.get("/api/health")
async def health_status(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    # trigger health check async if requested
    if request.query_params.get("check")=="1":
        from ..services.health import check_all_keys
        import asyncio
        asyncio.create_task(check_all_keys(conn))
        return JSONResponse(content={"checking": True})
    rows=conn.execute("SELECT platform, status, COUNT(*) FROM api_keys GROUP BY platform, status").fetchall()
    cds=conn.execute("SELECT platform, model_id, key_id, expires_at_ms FROM rate_limit_cooldowns").fetchall()
    return JSONResponse(content={"keysByStatus": [{"platform": r[0],"status": r[1],"count": r[2]} for r in rows], "cooldowns": [{"platform": r[0],"model": r[1],"key_id": r[2],"expires_at": r[3]} for r in cds]})

@router.get("/api/free-tier")
async def free_tier(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    # calculate monthly budget usage
    import time as _t
    month_start=_t.strftime("%Y-%m-01 00:00:00", _t.gmtime())
    used=conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM requests WHERE created_at>=? AND status='success'", (month_start,)).fetchone()[0] or 0
    # total budget sum of enabled models (simplified)
    total_budget=0
    for r in conn.execute("SELECT monthly_token_budget FROM models WHERE enabled=1").fetchall():
        from ..lib.budget import parse_budget
        b=parse_budget(r[0])
        if b:
            total_budget+=b
    return JSONResponse(content={"used": used, "budget": total_budget, "models": conn.execute("SELECT COUNT(*) FROM models WHERE enabled=1").fetchone()[0]})

@router.get("/api/fallback")
async def get_fallback(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    rows=conn.execute("SELECT m.id, m.platform, m.model_id, m.display_name, fc.priority, fc.enabled FROM models m JOIN fallback_config fc ON fc.model_db_id=m.id ORDER BY fc.priority").fetchall()
    return JSONResponse(content={"chain": [{"modelDbId": r[0],"platform": r[1],"model_id": r[2],"display_name": r[3],"priority": r[4],"enabled": bool(r[5])} for r in rows]})

@router.put("/api/fallback")
async def put_fallback(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err:
        return err
    body=await request.json()
    chain=body.get("chain") or body
    if isinstance(chain, list):
        conn.execute("DELETE FROM fallback_config")
        for item in chain:
            mid=item.get("modelDbId") or item.get("model_db_id") or item.get("id")
            prio=item.get("priority") or 0
            en=1 if item.get("enabled", True) else 0
            if mid:
                conn.execute("INSERT INTO fallback_config(model_db_id, priority, enabled) VALUES(?,?,?)", (mid, prio, en))
        conn.commit()
    return JSONResponse(content={"ok": True})

@router.get("/api/status")
async def status(request: Request):
    conn=get_db()
    return JSONResponse(content={"ok": True, "models": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0], "keys": conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]})

# ---- stubs for remaining admin surfaces (avoid 404 for dashboard) ----
@router.get("/api/analytics/by-platform")
async def analytics_by_platform(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT platform, COUNT(*) as cnt, COALESCE(SUM(output_tokens),0) FROM requests GROUP BY platform ORDER BY cnt DESC").fetchall()
    return JSONResponse(content={"data": [{"platform":r[0],"requests":r[1],"output_tokens":r[2]} for r in rows]})

@router.get("/api/analytics/by-client")
async def analytics_by_client(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT COALESCE(client_agent,'unknown'), COUNT(*) FROM requests GROUP BY client_agent ORDER BY COUNT(*) DESC LIMIT 20").fetchall()
    return JSONResponse(content={"data": [{"agent":r[0],"count":r[1]} for r in rows]})

@router.get("/api/analytics/timeline")
async def analytics_timeline(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    # return empty timeline buckets
    return JSONResponse(content={"data": []})

@router.get("/api/analytics/error-distribution")
async def analytics_errors_dist(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT error, COUNT(*) FROM requests WHERE error IS NOT NULL GROUP BY error LIMIT 20").fetchall()
    return JSONResponse(content={"data": [{"error":r[0],"count":r[1]} for r in rows]})

@router.get("/api/requests")
async def list_requests(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    limit=min(int(request.query_params.get("limit","100")), 500)
    rows=conn.execute("SELECT id, platform, model_id, status, input_tokens, output_tokens, latency_ms, created_at, error FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return JSONResponse(content={"data": [{"id":r[0],"platform":r[1],"model":r[2],"status":r[3],"input_tokens":r[4],"output_tokens":r[5],"latency":r[6],"created_at":r[7],"error":r[8]} for r in rows]})

@router.get("/api/logs")
async def get_logs(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT ts, level, scope, message FROM server_logs ORDER BY ts DESC LIMIT 100").fetchall()
    return JSONResponse(content={"logs": [{"ts":r[0],"level":r[1],"scope":r[2],"message":r[3]} for r in rows]})

@router.get("/api/profiles")
async def list_profiles(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT id, name, emoji, color, type, is_favorite, sort_order FROM profiles ORDER BY sort_order").fetchall()
    return JSONResponse(content={"profiles": [{"id":r[0],"name":r[1],"emoji":r[2],"color":r[3],"type":r[4],"is_favorite":bool(r[5]),"sort_order":r[6]} for r in rows]})

@router.post("/api/profiles")
async def create_profile(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    body=await request.json()
    name=body.get("name") or "New Profile"
    conn.execute("INSERT INTO profiles(name, emoji, color) VALUES(?,?,?)", (name, body.get("emoji",""), body.get("color","#6366f1")))
    conn.commit()
    return JSONResponse(content={"ok": True, "id": conn.execute("SELECT last_insert_rowid()").fetchone()[0]})

@router.get("/api/conversations")
async def list_conversations(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT id, title, model, updated_at_ms FROM playground_conversations ORDER BY updated_at_ms DESC LIMIT 50").fetchall()
    return JSONResponse(content={"conversations": [{"id":r[0],"title":r[1],"model":r[2],"updated_at":r[3]} for r in rows]})

@router.post("/api/conversations")
async def create_conversation(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    body=await request.json()
    import uuid, time as _t
    cid=str(uuid.uuid4())
    now=int(_t.time()*1000)
    conn.execute("INSERT INTO playground_conversations(id, title, model, messages_json, created_at_ms, updated_at_ms) VALUES(?,?,?,?,?,?)",
                 (cid, body.get("title",""), body.get("model"), json.dumps(body.get("messages") or []), now, now))
    conn.commit()
    return JSONResponse(content={"id": cid})

@router.get("/api/backups")
async def list_backups(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT id, filename, size_bytes, created_at_ms FROM backups ORDER BY created_at_ms DESC").fetchall()
    return JSONResponse(content={"backups": [{"id":r[0],"filename":r[1],"size":r[2],"created_at":r[3]} for r in rows]})

@router.get("/api/cache")
async def cache_info(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    from ..services.cache import get_stats
    return JSONResponse(content=get_stats())

@router.get("/api/premium/status")
async def premium_status(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    return JSONResponse(content={"hasKey": False, "license": None, "catalog": {"lastSync": None}})

@router.get("/api/update/status")
async def update_status(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    return JSONResponse(content={"status":"unknown","current": "1.0.0"})

@router.get("/api/client-profiles")
async def client_profiles(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    rows=conn.execute("SELECT id, name, key_prefix FROM client_profiles").fetchall()
    return JSONResponse(content={"profiles": [{"id":r[0],"name":r[1],"prefix":r[2]} for r in rows]})

@router.get("/api/providers")
async def list_providers(request: Request):
    from ..providers.registry import list_providers_meta
    return JSONResponse(content={"providers": list_providers_meta()})

@router.get("/api/compression")
async def compression_info(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    from ..services.compression import get_config, ENGINES
    cfg = get_config(conn)
    return JSONResponse(content={"mode": cfg["mode"], "engines": cfg["engines"], "definitions": ENGINES})

@router.put("/api/compression")
async def compression_update(request: Request):
    conn=get_db()
    _, err=_require_auth(request, conn)
    if err: return err
    body=await request.json()
    mode = body.get("mode", "off")
    engines = body.get("engines")
    if isinstance(engines, list):
        engines = {e: (e in engines) for e in [x["id"] for x in __import__("freellm.services.compression", fromlist=["ENGINES"]).ENGINES]}
    from ..services.compression import set_config
    cfg = set_config(conn, mode, engines if isinstance(engines, dict) else None)
    return JSONResponse(content={"ok": True, "config": cfg})
