from __future__ import annotations
import math
import random
import sqlite3
import time

from ..lib.tokens import estimate_tokens
from ..lib.budget import parse_budget
from .scoring import get_weights, reliability_score, speed_score, intelligence_composite, intelligence_scores, combine, headroom_factor, rate_limit_factor
from .ratelimit import can_make_request, can_use_tokens, is_on_cooldown, acquire_lease, release_lease
from .sticky import get_sticky_model

CACHE_TTL_MS=60000
WINDOW_MS=7*24*60*60*1000
HALF_LIFE_DAYS=2
EXPLORE_CHANCE=0.1
EXPLORE_MIN_SAMPLES=5
OUTPUT_RESERVE_CAP=2000
CONTEXT_SAFETY=1.25

# in-memory stats cache
_stats_cache: dict = {}
_stats_ts: int = 0

def _load_stats(conn: sqlite3.Connection):
    global _stats_cache, _stats_ts
    now=int(time.time()*1000)
    if now - _stats_ts < CACHE_TTL_MS and _stats_cache:
        return _stats_cache
    # one grouped query over requests last 7 days
    since_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time()-7*86400))
    rows=conn.execute("""
        SELECT platform, model_id, key_id, status, output_tokens, latency_ms, ttfb_ms, created_at
        FROM requests WHERE created_at >= ?
    """, (since_time,)).fetchall()
    buckets: dict[str, dict] = {}
    for r in rows:
        plat,mid,kid,status,out,lat,ttfb,created=r
        # age in days
        try:
            # created_at is ISO; parse days ago
            import datetime
            dt=datetime.datetime.fromisoformat(created.replace("Z","+00:00")) if "T" in created else datetime.datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            age_days=(time.time()-dt.timestamp())/86400
        except Exception:
            age_days=0
        weight=0.5**(age_days/HALF_LIFE_DAYS)
        key=f"{plat}:{mid}"
        kkey=f"{key}:{kid}" if kid else key
        for kk in (key, kkey):
            b=buckets.setdefault(kk, {"succ_w":0,"fail_w":0,"timeout_w":0,"out_w":0,"lat_w":0,"ttfb_w":0,"ttfb_sum":0,"count":0})
            low=(status or "").lower()
            is_timeout= any(m in low for m in ("timeout","stalled","etimedout","aborted"))
            is_canceled= low=="canceled"
            if is_canceled:
                continue
            if status=="success":
                b["succ_w"]+=weight
                b["out_w"]+= (out or 0)*weight
                b["lat_w"]+= (lat or 0)*weight
                if ttfb:
                    b["ttfb_sum"]+= ttfb*weight
                    b["ttfb_w"]+= weight
            elif is_timeout:
                b["timeout_w"]+=weight
                # timeout contributes 0 output but latency capped at 120s
                cap=min(lat or 120000, 120000)
                b["lat_w"]+= cap*weight
                if ttfb:
                    b["ttfb_sum"]+= min(ttfb,120000)*weight
                    b["ttfb_w"]+=weight
            else:
                b["fail_w"]+=weight
            b["count"]+=1
    # monthly usage
    month_start=time.strftime("%Y-%m-01 00:00:00", time.gmtime())
    mu={}
    for r in conn.execute("SELECT platform, model_id, COALESCE(SUM(input_tokens+output_tokens),0) FROM requests WHERE created_at>=? AND status='success' GROUP BY platform, model_id", (month_start,)).fetchall():
        mu[f"{r[0]}:{r[1]}"]=r[2]
    _stats_cache={"buckets": buckets, "monthly": mu}
    _stats_ts=now
    return _stats_cache

def _chain_rows(conn: sqlite3.Connection, profile_id: int | None = None):
    if profile_id:
        rows=conn.execute("""
            SELECT m.*, COALESCE(pm.priority, 9999) as prio, COALESCE(pm.enabled,0) as en
            FROM models m LEFT JOIN profile_models pm ON pm.model_db_id=m.id AND pm.profile_id=?
            ORDER BY prio ASC, m.intelligence_rank ASC
        """, (profile_id,)).fetchall()
        # if profile exists, its chain IS the chain even if empty — caller handles empty
        return [dict(r) for r in rows if r["en"]==1]
    rows=conn.execute("""
        SELECT m.*, fc.priority as prio, fc.enabled as en
        FROM models m JOIN fallback_config fc ON fc.model_db_id=m.id
        WHERE fc.enabled=1
        ORDER BY fc.priority ASC
    """).fetchall()
    return [dict(r) for r in rows]

