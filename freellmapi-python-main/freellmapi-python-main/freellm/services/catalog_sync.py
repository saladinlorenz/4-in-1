from __future__ import annotations
import time

def get_sync_state(conn):
    row=conn.execute("SELECT value FROM settings WHERE key='catalog_last_sync_ms'").fetchone()
    return {"lastSync": row[0] if row else None}

async def sync_catalog(conn, force: bool = False):
    # stub: in real impl, fetch signed catalog from freellmapi.co and apply
    # For Python port, this is a placeholder — catalog is seeded locally; live sync requires premium key
    return {"ok": True, "applied": 0}
