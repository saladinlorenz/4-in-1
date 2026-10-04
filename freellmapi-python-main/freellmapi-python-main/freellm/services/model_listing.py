from __future__ import annotations
import sqlite3

def build_model_listing(conn: sqlite3.Connection, available_only: bool = False):
    # simplified: list models with availability = enabled and has key
    if available_only:
        rows=conn.execute("""
            SELECT m.id, m.platform, m.model_id, m.display_name, m.context_window, m.intelligence_rank, m.supports_tools, m.enabled,
                   EXISTS(SELECT 1 FROM api_keys k WHERE k.platform=m.platform AND k.enabled=1 AND (m.key_id IS NULL OR k.id=m.key_id)) as available
            FROM models m WHERE m.enabled=1
        """).fetchall()
        rows=[r for r in rows if r["available"]]
    else:
        rows=conn.execute("""
            SELECT m.id, m.platform, m.model_id, m.display_name, m.context_window, m.intelligence_rank, m.supports_tools, m.enabled,
                   EXISTS(SELECT 1 FROM api_keys k WHERE k.platform=m.platform AND k.enabled=1 AND (m.key_id IS NULL OR k.id=m.key_id)) as available
            FROM models m
        """).fetchall()
    # group by model_id canonical — simplified: one row per platform/model_id
    objs=[]
    for r in rows:
        objs.append({
            "id": r["model_id"],
            "object": "model",
            "created": 0,
            "owned_by": "freellmapi",
            "display_name": r["display_name"],
            "platform": r["platform"],
            "context_window": r["context_window"],
            "supports_tools": bool(r["supports_tools"]),
            "available": bool(r["available"]),
            "enabled": bool(r["enabled"]),
        })
    objs.sort(key=lambda x: (0 if x["available"] else 1, 0 if x["enabled"] else 1, x["display_name"]))
    auto_ctx=None
    for o in objs:
        if o["available"] and o["context_window"]:
            auto_ctx=max(auto_ctx or 0, o["context_window"])
    return objs, auto_ctx or 128000