def _resolve_profile(conn, requested: str | None):
    if not requested or requested=="auto":
        row=conn.execute("SELECT value FROM settings WHERE key='active_profile_id'").fetchone()
        pid=(row[0] if row else None)
        if pid and str(pid).strip():
            try:
                return int(pid)
            except Exception:
                return None
        return None
    if requested.startswith("auto:"):
        suffix=requested[5:].lower()
        alias_map={"smart":"smartest","smartest":"smartest","intelligence":"smartest","fast":"fastest","fastest":"fastest","speed":"fastest","cheap":"balanced","cheapest":"balanced","price":"balanced","budget":"balanced","reliable":"reliable","reliability":"reliable","balanced":"balanced"}
        if suffix in alias_map:
            return None  # strategy alias, not profile
        row=conn.execute("SELECT id FROM profiles WHERE lower(name)=?", (suffix,)).fetchone()
        if row:
            return row[0]
        return None
    return None

def _key_candidates(conn, platform: str, model_id: str, endpoint_scope: str, skip_keys: set[str]):
    rows=conn.execute("SELECT id, label, encrypted_key, iv, auth_tag, status, enabled, base_url, model_scope_json FROM api_keys WHERE platform=? AND enabled=1 AND status IN ('healthy','unknown')", (platform,)).fetchall()
    out=[]
    for r in rows:
        key_id=r[0]
        sk=f"{platform}:{model_id}:{key_id}"
        if sk in skip_keys:
            continue
        # model_scope filter
        msj=r[8]
        if msj:
            import json
            try:
                allowed=json.loads(msj)
                if isinstance(allowed,list) and model_id not in allowed:
                    continue
            except Exception:
                pass
        # endpoint_scope pool membership for custom
        if platform=="custom" and endpoint_scope:
            base=(r[7] or "").strip().rstrip("/")
            if base.rstrip("/") != endpoint_scope.rstrip("/"):
                continue
        out.append(dict(id=r[0], label=r[1], encrypted_key=r[2], iv=r[3], auth_tag=r[4], status=r[5], base_url=r[7]))
    return out

