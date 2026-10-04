from __future__ import annotations
import asyncio
import time
import sqlite3

async def check_all_keys(conn: sqlite3.Connection):
    from ..providers.registry import get_provider
    rows=conn.execute("SELECT id, platform, encrypted_key, iv, auth_tag FROM api_keys WHERE enabled=1").fetchall()
    for r in rows:
        kid, plat, enc, iv, tag = r
        try:
            from ..crypto import decrypt
            key=decrypt(enc, iv, tag)
        except Exception:
            conn.execute("UPDATE api_keys SET status='error', last_error='decrypt failed' WHERE id=?", (kid,))
            continue
        prov=get_provider(plat)
        if not prov:
            continue
        try:
            res=await prov.validate_key(key)
            if res is True:
                conn.execute("UPDATE api_keys SET status='healthy', last_checked_at=datetime('now'), last_error=NULL WHERE id=?", (kid,))
            elif isinstance(res, dict) and not res.get("valid", True):
                conn.execute("UPDATE api_keys SET status='error', last_error=?, last_checked_at=datetime('now') WHERE id=?", (res.get("error","invalid")[:500], kid))
            else:
                conn.execute("UPDATE api_keys SET status='healthy', last_checked_at=datetime('now') WHERE id=?", (kid,))
        except Exception as e:
            # don't mark error on network failure
            pass
    conn.commit()
