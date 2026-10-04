from __future__ import annotations
import json
import time
from typing import AsyncGenerator

import httpx

from .base import BaseProvider, provider_http_error, provider_timeout_ms

# Google Gemini native wire — maps OpenAI messages to Gemini contents

def _openai_to_gemini(messages: list[dict], tools=None, tool_choice=None):
    contents=[]
    system_parts=[]
    for m in messages:
        role=m.get("role")
        content=m.get("content")
        if role=="system":
            system_parts.append({"text": content if isinstance(content,str) else str(content)})
        elif role=="user":
            parts=[]
            if isinstance(content, list):
                for p in content:
                    if isinstance(p,dict) and p.get("type")=="text":
                        parts.append({"text": p.get("text","")})
                    elif isinstance(p,dict) and p.get("type")=="image_url":
                        # simplified: pass as text placeholder — real impl would download+base64
                        url=p.get("image_url",{}).get("url","")
                        parts.append({"text": f"[image: {url[:80]}]"})
            else:
                parts.append({"text": content or ""})
            # tool results (role tool) are mapped as user parts
            contents.append({"role":"user","parts":parts})
        elif role=="assistant":
            tcs=m.get("tool_calls") or []
            if tcs:
                # functionCall parts
                parts=[]
                for tc in tcs:
                    fn=tc.get("function",{})
                    try:
                        args=json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args={}
                    parts.append({"functionCall":{"name":fn.get("name",""),"args":args}})
                contents.append({"role":"model","parts":parts})
            elif isinstance(content,str) and content:
                contents.append({"role":"model","parts":[{"text":content}]})
        elif role=="tool":
            # functionResponse
            call_id=m.get("tool_call_id","")
            # find matching tool name from previous assistant calls? fallback to generic
            try:
                resp_text = content if isinstance(content,str) else json.dumps(content)
            except Exception:
                resp_text=str(content)
            # need function name — search previous assistant message tool_calls
            # simplified: use unknown
            contents.append({"role":"user","parts":[{"functionResponse":{"name": call_id or "unknown","response":{"result": resp_text}}}]})
    payload={"contents": contents}
    if system_parts:
        payload["systemInstruction"]={"parts": system_parts}
    if tools:
        # map OpenAI tools to Gemini functionDeclarations
        decls=[]
        for t in tools:
            fn=t.get("function",{})
            decl={"name": fn.get("name",""),"description": fn.get("description","")}
            params=fn.get("parameters")
            if params:
                decl["parameters"]=params
            decls.append(decl)
        payload["tools"]=[{"functionDeclarations": decls}]
        if tool_choice:
            # map to toolConfig
            if isinstance(tool_choice, dict) and tool_choice.get("type")=="function":
                payload["toolConfig"]={"functionCallingConfig":{"mode":"ANY"}}
            elif tool_choice=="required":
                payload["toolConfig"]={"functionCallingConfig":{"mode":"ANY"}}
            elif tool_choice=="none":
                payload["toolConfig"]={"functionCallingConfig":{"mode":"NONE"}}
    return payload

def _gemini_to_openai(resp_json: dict, model_id: str) -> dict:
    cand = (resp_json.get("candidates") or [{}])[0]
    content = cand.get("content") or {}
    parts = content.get("parts") or []
    text_parts=[]
    tool_calls=[]
    for p in parts:
        if "text" in p:
            text_parts.append(p["text"])
        if "functionCall" in p:
            fc=p["functionCall"]
            tool_calls.append({"id":f"call_{fc.get('name')}_{len(tool_calls)}","type":"function","function":{"name":fc.get("name",""),"arguments": json.dumps(fc.get("args") or {})}})
    text = "".join(text_parts) if text_parts else None
    finish = cand.get("finishReason") or "STOP"
    mapping={"STOP":"stop","MAX_TOKENS":"length","SAFETY":"content_filter","RECITATION":"content_filter","OTHER":"stop","TOOL_CALLS":"tool_calls"}
    fr = mapping.get(finish, "stop")
    if tool_calls:
        fr="tool_calls"
        text=None
    usage=resp_json.get("usageMetadata") or {}
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object":"chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices":[{"index":0,"message":{"role":"assistant","content": text, **({"tool_calls": tool_calls} if tool_calls else {})},"finish_reason": fr}],
        "usage": {"prompt_tokens": usage.get("promptTokenCount",0),"completion_tokens": usage.get("candidatesTokenCount",0),"total_tokens": usage.get("totalTokenCount",0)},
        "_routed_via": {"platform":"google","model":model_id}
    }

