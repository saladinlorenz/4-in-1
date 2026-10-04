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

def _err(s,m):
    return JSONResponse(status_code=s, content={"type":"error","error":{"type":"invalid_request_error","message":m}})

def _openai_messages_from_anthropic(body: dict) -> list[dict]:
    msgs=[]
    system=body.get("system")
    if system:
        if isinstance(system, str):
            msgs.append({"role":"system","content": system})
        elif isinstance(system, list):
            txt="\n".join(p.get("text","") for p in system if isinstance(p,dict))
            msgs.append({"role":"system","content": txt})
    for m in body.get("messages") or []:
        role=m.get("role")
        content=m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
        elif isinstance(content, list):
            # anthropic blocks: text, image, tool_use, tool_result
            parts=[]
            tool_calls=[]
            for blk in content:
                t=blk.get("type")
                if t=="text":
                    parts.append({"type":"text","text": blk.get("text","")})
                elif t=="image":
                    src=blk.get("source") or {}
                    if src.get("type")=="base64":
                        parts.append({"type":"image_url","image_url":{"url": f"data:{src.get('media_type','image/jpeg')};base64,{src.get('data','')}"}})
                elif t=="tool_use":
                    tool_calls.append({"id": blk.get("id"),"type":"function","function":{"name": blk.get("name"),"arguments": json.dumps(blk.get("input") or {})}})
            if tool_calls:
                msgs.append({"role":"assistant","content": None, "tool_calls": tool_calls})
                # if there were also text parts, add as separate? anthropic mixes, but openai expects separate
                if parts:
                    msgs[-1]["content"]="".join(p["text"] for p in parts)
            else:
                msgs.append({"role": role, "content": parts if parts else ""})
        # tool_result is separate message with role tool in anthropic? actually user with tool_result blocks
        # handle tool_result blocks inside user message
        if isinstance(content, list):
            for blk in content:
                if blk.get("type")=="tool_result":
                    # add as tool message
                    msgs.append({"role":"tool","tool_call_id": blk.get("tool_use_id"),"content": blk.get("content") if isinstance(blk.get("content"), str) else json.dumps(blk.get("content"))})
    return msgs

def _anthropic_from_openai(res: dict, orig_model: str) -> dict:
    msg=res["choices"][0]["message"]
    content_blocks=[]
    text=msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type":"text","text": text})
    for tc in msg.get("tool_calls") or []:
        try:
            args=json.loads(tc["function"]["arguments"])
        except Exception:
            args={}
        content_blocks.append({"type":"tool_use","id": tc["id"],"name": tc["function"]["name"],"input": args})
    stop_map={"stop":"end_turn","length":"max_tokens","tool_calls":"tool_use","content_filter":"stop"}
    fr=stop_map.get(res["choices"][0].get("finish_reason") or "stop","end_turn")
    usage=res.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type":"message",
        "role":"assistant",
        "model": orig_model,
        "content": content_blocks,
        "stop_reason": fr,
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens",0),"output_tokens": usage.get("completion_tokens",0)}
    }

@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    try:
        body=await request.json()
    except Exception:
        return _err(400, "Invalid JSON")
    stream=bool(body.get("stream"))
    max_tokens=body.get("max_tokens") or 1024
    model=body.get("model") or "auto"
    # anthropic model map? simplified: if model starts with claude, map to auto
    if model.startswith("claude"):
        # check settings anthropic_model_map
        row=conn.execute("SELECT value FROM settings WHERE key='anthropic_model_map'").fetchone()
        # ignore, just use auto for now
        model="auto"
    messages=_openai_messages_from_anthropic(body)
    tools=None
    if body.get("tools"):
        tools=[{"type":"function","function":{"name": t.get("name"),"description": t.get("description"),"parameters": t.get("input_schema")}} for t in body["tools"]]
    tool_choice=None
    tc=body.get("tool_choice")
    if tc:
        if tc.get("type")=="tool":
            tool_choice={"type":"function","function":{"name": tc.get("name")}}
        elif tc.get("type")=="any":
            tool_choice="required"
    est=estimate_tokens(messages, tools)
    try:
        route=route_request(conn, estimated_tokens=est, messages=messages, wants_tools=bool(tools), output_reserve=max_tokens, requested_model=model)
    except Exception as e:
        return _err(getattr(e,"status",429), str(e))
    provider=route["provider"]
    opts={"max_tokens": max_tokens, "temperature": body.get("temperature"), "top_p": body.get("top_p"), "stop": body.get("stop_sequences"), "tools": tools, "tool_choice": tool_choice}
    opts={k:v for k,v in opts.items() if v is not None}
    if not stream:
        try:
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], opts)
            out=_anthropic_from_openai(res, body.get("model") or route["modelId"])
            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                         (route["platform"], route["modelId"], route["keyId"], "success", out["usage"]["input_tokens"], out["usage"]["output_tokens"], 0))
            conn.commit()
            return JSONResponse(content=out, headers={"X-Routed-Via": routed_via_value(route["platform"], route["modelId"])})
        except Exception as e:
            return _err(502, f"Provider error ({route['platform']}): {e}")
    else:
        async def gen():
            msg_id=f"msg_{uuid.uuid4().hex[:24]}"
            yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','model': body.get('model') or route['modelId'],'content':[],'stop_reason':None,'usage':{'input_tokens': est,'output_tokens':0}}})}\n\n"
            yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
            text_acc=""
            try:
                async for chunk in provider.stream_chat_completion(route["apiKey"], messages, route["modelId"], opts):
                    for ch in chunk.get("choices") or []:
                        delta=ch.get("delta") or {}
                        if delta.get("content"):
                            txt=delta["content"]
                            text_acc+=txt
                            yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text': txt}})}\n\n"
                        if delta.get("tool_calls"):
                            # emit as tool_use blocks (simplified: after stream)
                            pass
                        if ch.get("finish_reason"):
                            yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
                            fr=ch["finish_reason"]
                            amap={"stop":"end_turn","length":"max_tokens","tool_calls":"tool_use"}
                            yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason': amap.get(fr,'end_turn'),'stop_sequence':None},'usage':{'output_tokens': len(text_acc)//4}})}\n\n"
                            yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"
                            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text_acc)//4, 0))
                            conn.commit()
                            return
                # fallback if no finish
                yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
                yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens': len(text_acc)//4}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'type':'error','error':{'type':'api_error','message': str(e)[:300]}})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Routed-Via": routed_via_value(route["platform"], route["modelId"])})

@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    try:
        body=await request.json()
    except Exception:
        body={}
    messages=_openai_messages_from_anthropic(body)
    est=estimate_tokens(messages)
    return JSONResponse(content={"input_tokens": est})

# GET /v1/models is handled by proxy.py with content-negotiation; anthropic discovery is merged there.
