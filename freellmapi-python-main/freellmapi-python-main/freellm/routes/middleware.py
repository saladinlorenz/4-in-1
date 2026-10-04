from __future__ import annotations
import hashlib
import hmac
import time
from fastapi import Request
from fastapi.responses import JSONResponse

# simple per-IP fixed window limiter
_windows: dict[str, list[float]] = {}
_admin_windows: dict[str, list[float]] = {}

def _is_rate_limited(ip: str, rpm: int, store: dict) -> bool:
    if rpm==0:
        return False
    now=time.time()
    window=store.get(ip, [])
    window=[t for t in window if now - t < 60]
    if len(window) >= rpm:
        store[ip]=window
        return True
    window.append(now)
    store[ip]=window
    # cap tracked IPs
    if len(store)>10000:
        oldest=min(store.items(), key=lambda kv: min(kv[1]) if kv[1] else now)
        store.pop(oldest[0], None)
    return False

def extract_api_token(request: Request) -> str | None:
    auth=request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # anthropic/gemini headers
    for h in ("x-api-key","x-goog-api-key","x-dashboard-token"):
        v=request.headers.get(h) or request.headers.get(h.lower())
        if v:
            return v.strip()
    # query key for gemini
    q=request.query_params.get("key")
    if q:
        return q.strip()
    return None

def timing_safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

def get_unified_key(conn) -> str | None:
    try:
        row=conn.execute("SELECT value FROM settings WHERE key='unified_api_key'").fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None

def validate_unified_key(token: str | None, conn) -> bool:
    if not token:
        return False
    uk=get_unified_key(conn)
    if uk and token==uk:
        return True
    # client-profile keys sk-cp- : check sha256 hash
    if token and token.startswith("sk-cp-"):
        digest=hashlib.sha256(token.encode()).hexdigest()
        row=conn.execute("SELECT id FROM client_profiles WHERE key_hash=?", (digest,)).fetchone()
        if row:
            return True
    # url tokens
    if token:
        h=hashlib.sha256(token.encode()).hexdigest()
        row=conn.execute("SELECT id FROM api_url_tokens WHERE token_hash=?", (h,)).fetchone()
        if row:
            return True
    return False

def validate_dashboard_session(request: Request, conn) -> dict | None:
    token=extract_api_token(request)
    if not token:
        return None
    import hashlib
    h=hashlib.sha256(token.encode()).hexdigest()
    row=conn.execute("SELECT user_id, expires_at_ms FROM sessions WHERE token_hash=?", (h,)).fetchone()
    if not row:
        return None
    uid, exp=row
    if exp < int(time.time()*1000):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
        conn.commit()
        return None
    urow=conn.execute("SELECT id, email FROM users WHERE id=?", (uid,)).fetchone()
    if not urow:
        return None
    return {"user_id": urow[0], "email": urow[1]}

def require_dashboard_auth(request: Request, conn):
    user=validate_dashboard_session(request, conn)
    if not user:
        return JSONResponse(status_code=401, content={"error":{"message":"Authentication required","type":"authentication_error"}})
    return user
