from __future__ import annotations
import json
import time
from typing import AsyncGenerator

import httpx

from .base import BaseProvider, ProviderHttpError, provider_http_error, provider_timeout_ms
from ..lib.sampling_params import extended_body_params, resolve_max_tokens
from ..lib.think_tags import extract_think_from_message

class OpenAICompatProvider(BaseProvider):
    def __init__(self, platform: str, name: str, base_url: str, extra_headers: dict | None = None, validate_url: str | None = None, timeout_ms: int | None = None, keyless: bool = False, force_single_tool_call: bool = False):
        self.platform = platform
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.extra_headers = extra_headers or {}
        self.validate_url = validate_url
        self.timeout_ms = provider_timeout_ms(platform, timeout_ms if timeout_ms is not None else 60000)
        self.keyless = keyless
        self.force_single_tool_call = force_single_tool_call

    def _auth_header(self, api_key: str) -> dict:
        if self.keyless:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def _sampling(self, model_id: str, options: dict | None):
        if self.platform=="requesty" and model_id=="mistral/leanstral-1-5" and options and options.get("temperature")==0:
            return None, options.get("top_p",1)
        if not options:
            return None, None
        return options.get("temperature"), options.get("top_p")

    def _messages_for_platform(self, messages):
        if self.platform!="mistral":
            return messages
        out=[]
        for m in messages:
            if m.get("role")=="assistant":
                nm={"role":"assistant","content":m.get("content")}
                if m.get("tool_calls"):
                    nm["tool_calls"]=[{"id":tc["id"],"type":tc["type"],"function":{"name":tc["function"]["name"],"arguments":tc["function"]["arguments"]}} for tc in m["tool_calls"]]
                out.append(nm)
            elif m.get("role")=="tool":
                out.append({"role":"tool","content":m.get("content"),"tool_call_id":m.get("tool_call_id")})
            else:
                out.append({"role":m.get("role"),"content":m.get("content")})
        return out

    def _parallel_tool_calls(self, options):
        if self.force_single_tool_call and options and options.get("tools"):
            return False
        return options.get("parallel_tool_calls") if options else None

    async def chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> dict:
        temp, top_p = self._sampling(model_id, options)
        body={
            "model": model_id,
            "messages": self._messages_for_platform(messages),
        }
        if temp is not None:
            body["temperature"]=temp
        mt=resolve_max_tokens(self.platform, (options or {}).get("max_tokens"))
        if mt is not None:
            body["max_tokens"]=mt
        if top_p is not None:
            body["top_p"]=top_p
        if options:
            for k in ("stop","tools","tool_choice"):
                if options.get(k) is not None:
                    body[k]=options[k]
            ptc=self._parallel_tool_calls(options)
            if ptc is not None:
                body["parallel_tool_calls"]=ptc
            body.update(extended_body_params(self.platform, options))
        headers={**self._auth_header(api_key), "Content-Type":"application/json", **self.extra_headers}
        timeout = (options or {}).get("timeout_ms", self.timeout_ms)
        url=f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=(timeout/1000 if timeout>0 else None)) as client:
            resp=await client.post(url, headers=headers, json=body)
            # quota observation hook (no-op if not configured)
            try:
                from ..services.quota import record_quota_from_response
                record_quota_from_response(resp, self.platform, model_id)
            except Exception:
                pass
            if not resp.is_success:
                try:
                    err_body=resp.json()
                except Exception:
                    err_body={"error": resp.text}
                # rescue tool_calls_section failure (issue #264)
                failed = None
                try:
                    failed = err_body.get("error",{}).get("failed_generation")
                except Exception:
                    pass
                if isinstance(failed,str) and failed and options and options.get("tools"):
                    from ..lib.tool_rescue import rescue_inline_tool_calls
                    names={t["function"]["name"] for t in options["tools"]}
                    r=rescue_inline_tool_calls(failed, names)
                    if r["detected"] and r["calls"]:
                        from ..lib.tool_rescue import repair_tool_arguments
                        rescued=[{"id":f"call_rescued_{i+1}","type":"function","function":{"name":c["name"],"arguments":repair_tool_arguments(c["arguments"])}} for i,c in enumerate(r["calls"])]
                        return {"id":f"chatcmpl-rescued-{int(time.time())}","object":"chat.completion","created":int(time.time()),"model":model_id,"choices":[{"index":0,"message":{"role":"assistant","content":None,"tool_calls":rescued},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0},"_routed_via":{"platform":self.platform,"model":model_id}}
                # upstream error text
                msg = ""
                if isinstance(err_body, dict):
                    msg = err_body.get("error",{}).get("message") or err_body.get("detail") or err_body.get("title") or resp.text
                raise provider_http_error(resp, f"{self.name} API error {resp.status_code}: {msg}", err_body)
            try:
                data=resp.json()
            except Exception as e:
                # truncated?
                ct=resp.headers.get("content-type","").lower()
                if "application/json" in ct and "Unexpected end" in str(e):
                    raise RuntimeError(f"{self.name} returned 200 but body truncated (proxy/CDN idle timeout)")
                raise RuntimeError(f"{self.name} returned 200 with non-JSON body — endpoint not OpenAI-compatible. Check base URL.")
            # normalize choices (mistral array content, reasoning_content fold)
            for ch in data.get("choices") or []:
                msg=ch.get("message") or {}
                if isinstance(msg.get("content"), list):
                    msg["content"]="".join(seg.get("text","") if isinstance(seg,dict) else str(seg) for seg in msg["content"])
                extract_think_from_message(msg)
                if not msg.get("tool_calls") and (msg.get("content")== "" or msg.get("content") is None):
                    fold = msg.get("reasoning_content") or msg.get("reasoning")
                    if isinstance(fold,str) and fold:
                        msg["content"]=fold
            data["_routed_via"]={"platform":self.platform,"model":model_id}
            return data

    async def stream_chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> AsyncGenerator[dict, None]:
        temp, top_p = self._sampling(model_id, options)
        body={
            "model": model_id,
            "messages": self._messages_for_platform(messages),
            "stream": True,
        }
        if temp is not None:
            body["temperature"]=temp
        mt=resolve_max_tokens(self.platform, (options or {}).get("max_tokens"))
        if mt is not None:
            body["max_tokens"]=mt
        if top_p is not None:
            body["top_p"]=top_p
        if options:
            for k in ("stop","tools","tool_choice"):
                if options.get(k) is not None:
                    body[k]=options[k]
            ptc=self._parallel_tool_calls(options)
            if ptc is not None:
                body["parallel_tool_calls"]=ptc
            body.update(extended_body_params(self.platform, options))
            if options.get("stream_options"):
                body["stream_options"]=options["stream_options"]
        headers={**self._auth_header(api_key), "Content-Type":"application/json", **self.extra_headers}
        timeout = (options or {}).get("timeout_ms", self.timeout_ms)
        url=f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=(timeout/1000 if timeout>0 else None)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                try:
                    from ..services.quota import record_quota_from_response
                    record_quota_from_response(resp, self.platform, model_id)
                except Exception:
                    pass
                if resp.status_code>=400:
                    try:
                        err_body=resp.json()
                    except Exception:
                        text=await resp.aread()
                        err_body={"error": text.decode(errors="ignore")}
                    failed=None
                    try:
                        failed=err_body.get("error",{}).get("failed_generation")
                    except Exception:
                        pass
                    if isinstance(failed,str) and failed and options and options.get("tools"):
                        from ..lib.tool_rescue import rescue_inline_tool_calls, repair_tool_arguments
                        names={t["function"]["name"] for t in options["tools"]}
                        r=rescue_inline_tool_calls(failed, names)
                        if r["detected"] and r["calls"]:
                            rescued=[{"id":f"call_rescued_{i+1}","type":"function","function":{"name":c["name"],"arguments":repair_tool_arguments(c["arguments"])}} for i,c in enumerate(r["calls"])]
                            base={"id":f"chatcmpl-rescued-{int(time.time())}","object":"chat.completion.chunk","created":int(time.time()),"model":model_id}
                            yield {**base, "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":None}]}
                            yield {**base, "choices":[{"index":0,"delta":{"tool_calls":[{"index":i,**c} for i,c in enumerate(rescued)]},"finish_reason":None}]}
                            yield {**base, "choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
                            return
                    msg=""
                    if isinstance(err_body,dict):
                        msg=err_body.get("error",{}).get("message") or err_body.get("detail") or ""
                    raise provider_http_error(resp, f"{self.name} API error {resp.status_code}: {msg}", err_body)
                async for chunk in self.read_sse_stream(resp):
                    yield chunk

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    async def fetch_model_catalog(self, api_key: str):
        headers={**self._auth_header(api_key), **self.extra_headers}
        async with httpx.AsyncClient(timeout=30) as client:
            resp=await client.get(self.models_url, headers=headers)
            return resp

    async def validate_key(self, api_key: str) -> bool | dict:
        url = self.validate_url or self.models_url
        headers={**self._auth_header(api_key), **self.extra_headers}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp=await client.get(url, headers=headers)
                if resp.status_code not in (401,403):
                    return True
                try:
                    body=resp.json()
                    detail = body.get("error",{}).get("message") or body.get("message") or resp.text
                except Exception:
                    detail=resp.text
                return {"valid": False, "error": f"{self.name} key validation failed (HTTP {resp.status_code}): {detail}"}
        except httpx.TimeoutException:
            return True
        except Exception as e:
            return {"valid": False, "error": str(e)}
