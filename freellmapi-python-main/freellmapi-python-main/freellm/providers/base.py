from __future__ import annotations
import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from typing import AsyncGenerator

import httpx

MAX_RETRY_AFTER_MS = 24*60*60*1000
PROTOBUF_DURATION = re.compile(r"^(\d+(?:\.\d+)?)s$")
PROSE_RETRY = re.compile(r"(?:try again|retry)\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours)\b", re.I)
UNIT_MS = {"ms":1,"s":1000,"sec":1000,"secs":1000,"second":1000,"seconds":1000,"m":60000,"min":60000,"mins":60000,"minute":60000,"minutes":60000,"h":3600000,"hour":3600000,"hours":3600000}

def parse_retry_after_ms(value: str | None) -> int | None:
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d+", v):
        return min(int(v)*1000, MAX_RETRY_AFTER_MS)
    try:
        when = int(time.mktime(time.strptime(v, "%a, %d %b %Y %H:%M:%S %Z"))*1000) if "," in v else None
    except Exception:
        when = None
    if when is not None:
        return min(max(0, when - int(time.time()*1000)), MAX_RETRY_AFTER_MS)
    # try ISO
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(v.replace("Z","+00:00"))
        return min(max(0, int(dt.timestamp()*1000) - int(time.time()*1000)), MAX_RETRY_AFTER_MS)
    except Exception:
        return None

def _clamp(ms: float) -> int | None:
    if ms != ms or ms < 0:
        return None
    return min(int(ms), MAX_RETRY_AFTER_MS)

def _find_delay(node, depth=0):
    if depth>6 or node is None or not isinstance(node,(dict,list)):
        return None
    if isinstance(node, list):
        for item in node:
            f=_find_delay(item, depth+1)
            if f is not None:
                return f
        return None
    for k,v in node.items():
        nk=k.lower().replace("_","").replace("-","")
        if nk in ("retrydelay","retryafter","retryafterseconds"):
            if isinstance(v,str):
                m=PROTOBUF_DURATION.match(v.strip())
                if m:
                    return _clamp(float(m.group(1))*1000)
                if re.fullmatch(r"\d+(?:\.\d+)?", v.strip()):
                    return _clamp(float(v.strip())*1000)
            if isinstance(v,(int,float)):
                return _clamp(float(v)*1000)
        f=_find_delay(v, depth+1)
        if f is not None:
            return f
    return None

def parse_stated_retry_ms(body) -> int | None:
    if body is None:
        return None
    if not isinstance(body, str):
        s=_find_delay(body)
        if s is not None:
            return s
    text = body if isinstance(body,str) else json.dumps(body)
    m=PROSE_RETRY.search(text)
    if m:
        unit=UNIT_MS.get(m.group(2).lower())
        if unit:
            return _clamp(float(m.group(1))*unit)
    return None

class ProviderHttpError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after_ms: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_ms = retry_after_ms

def provider_http_error(resp: httpx.Response, message: str, body=None) -> ProviderHttpError:
    retry = None
    try:
        retry = parse_retry_after_ms(resp.headers.get("retry-after"))
    except Exception:
        pass
    if retry is None:
        retry = parse_stated_retry_ms(body)
    err = ProviderHttpError(message, status=resp.status_code, retry_after_ms=retry)
    return err

def provider_timeout_ms(platform: str, default: int = 15000) -> int:
    import os
    key = f"PROVIDER_TIMEOUT_{platform.upper()}"
    raw = os.getenv(key)
    if raw is not None and raw.strip()!="":
        try:
            n=int(float(raw.strip()))
            return max(0,n)
        except ValueError:
            pass
    # also check global stall?
    return default

def stream_stall_timeout_ms(platform: str) -> int:
    import os
    for k in [f"PROVIDER_STREAM_STALL_TIMEOUT_{platform.upper()}", "PROVIDER_STREAM_STALL_TIMEOUT_MS"]:
        raw=os.getenv(k)
        if raw is not None and raw.strip()!="":
            try:
                n=int(float(raw.strip()))
                return max(0,n)
            except ValueError:
                pass
    return 90000

class BaseProvider(ABC):
    platform: str
    name: str
    keyless: bool = False

    @abstractmethod
    async def chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> dict:
        ...

    @abstractmethod
    async def stream_chat_completion(self, api_key: str, messages: list[dict], model_id: str, options: dict | None = None) -> AsyncGenerator[dict, None]:
        ...

    @abstractmethod
    async def validate_key(self, api_key: str) -> bool | dict:
        ...

    async def fetch_with_timeout(self, method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None, timeout_ms: int = 15000, extra_headers: dict | None = None):
        hdrs = {**(headers or {}), **(extra_headers or {})}
        timeout = timeout_ms/1000 if timeout_ms>0 else None
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=hdrs, json=json_body)
            return resp

    def make_id(self) -> str:
        import secrets, time as t
        return f"chatcmpl-{int(t.time())}-{secrets.token_hex(4)}"

    async def read_sse_stream(self, resp: httpx.Response, *, first_byte_timeout_ms: int | None = None, stall_timeout_ms: int | None = None):
        # simple SSE parser for httpx streaming
        import json as js
        saw_finish=False
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            data=line[6:]
            if data=="[DONE]":
                return
            try:
                chunk=js.loads(data)
                # track finish_reason
                for c in chunk.get("choices") or []:
                    if c.get("finish_reason") is not None:
                        saw_finish=True
                # think-tag extraction
                for c in chunk.get("choices") or []:
                    delta=c.get("delta") or {}
                    content=delta.get("content")
                    if isinstance(content,str) and "<think>" in content.lower():
                        from ..lib.think_tags import extract_think_from_stream_text
                        cleaned, reasoning = extract_think_from_stream_text(content)
                        delta["content"]=cleaned
                        if reasoning:
                            delta["reasoning_content"]=reasoning
                yield chunk
            except Exception:
                continue
        if not saw_finish:
            # allow providers that omit [DONE] but sent finish_reason — already tracked. If no finish at all, treat as truncated
            pass
