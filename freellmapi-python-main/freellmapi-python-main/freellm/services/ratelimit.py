from __future__ import annotations
import time
import sqlite3
from typing import Optional

DAY_MS=24*60*60*1000

_lease_counts: dict[tuple, int] = {}  # (platform, model_id, key_id) -> in-flight
_lease_expiry: dict[tuple, float] = {}

def _now_ms()->int:
    return int(time.time()*1000)

def _prune_leases():
    now=time.time()
    for k,exp in list(_lease_expiry.items()):
        if exp < now:
            _lease_counts.pop(k,None)
            _lease_expiry.pop(k,None)

def acquire_lease(platform: str, model_id: str, key_id: int):
    k=(platform,model_id,key_id)
    _lease_counts[k]=_lease_counts.get(k,0)+1
    _lease_expiry[k]=time.time()+120  # 2 min backstop

def release_lease(platform: str, model_id: str, key_id: int):
    k=(platform,model_id,key_id)
    c=_lease_counts.get(k,0)
    if c<=1:
        _lease_counts.pop(k,None)
        _lease_expiry.pop(k,None)
    else:
        _lease_counts[k]=c-1

def _count_requests(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, window_ms: int) -> int:
    since=_now_ms()-window_ms
    row=conn.execute("SELECT COUNT(*) FROM rate_limit_usage WHERE platform=? AND model_id=? AND key_id=? AND kind='request' AND created_at_ms>?", (platform,model_id,key_id,since)).fetchone()
    return row[0] if row else 0

def _sum_tokens(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, window_ms: int) -> int:
    since=_now_ms()-window_ms
    row=conn.execute("SELECT COALESCE(SUM(tokens),0) FROM rate_limit_usage WHERE platform=? AND model_id=? AND key_id=? AND kind='tokens' AND created_at_ms>?", (platform,model_id,key_id,since)).fetchone()
    return int(row[0]) if row else 0

def can_make_request(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, limits: dict) -> bool:
    _prune_leases()
    rpm=limits.get("rpm")
    rpd=limits.get("rpd")
    k=(platform,model_id,key_id)
    infl=_lease_counts.get(k,0)
    if rpm is not None:
        if _count_requests(conn,platform,model_id,key_id,60_000)+infl >= rpm:
            return False
    if rpd is not None:
        if _count_requests(conn,platform,model_id,key_id,DAY_MS)+infl >= rpd:
            return False
    return True

def can_use_tokens(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, limits: dict, estimated: int) -> bool:
    _prune_leases()
    tpm=limits.get("tpm")
    tpd=limits.get("tpd")
    if tpm is not None:
        if _sum_tokens(conn,platform,model_id,key_id,60_000)+estimated > tpm:
            return False
    if tpd is not None:
        if _sum_tokens(conn,platform,model_id,key_id,DAY_MS)+estimated > tpd:
            return False
    return True

def record_request(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, tokens: int = 0):
    now=_now_ms()
    conn.execute("INSERT INTO rate_limit_usage(platform, model_id, key_id, kind, tokens, created_at_ms) VALUES(?,?,?,?,?,?)",(platform,model_id,key_id,"request",0,now))
    if tokens>0:
        conn.execute("INSERT INTO rate_limit_usage(platform, model_id, key_id, kind, tokens, created_at_ms) VALUES(?,?,?,?,?,?)",(platform,model_id,key_id,"tokens",tokens,now))
    conn.commit()
    # prune old >24h
    cutoff=now-DAY_MS
    conn.execute("DELETE FROM rate_limit_usage WHERE created_at_ms<?", (cutoff,))
    conn.commit()

# cooldowns
def is_on_cooldown(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int) -> bool:
    row=conn.execute("SELECT expires_at_ms FROM rate_limit_cooldowns WHERE platform=? AND model_id=? AND key_id=?", (platform,model_id,key_id)).fetchone()
    if not row:
        return False
    exp=row[0]
    if exp < _now_ms():
        conn.execute("DELETE FROM rate_limit_cooldowns WHERE platform=? AND model_id=? AND key_id=?", (platform,model_id,key_id))
        conn.commit()
        return False
    return True

def set_cooldown(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int, duration_ms: int = 60000, source: str = "heuristic"):
    now=_now_ms()
    exp=now+duration_ms
    conn.execute("INSERT INTO rate_limit_cooldowns(platform, model_id, key_id, expires_at_ms, source, set_at_ms) VALUES(?,?,?,?,?,?) ON CONFLICT(platform,model_id,key_id) DO UPDATE SET expires_at_ms=excluded.expires_at_ms, source=excluded.source, set_at_ms=excluded.set_at_ms",(platform,model_id,key_id,exp,source,now))
    conn.commit()

def clear_cooldown(conn: sqlite3.Connection, platform: str, model_id: str, key_id: int):
    conn.execute("DELETE FROM rate_limit_cooldowns WHERE platform=? AND model_id=? AND key_id=?", (platform,model_id,key_id))
    conn.commit()

def get_soonest_expiry(conn: sqlite3.Connection) -> int | None:
    row=conn.execute("SELECT MIN(expires_at_ms) FROM rate_limit_cooldowns WHERE expires_at_ms>?", (_now_ms(),)).fetchone()
    return row[0] if row and row[0] else None

def get_cooldown_decision(status: int | None, message: str, model_limits: dict, key_id: int, retry_after_ms: int | None) -> tuple[int,str]:
    # simplified ladder from spec
    low=message.lower()
    is_rate = status==429 or "rate" in low or "quota" in low or "too many" in low
    if not is_rate and status not in (402,403):
        return 60000,"heuristic"
    if retry_after_ms is not None:
        return min(retry_after_ms, DAY_MS),"authoritative"
    if status==402:
        return DAY_MS,"credit"
    if status==403 and "tier" in low:
        return DAY_MS,"tier"
    # unknown limits escalation -> 2min
    return 120000,"heuristic"

def parse_provider_limit(message: str):
    import re
    # require number + axis keyword; simplified
    low=message.lower()
    m=re.search(r"limit[:\s]+([\d,]+)", low)
    if not m:
        return None,None
    val=int(m.group(1).replace(",",""))
    if "tpd" in low:
        return "tpd",val
    if "tpm" in low:
        return "tpm",val
    if "rpd" in low:
        return "rpd",val
    if "rpm" in low:
        return "rpm",val
    return None,None

def learn_limit_from_error(conn: sqlite3.Connection, model_db_id: int, message: str):
    axis,val=parse_provider_limit(message)
    if not axis or val is None:
        return
    col={"rpm":"rpm_limit","rpd":"rpd_limit","tpm":"tpm_limit","tpd":"tpd_limit"}[axis]
    cur=conn.execute(f"SELECT {col} FROM models WHERE id=?", (model_db_id,)).fetchone()
    if not cur:
        return
    cur_val=cur[0]
    if cur_val is None or cur_val>val:
        conn.execute(f"UPDATE models SET {col}=? WHERE id=?", (val, model_db_id))
        conn.commit()
