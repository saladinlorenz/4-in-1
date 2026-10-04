from __future__ import annotations

def record_quota_from_response(resp, platform: str, model_id: str):
    # parse rate-limit headers and upsert provider_quota_state
    # simplified: read x-ratelimit-remaining/limit and x-ratelimit-reset
    try:
        import time, sqlite3
        from ..db import get_db
        headers=getattr(resp,"headers",{}) or {}
        # httpx Headers case-insensitive
        def get(h):
            try:
                return headers.get(h) or headers.get(h.lower())
            except Exception:
                return None
        limit=get("x-ratelimit-limit-requests") or get("x-ratelimit-limit")
        remaining=get("x-ratelimit-remaining-requests") or get("x-ratelimit-remaining")
        reset=get("x-ratelimit-reset-requests") or get("x-ratelimit-reset")
        if limit is None and remaining is None:
            return
        conn=get_db()
        try:
            lim=int(str(limit).split(",")[0]) if limit else None
            rem=int(str(remaining).split(",")[0]) if remaining else None
        except Exception:
            return
        # upsert into provider_quota_state with pool ::account
        pool=f"{platform}::account"
        now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute("""
            INSERT INTO provider_quota_state(platform, key_id, quota_pool_key, metric, limit_value, remaining_value, reset_at, source, confidence, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform,key_id,quota_pool_key,metric) DO UPDATE SET limit_value=excluded.limit_value, remaining_value=excluded.remaining_value, reset_at=excluded.reset_at, updated_at=excluded.updated_at
        """, (platform, 0, pool, "requests", lim, rem, reset, "header", 1.0, now))
        conn.commit()
    except Exception:
        pass
