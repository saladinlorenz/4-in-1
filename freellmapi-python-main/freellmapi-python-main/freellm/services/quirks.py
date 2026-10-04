from __future__ import annotations
def get_quirks_for_model(platform: str, model_id: str, conn) -> list[dict]:
    rows=conn.execute("SELECT q.slug, q.title, q.body, q.severity FROM quirks q JOIN quirk_targets t ON t.quirk_id=q.slug WHERE (t.platform IS NULL OR t.platform=?) AND (t.model_glob IS NULL OR ? GLOB t.model_glob)", (platform, model_id)).fetchall()
    return [{"slug":r[0],"title":r[1],"body":r[2],"severity":r[3]} for r in rows]
