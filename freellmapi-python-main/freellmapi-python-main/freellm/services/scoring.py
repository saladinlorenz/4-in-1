from __future__ import annotations
import math
import random
import time

PRESETS={
    "balanced": {"reliability":0.5,"speed":0.25,"intelligence":0.25},
    "smartest": {"reliability":0.35,"speed":0.1,"intelligence":0.55},
    "fastest": {"reliability":0.35,"speed":0.55,"intelligence":0.1},
    "reliable": {"reliability":0.7,"speed":0.15,"intelligence":0.15},
}

def _beta_sample(alpha: float, beta: float)->float:
    # approximate Beta via gamma sampling using random.gammavariate if available
    try:
        ga=random.gammavariate(alpha,1)
        gb=random.gammavariate(beta,1)
        return ga/(ga+gb) if (ga+gb)>0 else 0.5
    except Exception:
        return alpha/(alpha+beta) if (alpha+beta)>0 else 0.5

def reliability_score(succ: int, fail: int, sampled: bool = True)->float:
    alpha=succ+1
    beta=fail+1
    if sampled:
        return _beta_sample(alpha,beta)
    return alpha/(alpha+beta)

def speed_score(tok_per_s: float | None, ttfb_ms: float | None)->float:
    if tok_per_s is None and ttfb_ms is None:
        return 0.6
    thr=0.0
    if tok_per_s is not None:
        thr=1 - math.exp(-(tok_per_s/60))
    ttfb_s=0.0
    if ttfb_ms is not None:
        ttfb_s=max(0,min(1,(5000-ttfb_ms)/(5000-300)))
    if tok_per_s is not None and ttfb_ms is not None:
        return 0.6*thr+0.4*ttfb_s
    if tok_per_s is not None:
        return thr
    return ttfb_s

def intelligence_composite(size_label: str, rank: int)->float:
    tier={"Frontier":4,"Large":3,"Medium":2,"Small":1}.get(size_label,0)
    return tier*1000 - math.sqrt(max(1,rank))*31

def intelligence_scores(composites: list[float])->list[float]:
    if not composites:
        return []
    mn=min(composites)
    mx=max(composites)
    if mx<=mn:
        return [1.0]*len(composites)
    return [(c-mn)/(mx-mn) for c in composites]

def combine(weights: dict, rel: float, spd: float, intel: float)->float:
    wsum=sum(weights.values())
    if wsum==0:
        return 0
    w={k:v/wsum for k,v in weights.items()}
    return w["reliability"]*rel + w["speed"]*spd + w["intelligence"]*intel

def get_weights(strategy: str)->dict:
    return PRESETS.get(strategy, PRESETS["balanced"]).copy()

def headroom_factor(remaining_ratio: float)->float:
    # linear ramp 0.2->0.1
    if remaining_ratio>=0.2:
        return 1.0
    if remaining_ratio<=0:
        return 0.1
    return 0.1 + 0.9*(remaining_ratio/0.2)

def rate_limit_factor(penalty: int)->float:
    MAX_PENALTY=10
    return 1 - (min(penalty,MAX_PENALTY)/MAX_PENALTY)*0.6
