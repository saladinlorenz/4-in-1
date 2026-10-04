from __future__ import annotations
import json
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..db import get_db
from .middleware import extract_api_token, validate_unified_key
from ..services.model_listing import build_model_listing
from ..lib.tokens import estimate_tokens

router = APIRouter()

def _err(code, msg):
    return JSONResponse(status_code=code, content={"error":{"code": code, "message": msg, "status": "INVALID_ARGUMENT" if code==400 else "UNAUTHENTICATED"}})

@router.get("/v1beta/models")
async def list_models(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if token and not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    avail = request.query_params.get("available")=="true"
    objs, auto_ctx=build_model_listing(conn, available_only=avail)
    # gemini shape
    data=[]
    for o in objs:
        ctx=o.get("context_window") or auto_ctx
        data.append({
            "name": f"models/{o['id']}",
            "displayName": o.get("display_name") or o["id"],
            "description": f"{o['platform']}/{o['id']}",
            "inputTokenLimit": ctx,
            "outputTokenLimit": min(8192, ctx),
            "supportedGenerationMethods": ["generateContent","streamGenerateContent","countTokens"]
        })
    # add auto
    data.insert(0, {"name":"models/auto","displayName":"Auto (router)","inputTokenLimit": auto_ctx, "outputTokenLimit": 8192, "supportedGenerationMethods":["generateContent","streamGenerateContent","countTokens"]})
    return JSONResponse(content={"models": data})

@router.get("/v1beta/models/{model:path}")
async def get_model(model: str, request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if token and not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    if model.startswith("models/"):
        model=model[7:]
    objs, auto_ctx=build_model_listing(conn)
    for o in objs:
        if o["id"]==model or model=="auto":
            ctx=o.get("context_window") or auto_ctx
            return JSONResponse(content={"name": f"models/{model}","displayName": o.get("display_name") or model,"inputTokenLimit": ctx, "outputTokenLimit": min(8192, ctx), "supportedGenerationMethods":["generateContent","streamGenerateContent","countTokens"]})
    return _err(404, f"Model {model} not found")

def _gemini_to_openai_messages(body: dict) -> list[dict]:
    contents=body.get("contents") or []
    msgs=[]
    sys_instr=body.get("systemInstruction")
    if sys_instr:
        parts=sys_instr.get("parts") or []
        txt="".join(p.get("text","") for p in parts)
        if txt:
            msgs.append({"role":"system","content": txt})
    for c in contents:
        role=c.get("role") or "user"
        parts=c.get("parts") or []
        # check for functionCall / functionResponse
        has_fc=any("functionCall" in p for p in parts)
        has_fr=any("functionResponse" in p for p in parts)
        if has_fc:
            tcs=[]
            for p in parts:
                if "functionCall" in p:
                    fc=p["functionCall"]
                    tcs.append({"id": f"call_{fc.get('name','')}_{len(tcs)}","type":"function","function":{"name": fc.get("name",""),"arguments": json.dumps(fc.get("args") or {})}})
            msgs.append({"role":"assistant","content": None, "tool_calls": tcs})
        elif has_fr:
            for p in parts:
                if "functionResponse" in p:
                    fr=p["functionResponse"]
                    msgs.append({"role":"tool","tool_call_id": fr.get("name",""),"content": json.dumps(fr.get("response") or {})})
        else:
            txt="".join(p.get("text","") for p in parts if "text" in p)
            if role=="model":
                role="assistant"
            msgs.append({"role": role, "content": txt})
    return msgs

@router.post("/v1beta/models/{model:path}:generateContent")
async def generate_content(model: str, request: Request):
    return await _handle_generate(model, request, stream=False)

@router.post("/v1beta/models/{model:path}:streamGenerateContent")
async def stream_generate_content(model: str, request: Request):
    return await _handle_generate(model, request, stream=True)

@router.post("/v1beta/models/{model:path}:countTokens")
async def count_tokens(model: str, request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if token and not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    try:
        body=await request.json()
    except Exception:
        body={}
    msgs=_gemini_to_openai_messages(body)
    est=estimate_tokens(msgs)
    return JSONResponse(content={"totalTokens": est, "promptTokens": est})

async def _handle_generate(model: str, request: Request, stream: bool):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key")
    if model.startswith("models/"):
        model=model[7:]
    if model.endswith(":generateContent"):
        model=model[:-16]
    if model.endswith(":streamGenerateContent"):
        model=model[:-22]
    try:
        body=await request.json()
    except Exception:
        body={}
    messages=_gemini_to_openai_messages(body)
    if not messages:
        return _err(400, "contents is required")
    # generationConfig
    gen=body.get("generationConfig") or {}
    max_tokens=gen.get("maxOutputTokens")
    temp=gen.get("temperature")
    top_p=gen.get("topP")
    stop=gen.get("stopSequences")
    tools=None
    if body.get("tools"):
        tools=[]
        for tl in body["tools"]:
            for fd in tl.get("functionDeclarations") or []:
                tools.append({"type":"function","function":{"name": fd.get("name"),"description": fd.get("description"),"parameters": fd.get("parameters")}})
    est=estimate_tokens(messages, tools)
    from ..services.router import route_request
    req_model=model if model!="auto" else "auto"
    try:
        route=route_request(conn, estimated_tokens=est, messages=messages, wants_tools=bool(tools), output_reserve=max_tokens or 8192, requested_model=req_model)
    except Exception as e:
        return _err(getattr(e,"status",429), str(e))
    provider=route["provider"]
    opts={}
    if max_tokens:
        opts["max_tokens"]=max_tokens
    if temp is not None:
        opts["temperature"]=temp
    if top_p is not None:
        opts["top_p"]=top_p
    if stop:
        opts["stop"]=stop
    if tools:
        opts["tools"]=tools
    if not stream:
        try:
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], opts)
            text=(res["choices"][0]["message"].get("content") or "")
            parts=[{"text": text}] if text else []
            # tool calls?
            tcs=res["choices"][0]["message"].get("tool_calls") or []
            for tc in tcs:
                try:
                    args=json.loads(tc["function"]["arguments"])
                except Exception:
                    args={}
                parts.append({"functionCall":{"name": tc["function"]["name"],"args": args}})
            finish=res["choices"][0].get("finish_reason") or "stop"
            fmap={"stop":"STOP","length":"MAX_TOKENS","tool_calls":"STOP","content_filter":"SAFETY"}
            usage=res.get("usage") or {}
            return JSONResponse(content={
                "candidates":[{"content":{"role":"model","parts": parts},"finishReason": fmap.get(finish,"STOP"),"index":0}],
                "usageMetadata":{"promptTokenCount": usage.get("prompt_tokens",0),"candidatesTokenCount": usage.get("completion_tokens",0),"totalTokenCount": usage.get("total_tokens",0)},
                "modelVersion": route["modelId"]
            }, headers={"X-Routed-Via": f"{route['platform']}/{route['modelId']}"})
        except Exception as e:
            return _err(502, str(e)[:500])
    else:
        alt=request.query_params.get("alt")
        is_sse=alt=="sse"
        async def sse_gen():
            try:
                async for chunk in provider.stream_chat_completion(route["apiKey"], messages, route["modelId"], opts):
                    for ch in chunk.get("choices") or []:
                        delta=ch.get("delta") or {}
                        parts=[]
                        if delta.get("content"):
                            parts.append({"text": delta["content"]})
                        if delta.get("reasoning_content"):
                            parts.append({"text": delta["reasoning_content"], "thought": True})
                        # tool calls delta not mapped for gemini streaming simplified
                        if parts:
                            payload={"candidates":[{"content":{"role":"model","parts": parts},"index":0}]}
                            yield f"data: {json.dumps(payload)}\n\n"
                        if ch.get("finish_reason"):
                            fmap={"stop":"STOP","length":"MAX_TOKENS","tool_calls":"STOP"}
                            yield f"data: {json.dumps({'candidates':[{'finishReason': fmap.get(ch['finish_reason'],'STOP'),'index':0}], 'usageMetadata':{'promptTokenCount': est,'candidatesTokenCount':0,'totalTokenCount':est}})}\n\n"
                if not is_sse:
                    pass
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)[:300]})}\n\n"
        if is_sse:
            return StreamingResponse(sse_gen(), media_type="text/event-stream", headers={"X-Routed-Via": f"{route['platform']}/{route['modelId']}"})
        else:
            # JSON array streaming
            async def json_gen():
                yield "["
                first=True
                async for chunk in sse_gen():
                    # sse_gen yields data: lines, need to extract json
                    for line in chunk.splitlines():
                        if line.startswith("data: "):
                            js=line[6:]
                            if not first:
                                yield ","
                            yield js
                            first=False
                yield "\n]"
            return StreamingResponse(json_gen(), media_type="application/json", headers={"X-Routed-Via": f"{route['platform']}/{route['modelId']}"})
