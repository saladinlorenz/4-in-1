from __future__ import annotations
import hashlib
import json
import time

_store: dict[str, tuple[float, dict]] = {}
MAX_ENTRIES=5000
TTL_S=3600

def _key(messages, model: str, options: dict | None) -> str:
    payload=json.dumps({"messages": messages, "model": model, "opts": options or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()

def get_cache(messages, model: str, options: dict | None):
    k=_key(messages, model, options)
    rec=_store.get(k)
    if not rec:
        return None
    exp, data=rec
    if exp < time.time():
        _store.pop(k,None)
        return None
    return data

def set_cache(messages, model: str, options: dict | None, data: dict):
    if len(_store)>=MAX_ENTRIES:
        # evict oldest
        oldest=min(_store.items(), key=lambda kv: kv[1][0])
        _store.pop(oldest[0],None)
    k=_key(messages, model, options)
    _store[k]=(time.time()+TTL_S, data)

def get_stats():
    return {"entries": len(_store), "maxEntries": MAX_ENTRIES, "ttlSeconds": TTL_S}

def clear():
    _store.clear()