class GoogleProvider(BaseProvider):
    def __init__(self, timeout_ms: int = 60000):
        self.platform="google"
        self.name="Google"
        self.timeout_ms=provider_timeout_ms("google", timeout_ms)
        self.base_url="https://generativelanguage.googleapis.com/v1beta"

    async def chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> dict:
        payload=_openai_to_gemini(messages, (options or {}).get("tools"), (options or {}).get("tool_choice"))
        # generationConfig
        gen={}
        if options:
            if options.get("temperature") is not None:
                gen["temperature"]=options["temperature"]
            if options.get("top_p") is not None:
                gen["topP"]=options["top_p"]
            if options.get("max_tokens") is not None:
                gen["maxOutputTokens"]=options["max_tokens"]
            if options.get("stop"):
                stop=options["stop"]
                gen["stopSequences"]= stop if isinstance(stop,list) else [stop]
        if gen:
            payload["generationConfig"]=gen
        url=f"{self.base_url}/models/{model_id}:generateContent?key={api_key}"
        timeout=(options or {}).get("timeout_ms", self.timeout_ms)
        async with httpx.AsyncClient(timeout=(timeout/1000 if timeout>0 else None)) as client:
            resp=await client.post(url, json=payload, headers={"Content-Type":"application/json"})
            if not resp.is_success:
                try:
                    body=resp.json()
                    msg=body.get("error",{}).get("message") or resp.text
                except Exception:
                    body=None
                    msg=resp.text
                raise provider_http_error(resp, f"Google API error {resp.status_code}: {msg}", body)
            data=resp.json()
            return _gemini_to_openai(data, model_id)

    async def stream_chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> AsyncGenerator[dict, None]:
        payload=_openai_to_gemini(messages, (options or {}).get("tools"), (options or {}).get("tool_choice"))
        gen={}
        if options:
            if options.get("temperature") is not None:
                gen["temperature"]=options["temperature"]
            if options.get("top_p") is not None:
                gen["topP"]=options["top_p"]
            if options.get("max_tokens") is not None:
                gen["maxOutputTokens"]=options["max_tokens"]
        if gen:
            payload["generationConfig"]=gen
        url=f"{self.base_url}/models/{model_id}:streamGenerateContent?alt=sse&key={api_key}"
        timeout=(options or {}).get("timeout_ms", self.timeout_ms)
        base={"id":f"chatcmpl-{int(time.time())}","object":"chat.completion.chunk","created":int(time.time()),"model":model_id}
        async with httpx.AsyncClient(timeout=(timeout/1000 if timeout>0 else None)) as client:
            async with client.stream("POST", url, json=payload, headers={"Content-Type":"application/json"}) as resp:
                if resp.status_code>=400:
                    try:
                        body=resp.json()
                        msg=body.get("error",{}).get("message") or ""
                    except Exception:
                        body=None
                        msg=""
                    raise provider_http_error(resp, f"Google API error {resp.status_code}: {msg}", body)
                yield {**base, "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":None}]}
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data=line[6:]
                    if not data.strip():
                        continue
                    try:
                        obj=json.loads(data)
                    except Exception:
                        continue
                    cand=(obj.get("candidates") or [{}])[0]
                    parts=(cand.get("content") or {}).get("parts") or []
                    for p in parts:
                        if "text" in p and p["text"]:
                            yield {**base, "choices":[{"index":0,"delta":{"content": p["text"]},"finish_reason":None}]}
                        if "functionCall" in p:
                            fc=p["functionCall"]
                            tc={"id":f"call_{fc.get('name','')}_0","type":"function","function":{"name":fc.get("name",""),"arguments": json.dumps(fc.get("args") or {})}}
                            yield {**base, "choices":[{"index":0,"delta":{"tool_calls":[{"index":0, **tc}]},"finish_reason":None}]}
                    fr=cand.get("finishReason")
                    if fr:
                        mapping={"STOP":"stop","MAX_TOKENS":"length","SAFETY":"content_filter","RECITATION":"content_filter","OTHER":"stop"}
                        yield {**base, "choices":[{"index":0,"delta":{},"finish_reason": mapping.get(fr,"stop")}]}
                # usage not streamed for google — synthesize

    async def validate_key(self, api_key: str):
        url=f"{self.base_url}/models?key={api_key}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp=await client.get(url)
                if resp.status_code in (401,403):
                    try:
                        body=resp.json()
                        detail=body.get("error",{}).get("message") or resp.text
                    except Exception:
                        detail=resp.text
                    return {"valid": False, "error": f"Google key validation failed (HTTP {resp.status_code}): {detail}"}
                return True
        except Exception as e:
            return {"valid": False, "error": str(e)}
