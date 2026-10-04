from __future__ import annotations
import json
import time
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..db import get_db
from .middleware import extract_api_token, validate_unified_key
from ..lib.tokens import estimate_tokens
from ..services.router import route_request
from ..lib.header_value import routed_via_value

router = APIRouter()

def _err(s,m,t="invalid_request_error"):
    return JSONResponse(status_code=s, content={"error":{"message":m,"type":t}})

@router.post("/v1/responses")
async def create_response(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return JSONResponse(status_code=401, content={"error":{"message":"Invalid API key","type":"authentication_error"}})
    try:
        body=await request.json()
    except Exception:
        return _err(400, "Invalid JSON")
    # computer-use check
    tools=body.get("tools") or []
    for tl in tools:
        t=tl.get("type") or ""
        if "computer" in t:
            return JSONResponse(status_code=422, content={"error":{"code":"no_computer_use_model","message":"Computer use is not yet supported on /v1/responses"}})
    input_items=body.get("input") or body.get("messages") or []
    # normalize to chat messages
    messages=[]
    if isinstance(input_items, str):
        messages=[{"role":"user","content": input_items}]
    elif isinstance(input_items, list):
        for it in input_items:
            if isinstance(it, dict) and "role" in it:
                messages.append({"role": it.get("role","user"), "content": it.get("content") or it.get("text") or ""})
            elif isinstance(it, dict) and "type" in it:
                # response input item types: message, function_call etc.
                if it.get("type")=="message":
                    messages.append({"role": it.get("role","user"), "content": "".join(p.get("text","") for p in it.get("content") or [] if isinstance(p,dict))})
            else:
                messages.append({"role":"user","content": str(it)})
    if not messages:
        messages=[{"role":"user","content":"hello"}]
    model=body.get("model") or "auto"
    stream=bool(body.get("stream"))
    max_tokens=body.get("max_output_tokens") or body.get("max_tokens")
    # route
    est=estimate_tokens(messages)
    try:
        route=route_request(conn, estimated_tokens=est, messages=messages, requested_model=model, output_reserve=max_tokens or 1024)
    except Exception as e:
        return _err(getattr(e,"status",429), str(e))
    provider=route["provider"]
    opts={"max_tokens": max_tokens, "temperature": body.get("temperature"), "tools": body.get("tools"), "tool_choice": body.get("tool_choice")}
    opts={k:v for k,v in opts.items() if v is not None}
    resp_id=f"resp_{uuid.uuid4().hex[:24]}"
    created=int(time.time())
    if not stream:
        try:
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], opts)
            text=(res["choices"][0]["message"].get("content") or "")
            # map to responses shape
            out={
                "id": resp_id,
                "object":"response",
                "created_at": created,
                "model": route["modelId"],
                "status":"completed",
                "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text": text}]}],
                "usage": res.get("usage") or {"input_tokens": est, "output_tokens": len(text)//4, "total_tokens": est+len(text)//4}
            }
            # log
            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                         (route["platform"], route["modelId"], route["keyId"], "success", out["usage"].get("input_tokens",0), out["usage"].get("output_tokens",0), 0))
            conn.commit()
            return JSONResponse(content=out, headers={"X-Routed-Via": routed_via_value(route["platform"], route["modelId"])})
        except Exception as e:
            return _err(502, f"Provider error ({route['platform']}): {e}")
    else:
        async def gen():
            # SSE sequence: response.created, response.output_text.delta, response.output_text.done, response.content_part.done, response.output_item.done, response.completed
            yield f"event: response.created\ndata: {json.dumps({'type':'response.created','response':{'id':resp_id,'object':'response','created_at':created,'model':route['modelId'],'status':'in_progress'}})}\n\n"
            text_acc=""
            try:
                async for chunk in provider.stream_chat_completion(route["apiKey"], messages, route["modelId"], opts):
                    for ch in chunk.get("choices") or []:
                        delta=ch.get("delta") or {}
                        if delta.get("content"):
                            txt=delta["content"]
                            text_acc+=txt
                            yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','delta': txt})}\n\n"
                        if ch.get("finish_reason"):
                            yield f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','text': text_acc})}\n\n"
                            yield f"event: response.content_part.done\ndata: {json.dumps({'type':'response.content_part.done'})}\n\n"
                            yield f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','item':{'type':'message','role':'assistant','content':[{'type':'output_text','text': text_acc}]}})}\n\n"
                            # commit
                            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text_acc)//4, 0))
                            conn.commit()
                            yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':{'id':resp_id,'status':'completed'}})}\n\n"
                            return
                # if stream ended without finish
                if text_acc:
                    yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':{'id':resp_id,'status':'completed'}})}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)[:300]})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Routed-Via": routed_via_value(route["platform"], route["modelId"])})
