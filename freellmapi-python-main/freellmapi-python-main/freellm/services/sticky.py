from __future__ import annotations
import hashlib
import time

_store: dict[str, dict] = {}
TTL_MS=30*60*1000

def _key(messages, session_header: str | None, strategy_key: str | None = None) -> str:
    if session_header:
        k=f"hdr:{session_header}"
    else:
        # sha1 of first user message
        first=""
        for m in messages:
            if m.get("role")=="user":
                c=m.get("content")
                if isinstance(c,str):
                    first=c
                elif isinstance(c,list):
                    for p in c:
                        if isinstance(p,dict) and p.get("type")=="text":
                            first+=p.get("text","")
                break
        k=hashlib.sha1(first.encode()).hexdigest()[:16]
    if strategy_key:
        k+=f"::{strategy_key}"
    return k

def get_sticky_model(messages, session_header: str | None, strategy_key: str | None = None):
    k=_key(messages, session_header, strategy_key)
    rec=_store.get(k)
    if not rec:
        return None
    if rec["exp"] < time.time()*1000:
        _store.pop(k,None)
        return None
    # require assistant turn already in history
    has_assistant=any(m.get("role")=="assistant" for m in messages)
    if not has_assistant:
        return None
    return rec.get("model_db_id")

def set_sticky_model(messages, model_db_id: int, session_header: str | None, strategy_key: str | None = None):
    k=_key(messages, session_header, strategy_key)
    _store[k]={"model_db_id": model_db_id, "exp": time.time()*1000+TTL_MS}
    # sweep >500, hard evict >1000
    if len(_store)>1000:
        # evict oldest
        oldest=min(_store.items(), key=lambda kv: kv[1]["exp"])
        _store.pop(oldest[0],None)
    elif len(_store)>500:
        now=time.time()*1000
        for kk,v in list(_store.items()):
            if v["exp"]<now:
                _store.pop(kk,None)