def route_request(conn: sqlite3.Connection, *, estimated_tokens: int, messages=None, skip_keys=None, preferred_model_id: int | None = None, has_image: bool = False, wants_tools: bool = False, skip_models=None, skip_platforms=None, output_reserve: int = 1000, response_format: bool = False, requested_model: str | None = None):
    skip_keys=skip_keys or set()
    skip_models=skip_models or set()
    skip_platforms=skip_platforms or set()
    strategy_row=conn.execute("SELECT value FROM settings WHERE key='routing_strategy'").fetchone()
    strategy=(strategy_row[0] if strategy_row else "balanced")
    profile_id=_resolve_profile(conn, requested_model)
    chain=_chain_rows(conn, profile_id)
    if profile_id is not None and not chain:
        # empty profile chain -> no candidates
        raise RouteError("All models exhausted: profile chain is empty", 400, [])
    diagnostics=[]
    stats=_load_stats(conn)
    buckets=stats["buckets"]
    monthly=stats["monthly"]
    # filter chain
    candidates=[]
    for row in chain:
        if row["id"] in skip_models:
            diagnostics.append(f"{row['platform']}/{row['model_id']} ruled out already-failed")
            continue
        if row["platform"] in skip_platforms:
            diagnostics.append(f"{row['platform']}/{row['model_id']} ruled out platform skipped")
            continue
        if has_image and not row["supports_vision"]:
            diagnostics.append(f"{row['platform']}/{row['model_id']} no vision support")
            continue
        if wants_tools and not row["supports_tools"]:
            diagnostics.append(f"{row['platform']}/{row['model_id']} no tool-calling support")
            continue
        if response_format and row["platform"] in ("cohere","cloudflare"):
            diagnostics.append(f"{row['platform']}/{row['model_id']} drops response_format")
            continue
        # context window check
        cw=row["context_window"]
        if cw:
            need=estimated_tokens*CONTEXT_SAFETY + min(output_reserve, OUTPUT_RESERVE_CAP)
            if cw < need:
                diagnostics.append(f"{row['platform']}/{row['model_id']} context {cw} < estimated {estimated_tokens} x1.25 = {need:.0f}")
                continue
        # tpm check
        tpm=row["tpm_limit"]
        if tpm is not None and estimated_tokens > tpm:
            diagnostics.append(f"{row['platform']}/{row['model_id']} tpm {tpm} < estimated {estimated_tokens}")
            continue
        candidates.append(row)
    # sticky preferred splice to front
    if preferred_model_id:
        pref=[c for c in candidates if c["id"]==preferred_model_id]
        if pref:
            rest=[c for c in candidates if c["id"]!=preferred_model_id]
            candidates=pref+rest
        else:
            row=conn.execute("SELECT * FROM models WHERE id=?", (preferred_model_id,)).fetchone()
            if row:
                candidates=[dict(row)]+candidates
    if not candidates:
        from ..lib.fallback_loop import summarize_exhaustion
        from .ratelimit import get_soonest_expiry
        msg=summarize_exhaustion(diagnostics, get_soonest_expiry(conn))
        raise RouteError(msg, 429, diagnostics)
    # scoring
    weights=get_weights(strategy)
    # intelligence composites
    comps=[intelligence_composite(r.get("size_label") or "", r["intelligence_rank"]) for r in candidates]
    intel_scores=intelligence_scores(comps)
    scored=[]
    for idx, row in enumerate(candidates):
        plat=row["platform"]; mid=row["model_id"]
        bkey=f"{plat}:{mid}"
        b=buckets.get(bkey, {"succ_w":0,"fail_w":0,"timeout_w":0})
        succ=int(b.get("succ_w",0))
        fail=int(b.get("fail_w",0)+b.get("timeout_w",0))
        rel=reliability_score(succ,fail, sampled=True)
        # speed
        tok_per_s=None
        ttfb=None
        if b.get("lat_w") and b.get("out_w"):
            try:
                tok_per_s=(b["out_w"]*1000)/max(1,b["lat_w"])
            except Exception:
                tok_per_s=None
        if b.get("ttfb_w"):
            ttfb=b["ttfb_sum"]/b["ttfb_w"]
        spd=speed_score(tok_per_s, ttfb)
        intel=intel_scores[idx] if idx<len(intel_scores) else 0.5
        base=combine(weights, rel, spd, intel)
        # headroom
        budget=parse_budget(row.get("monthly_token_budget"))
        used=monthly.get(bkey,0)
        hr=1.0
        if budget and budget>0:
            hr=1 - used/budget
            hr=max(0,min(1,hr))
            base*=headroom_factor(hr)
        # rate limit penalty (simplified: count cooldowns as penalty)
        penalty=0
        try:
            penalty=int(conn.execute("SELECT COUNT(*) FROM rate_limit_cooldowns WHERE platform=? AND model_id=?", (plat,mid)).fetchone()[0])
        except Exception:
            pass
        base*=rate_limit_factor(penalty)
        scored.append((base, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    # iterate candidates to find usable key
    for score, row in scored:
        plat=row["platform"]; mid=row["model_id"]; scope=row.get("endpoint_scope") or ""
        limits={"rpm":row["rpm_limit"],"rpd":row["rpd_limit"],"tpm":row["tpm_limit"],"tpd":row["tpd_limit"]}
        keys=_key_candidates(conn, plat, mid, scope, skip_keys)
        if not keys:
            diagnostics.append(f"{plat}/{mid} no usable key configured")
            continue
        # order keys by simple round-robin / success rate
        chosen=None
        for k in keys:
            if is_on_cooldown(conn, plat, mid, k["id"]):
                diagnostics.append(f"{plat}/{mid} key {k['id']} cooldown")
                continue
            if not can_make_request(conn, plat, mid, k["id"], limits):
                diagnostics.append(f"{plat}/{mid} key {k['id']} rpm/rpd limit")
                continue
            if not can_use_tokens(conn, plat, mid, k["id"], limits, estimated_tokens):
                diagnostics.append(f"{plat}/{mid} key {k['id']} tpm/tpd limit")
                continue
            # decrypt check
            try:
                from ..crypto import decrypt
                api_key=decrypt(k["encrypted_key"], k["iv"], k["auth_tag"])
            except Exception:
                diagnostics.append(f"{plat}/{mid} key {k['id']} decrypt-error")
                continue
            # custom provider resolve
            from ..providers.registry import resolve_provider
            provider=resolve_provider(plat, k.get("base_url"))
            if not provider:
                diagnostics.append(f"{plat}/{mid} no provider registered")
                continue
            chosen=(k, api_key, provider)
            break
        if not chosen:
            continue
        k, api_key, provider=chosen
        # acquire lease
        acquire_lease(plat, mid, k["id"])
        return {"provider": provider, "modelId": mid, "modelDbId": row["id"], "apiKey": api_key, "keyId": k["id"], "keyLabel": k["label"], "platform": plat, "displayName": row["display_name"], "endpointScope": scope, "release": lambda p=plat,m=mid,kid=k["id"]: release_lease(p,m,kid)}
    from ..lib.fallback_loop import summarize_exhaustion
    from .ratelimit import get_soonest_expiry
    msg=summarize_exhaustion(diagnostics, get_soonest_expiry(conn))
    raise RouteError(msg, 429, diagnostics)

class RouteError(Exception):
    def __init__(self, message: str, status: int = 429, diagnostics=None):
        super().__init__(message)
        self.status=status
        self.diagnostics=diagnostics or []
