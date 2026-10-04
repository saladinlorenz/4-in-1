from __future__ import annotations
import re

def normalize_name(name: str)->str:
    # strip trailing "(...)" once then trailing "free"
    s=re.sub(r"\s*\([^)]*\)\s*$", "", name)
    s=re.sub(r"\s+free\s*$", "", s, flags=re.I)
    # lowercase, collapse whitespace/hyphen/underscore, keep "+"
    s=s.lower()
    s=re.sub(r"[\s\-_]+", " ", s).strip()
    return s

def group_key(name: str)->str:
    return normalize_name(name)

def slug_label(label: str)->str:
    s=label.lower()
    s=re.sub(r"[^a-z0-9\.\- ]+", "", s)
    s=re.sub(r"\s+", "-", s.strip())
    return s

def build_groups(models: list[dict]) -> dict:
    # models: list of dict with id, display_name etc.
    groups: dict[str, list]= {}
    for m in models:
        gk=group_key(m.get("display_name") or m.get("model_id",""))
        groups.setdefault(gk, []).append(m)
    return groups

def resolve_requested_id(requested: str, conn) -> list[int]:
    # requested can be "platform/model", bare model_id, or canonical slug
    if "/" in requested and not requested.startswith("auto"):
        # platform/model
        plat,mid=requested.split("/",1)
        rows=conn.execute("SELECT id FROM models WHERE platform=? AND model_id=?", (plat,mid)).fetchall()
        if rows:
            return [r[0] for r in rows]
    # bare model_id
    rows=conn.execute("SELECT id FROM models WHERE model_id=?", (requested,)).fetchall()
    if rows:
        return [r[0] for r in rows]
    # slug match via display_name normalized
    all_models=conn.execute("SELECT id, display_name, model_id FROM models").fetchall()
    req_slug=slug_label(requested)
    out=[]
    for mid, disp, model_id in all_models:
        if slug_label(disp)==req_slug or slug_label(model_id)==req_slug:
            out.append(mid)
    return out
