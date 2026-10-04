from __future__ import annotations
import asyncio
import json
import time

FUSION_MAX_K=5
FUSION_JUDGE_PROMPT="You are a fusion judge. Synthesize the following draft answers into one best answer. Drafts:\n{drafts}\n\nReturn the single best combined answer."

async def run_fusion(messages, conn, options=None, route_fn=None) -> dict:
    # panel selection: top K fastest available models
    k=min((options or {}).get("k", 3), FUSION_MAX_K)
    rows=conn.execute("SELECT platform, model_id FROM models WHERE enabled=1 ORDER BY speed_rank ASC LIMIT ?", (k*3,)).fetchall()
    # need distinct platforms
    seen=set()
    panel=[]
    for r in rows:
        key=f"{r[0]}:{r[1]}"
        if key not in seen:
            seen.add(key)
            panel.append({"platform":r[0],"model_id":r[1]})
            if len(panel)>=k:
                break
    if not panel:
        raise RuntimeError("No models available for fusion")
    # fan-out in parallel
    drafts=[]
    async def call_one(p):
        try:
            # need to route to that exact model
            from .router import route_request
            from ..lib.tokens import estimate_tokens
            est=estimate_tokens(messages)
            route=route_request(conn, estimated_tokens=est, requested_model=f"{p['platform']}/{p['model_id']}")
            provider=route["provider"]
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], options)
            text=(res["choices"][0]["message"].get("content") or "")
            drafts.append({"platform": p["platform"],"model": p["model_id"],"content": text, "status":"ok"})
        except Exception as e:
            drafts.append({"platform": p["platform"],"model": p["model_id"],"content":"","status":"failed","error": str(e)[:200]})
    await asyncio.gather(*(call_one(p) for p in panel))
    # judge
    ok_drafts=[d for d in drafts if d["status"]=="ok" and d["content"]]
    if not ok_drafts:
        raise RuntimeError("All fusion drafts failed")
    judge_text="\n\n---\n\n".join(f"[{d['platform']}/{d['model']}] {d['content'][:2000]}" for d in ok_drafts)
    judge_prompt=FUSION_JUDGE_PROMPT.format(drafts=judge_text)
    judge_messages=[{"role":"user","content": judge_prompt}]
    # route judge to fastest reliable
    try:
        from .router import route_request
        from ..lib.tokens import estimate_tokens
        est=estimate_tokens(judge_messages)
        route=route_request(conn, estimated_tokens=est)
        provider=route["provider"]
        res=await provider.chat_completion(route["apiKey"], judge_messages, route["modelId"], {"temperature":0.3})
        final=res["choices"][0]["message"].get("content") or ok_drafts[0]["content"]
        judge_info={"platform": route["platform"],"model": route["modelId"]}
    except Exception:
        final=ok_drafts[0]["content"]
        judge_info=None
    return {"content": final, "drafts": drafts, "judge": judge_info}
