from __future__ import annotations
import asyncio
import json
import time
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..db import get_db
from ..routes.middleware import extract_api_token, validate_unified_key
from ..lib.tokens import estimate_tokens
from ..lib.content import has_image_content
from ..lib.think_tags import extract_think_from_message
from ..lib.tool_rescue import rescue_inline_tool_calls, repair_tool_arguments
from ..lib.header_value import routed_via_value
from ..services.router import route_request, RouteError
from ..services.ratelimit import record_request, set_cooldown, get_cooldown_decision, learn_limit_from_error, release_lease
from ..lib.fallback_loop import new_fallback_state, summarize_exhaustion

router = APIRouter()

def _error(status: int, message: str, type_: str = "invalid_request_error", code: str | None = None):
    err={"message": message, "type": type_}
    if code:
        err["code"]=code
    return JSONResponse(status_code=status, content={"error": err})

def _log_request(conn, platform, model_id, key_id, status, in_tok, out_tok, lat, error=None, ttfb=None, pinned=None):
    try:
        conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms, ttfb_ms, error, requested_model) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (platform, model_id, key_id, status, in_tok or 0, out_tok or 0, lat or 0, ttfb, error[:500] if error else None, pinned))
        conn.commit()
        # update total_requests counter
        cur=conn.execute("SELECT value FROM settings WHERE key='total_requests'").fetchone()
        try:
            n=int(cur[0]) if cur else 0
        except Exception:
            n=0
        conn.execute("INSERT INTO settings(key,value) VALUES('total_requests',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(n+1),))
        conn.commit()
    except Exception:
        pass

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _error(401, "Invalid API key", "authentication_error")
    try:
        body=await request.json()
    except Exception:
        return _error(400, "Invalid JSON")
    messages=body.get("messages") or []
    if not isinstance(messages, list) or len(messages)==0:
        return _error(400, "messages must be a non-empty array")
    model=body.get("model") or "auto"
    stream=bool(body.get("stream"))
    max_tokens=body.get("max_tokens") or body.get("max_completion_tokens")
    temperature=body.get("temperature")
    top_p=body.get("top_p")
    stop=body.get("stop")
    tools=body.get("tools")
    tool_choice=body.get("tool_choice")
    parallel_tool_calls=body.get("parallel_tool_calls")
    response_format=body.get("response_format")
    # fusion virtual model
    if model=="fusion":
        try:
            from ..services.fusion import run_fusion
            res=await run_fusion(messages, conn, body)
            return JSONResponse(content={
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fusion",
                "choices":[{"index":0,"message":{"role":"assistant","content": res["content"]},"finish_reason":"stop"}],
                "usage":{"prompt_tokens": estimate_tokens(messages),"completion_tokens": len(res["content"])//4,"total_tokens": estimate_tokens(messages)+ len(res["content"])//4},
                "_fusion_panel": res["drafts"],
                "_fusion_judge": res["judge"]
            }, headers={"X-Routed-Via": "fusion/judge"})
        except Exception as e:
            return _error(502, f"fusion error: {e}", "server_error")
    # session
    session_id=request.headers.get("x-session-id")
    # estimate
    est=estimate_tokens(messages, tools)
    # response cache check (simplified, in-memory)
    from ..services.cache import get_cache
    cache_hit=None
    if not stream:
        try:
            hit=get_cache(messages, model, body)
            if hit:
                cache_hit=hit
        except Exception:
            pass
    if cache_hit:
        return JSONResponse(content=cache_hit, headers={"X-Routed-Via":"cache","X-FreeLLM-Cache":"HIT"})
    # fallback loop
    state=new_fallback_state()
    attempt_logs=[]
    diagnostics=[]
    max_retries=20
    time_budget=45000
    try:
        # fetch setting fallback_time_budget_ms
        row=conn.execute("SELECT value FROM settings WHERE key='fallback_time_budget_ms'").fetchone()
        if row and row[0]:
            try:
                time_budget=int(row[0])
            except Exception:
                pass
    except Exception:
        pass
    start_wall=time.time()*1000
    last_error=None
    for attempt in range(max_retries+1):
        if time_budget>0 and attempt>=1 and (time.time()*1000 - start_wall) > time_budget:
            break
        # check client gone
        if await request.is_disconnected():
            break
        # route
        try:
            wants_tools=bool(tools)
            has_image=has_image_content(messages)
            # sticky
            preferred=None
            if model.lower().startswith("auto") or model=="auto":
                from ..services.sticky import get_sticky_model
                preferred=get_sticky_model(messages, session_id)
            # resolve model param
            requested_model=model
            # need to handle pinned vs auto
            pinned_for_route=None
            if model.lower() in ("auto","") or model.lower().startswith("auto"):
                pinned_for_route=None
            else:
                # try to resolve to db id for sticky group pin
                from ..services.model_groups import resolve_requested_id
                ids=resolve_requested_id(model, conn)
                if ids:
                    pinned_for_route=ids[0]
                    preferred=pinned_for_route  # force group pin
                else:
                    # unknown model -> 404
                    return _error(404, f"The model '{model}' does not exist or is disabled", "invalid_request_error", "model_not_found")
            # if we have a pinned group, we should restrict chain — simplified: pass skip_models that excludes non-group members after first failure?
            # For now, route normally; router will handle group via preferred splice
            route=route_request(conn, estimated_tokens=est, messages=messages, skip_keys=state.skip_keys, preferred_model_id=preferred or pinned_for_route, has_image=has_image, wants_tools=wants_tools, skip_models=state.skip_models, skip_platforms=state.skip_platforms, output_reserve=max_tokens or 1024, response_format=bool(response_format), requested_model=requested_model)
        except RouteError as re:
            # routing exhausted
            last_error=str(re)
            diagnostics.extend(re.diagnostics)
            break
        except Exception as e:
            last_error=str(e)
            break
        provider=route["provider"]
        api_key=route["apiKey"]
        key_id=route["keyId"]
        platform=route["platform"]
        model_id=route["modelId"]
        model_db_id=route["modelDbId"]
        t0=time.time()*1000
        ttfb=None
        options={
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stop": stop,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": parallel_tool_calls,
            "response_format": response_format,
            "stream_options": body.get("stream_options"),
        }
        # remove None
        options={k:v for k,v in options.items() if v is not None}
        try:
            if stream:
                # streaming path — we need to proxy SSE
                # For simplicity, collect upstream streaming then re-emit as SSE with our framing
                # but respect fallback: if upstream errors before headers, failover; after headers, commit.
                header_sent=False
                # we will stream to client
                async def sse_gen():
                    nonlocal ttfb, header_sent
                    first_byte=True
                    tool_deltas: dict[int, dict] = {}
                    text_chunks=[]
                    reasoning_acc=""
                    usage=None
                    finish_reason=None
                    upstream_model=None
                    try:
                        async for chunk in provider.stream_chat_completion(api_key, messages, model_id, options):
                            if first_byte:
                                ttfb=int(time.time()*1000 - t0)
                                first_byte=False
                            # capture upstream model
                            if chunk.get("model"):
                                upstream_model=chunk["model"]
                                chunk["model"]=model_id
                            # capture usage
                            if chunk.get("usage"):
                                usage=chunk["usage"]
                            for ch in chunk.get("choices") or []:
                                if ch.get("finish_reason"):
                                    finish_reason=ch["finish_reason"]
                                delta=ch.get("delta") or {}
                                if delta.get("tool_calls"):
                                    for tc in delta["tool_calls"]:
                                        idx=tc.get("index",0)
                                        cur=tool_deltas.setdefault(idx, {"id": tc.get("id"),"type": tc.get("type","function"),"function": {"name": tc.get("function",{}).get("name",""),"arguments": ""}})
                                        if tc.get("id"):
                                            cur["id"]=tc["id"]
                                        if tc.get("function",{}).get("name"):
                                            cur["function"]["name"]=tc["function"]["name"]
                                        if tc.get("function",{}).get("arguments"):
                                            cur["function"]["arguments"]+=tc["function"]["arguments"]
                                if delta.get("content"):
                                    text_chunks.append(delta["content"])
                                if delta.get("reasoning_content"):
                                    reasoning_acc+=delta["reasoning_content"]
                            # emit chunk with our model id
                            line=f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            yield line
                        # end: build final usage if missing
                        if usage is None:
                            usage={"prompt_tokens": est, "completion_tokens": max(1, len(''.join(text_chunks))//4), "total_tokens": est+ len(''.join(text_chunks))//4}
                        # repair tool args
                        if tool_deltas:
                            from ..lib.tool_rescue import repair_tool_arguments
                            for idx, tc in tool_deltas.items():
                                tc["function"]["arguments"]=repair_tool_arguments(tc["function"]["arguments"])
                        # log success
                        record_request(conn, platform, model_id, key_id, (usage.get("prompt_tokens") or est))
                        _log_request(conn, platform, model_id, key_id, "success", usage.get("prompt_tokens") or est, usage.get("completion_tokens") or 0, int(time.time()*1000 - t0), None, ttfb, model)
                        # sticky
                        from ..services.sticky import set_sticky_model
                        try:
                            set_sticky_model(messages, model_db_id, session_id)
                        except Exception:
                            pass
                        # emit [DONE]
                        yield "data: [DONE]\n\n"
                    except Exception as e:
                        # if header already sent, emit error frame
                        if header_sent or not first_byte:
                            err_payload=json.dumps({"error":{"message": f"Provider error ({platform}): {str(e)[:200]}","type":"stream_error"}}, ensure_ascii=False)
                            yield f"data: {err_payload}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        raise
                headers={
                    "Content-Type":"text/event-stream",
                    "Cache-Control":"no-cache",
                    "Connection":"keep-alive",
                    "X-Routed-Via": routed_via_value(platform, model_id),
                    "X-Fallback-Attempts": str(attempt+1),
                }
                return StreamingResponse(sse_gen(), headers=headers, media_type="text/event-stream")
            else:
                res=await provider.chat_completion(api_key, messages, model_id, options)
                # upstream model capture
                upstream_model=res.get("model")
                res["model"]=model_id
                # empty completion check
                choices=res.get("choices") or []
                content_empty=True
                has_tools=False
                for ch in choices:
                    msg=ch.get("message") or {}
                    if msg.get("tool_calls"):
                        has_tools=True
                        content_empty=False
                        break
                    c=msg.get("content")
                    if isinstance(c,str) and c.strip():
                        content_empty=False
                    elif isinstance(c,list) and any(isinstance(p,dict) and p.get("text") for p in c):
                        content_empty=False
                fr=(choices[0].get("finish_reason") if choices else None)
                if content_empty and not has_tools:
                    # empty completion without tools — retryable only if not length
                    if fr=="length":
                        # bench skip? for now just record and retry
                        pass
                    else:
                        # try rescue
                        combined="".join(c.get("message",{}).get("content") or "" for c in choices)
                        if tools:
                            from ..lib.tool_rescue import rescue_inline_tool_calls
                            names={t["function"]["name"] for t in tools}
                            r=rescue_inline_tool_calls(combined, names)
                            if r["detected"] and r["calls"]:
                                rescued=[{"id":f"call_rescued_{i+1}","type":"function","function":{"name":c["name"],"arguments": repair_tool_arguments(c["arguments"])}} for i,c in enumerate(r["calls"])]
                                res["choices"][0]["message"]["tool_calls"]=rescued
                                res["choices"][0]["message"]["content"]=r["cleanText"] or None
                                res["choices"][0]["finish_reason"]="tool_calls"
                                has_tools=True
                                content_empty=False
                if content_empty and not has_tools:
                    # empty -> treat as failure and fallback
                    raise RuntimeError(f"Empty completion from {platform}/{model_id} (finish_reason={fr})")
                # response_format healing
                if response_format and not has_tools:
                    content=res["choices"][0]["message"].get("content")
                    if isinstance(content,str) and content:
                        # try to ensure json
                        try:
                            json.loads(content)
                        except Exception:
                            # extract json block
                            import re
                            m=re.search(r"\{.*\}", content, re.DOTALL)
                            if m:
                                try:
                                    json.loads(m.group(0))
                                    res["choices"][0]["message"]["content"]=m.group(0)
                                except Exception:
                                    pass
                # repair tool args
                if has_tools or any((c.get("message") or {}).get("tool_calls") for c in choices):
                    for ch in choices:
                        msg=ch.get("message") or {}
                        for tc in msg.get("tool_calls") or []:
                            tc["function"]["arguments"]=repair_tool_arguments(tc["function"]["arguments"])
                usage=res.get("usage") or {"prompt_tokens": est, "completion_tokens": max(1, len(str(res["choices"][0]["message"].get("content") or ""))//4), "total_tokens": est+1}
                record_request(conn, platform, model_id, key_id, usage.get("prompt_tokens") or est)
                # also record tokens for TPM tracking
                try:
                    # tokens already recorded via record_request tokens param
                    pass
                except Exception:
                    pass
                _log_request(conn, platform, model_id, key_id, "success", usage.get("prompt_tokens") or est, usage.get("completion_tokens") or 0, int(time.time()*1000 - t0), None, None, model)
                from ..services.sticky import set_sticky_model
                try:
                    set_sticky_model(messages, model_db_id, session_id)
                except Exception:
                    pass
                # cache
                try:
                    from ..services.cache import set_cache
                    set_cache(messages, model, body, res)
                except Exception:
                    pass
                # sanitize content array -> string
                for ch in res.get("choices") or []:
                    msg=ch.get("message") or {}
                    if isinstance(msg.get("content"), list):
                        msg["content"]="".join(p.get("text","") if isinstance(p,dict) else str(p) for p in msg["content"])
                res_headers={"X-Routed-Via": routed_via_value(platform, model_id), "X-Fallback-Attempts": str(attempt+1)}
                # remove internal _routed_via
                res.pop("_routed_via", None)
                return JSONResponse(content=res, headers=res_headers)
        except Exception as e:
            last_error=str(e)
            diagnostics.append(f"{platform}/{model_id} {last_error[:120]}")
            status=getattr(e,"status", None)
            msg=str(e)
            # classify
            from ..lib.error_classify import classify_error
            cat=classify_error(status, msg)
            # record cooldown except client abort / empty length skip?
            if cat not in ("bad_request",):
                try:
                    retry_ms=getattr(e,"retry_after_ms", None)
                    dur, src=get_cooldown_decision(status, msg, {"rpm":None}, key_id, retry_ms)
                    set_cooldown(conn, platform, model_id, key_id, dur, src)
                    learn_limit_from_error(conn, model_db_id, msg)
                except Exception:
                    pass
            # skip handling
            if status==404 or cat=="model_not_found":
                state.skip_models.add(model_db_id)
            elif status==401 or cat=="auth_invalid":
                state.skip_keys.add(f"{platform}:{model_id}:{key_id}")
                # mark key error after 1 auth failure? keep healthy until health check
            elif cat in ("rate_limited","daily_quota_exhausted","forbidden","out_of_credits","server_error","timeout"):
                state.skip_keys.add(f"{platform}:{model_id}:{key_id}")
            else:
                state.skip_keys.add(f"{platform}:{model_id}:{key_id}")
            # release lease
            try:
                release_lease(platform, model_id, key_id)
            except Exception:
                pass
            attempt_logs.append({"platform":platform,"model":model_id,"error": msg[:120]})
            # log failure
            _log_request(conn, platform, model_id, key_id, cat, est, 0, int(time.time()*1000 - t0), msg, None, model)
            continue
    # exhausted
    diag_msg=summarize_exhaustion(diagnostics, None)
    # store diagnostics as error
    if last_error and "All models exhausted" not in last_error:
        diag_msg=last_error
    from ..lib.fallback_loop import exhaustion_status
    status=exhaustion_status(diagnostics)
    return _error(status, diag_msg, "server_error" if status>=500 else "rate_limit_error")

@router.post("/v1/completions")
async def completions(request: Request):
    # legacy completions: map prompt -> chat messages
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _error(401, "Invalid API key", "authentication_error")
    try:
        body=await request.json()
    except Exception:
        return _error(400, "Invalid JSON")
    prompt=body.get("prompt") or ""
    if isinstance(prompt, list):
        prompt="\n".join(str(p) for p in prompt)
    suffix=body.get("suffix") or ""
    model=body.get("model") or "auto"
    # build messages
    messages=[{"role":"user","content": prompt + suffix}]
    new_body={**body, "messages": messages, "model": model}
    # re-use chat path by mocking request? simplify: call chat provider directly via loop
    # patch request.json to return new_body
    # Instead, forward to chat_completions by constructing new request-like object
    # Simplify: duplicate chat logic inline with new_body
    # We'll just call the same handling by setting body=mew_body and calling internal helper
    # For brevity, create a new Request with new_body — easiest: call private helper
    # Inline: reuse chat_completions logic by monkey-patching await request.json
    request._json = new_body  # type: ignore
    orig_json = request.json
    async def patched_json():
        return new_body
    request.json = patched_json  # type: ignore
    # Call chat handler but need to avoid double auth — already validated
    # We'll directly invoke similar flow without re-parsing; for simplicity just call provider via router once (no fallback loop)
    # Simplified fallback: call router once
    try:
        est=estimate_tokens(messages)
        route=route_request(conn, estimated_tokens=est, messages=messages, requested_model=model)
        provider=route["provider"]
        res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], {"max_tokens": body.get("max_tokens") or 128, "temperature": body.get("temperature")})
        text=(res["choices"][0]["message"].get("content") or "")
        return JSONResponse(content={
            "id": f"cmpl-{int(time.time())}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": route["modelId"],
            "choices":[{"text": text,"index":0,"logprobs":None,"finish_reason": res["choices"][0].get("finish_reason","stop")}],
            "usage": res.get("usage") or {"prompt_tokens": est, "completion_tokens": len(text)//4, "total_tokens": est+len(text)//4}
        }, headers={"X-Routed-Via": routed_via_value(route["platform"], route["modelId"])})
    except Exception as e:
        return _error(502, str(e)[:500])

@router.get("/v1/models")
async def list_models(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    # allow models listing with unified key OR no auth if no keys configured? require auth
    if token and not validate_unified_key(token, conn):
        # check if anthropic-version header -> handle anthropic negotiation elsewhere; here just check openai key
        # if token looks like anthropic key, allow? simplified: reject
        return _error(401, "Invalid API key", "authentication_error")
    # if no token and no unified key yet, allow (first-run)
    avail_only = request.query_params.get("available") in ("true","1","connected","ready")
    from ..services.model_listing import build_model_listing
    objs, auto_ctx = build_model_listing(conn, available_only=avail_only)
    return JSONResponse(content={"object":"list","data": objs})
