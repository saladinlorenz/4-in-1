from __future__ import annotations
import asyncio, json, time, httpx
from .base import BaseProvider, ProviderHttpError, provider_timeout_ms

class AIHordeProvider(BaseProvider):
    def __init__(self):
        self.platform="aihorde"
        self.name="AI Horde"
        self.timeout_ms=provider_timeout_ms("aihorde", 120000)
        self.base_url="https://aihorde.net/api/v2"
        self.keyless=False

    async def chat_completion(self, api_key, messages, model_id, options=None):
        # AI Horde is queue-based: POST /generate/text/async then poll /generate/text/status/{id}
        # Simplified: map OpenAI messages to single prompt
        prompt="\n".join(f"{m.get('role')}: {m.get('content') or ''}" for m in messages)
        max_tokens=(options or {}).get("max_tokens") or 256
        if max_tokens < 16:
            max_tokens=16
        headers={"apikey": api_key if api_key and api_key!="no-key" else "0000000000", "Content-Type":"application/json"}
        payload={"prompt": prompt, "params":{"max_length": max_tokens, "temperature": (options or {}).get("temperature",0.7)}, "models": [model_id] if model_id else [], "workers":[]}
        async with httpx.AsyncClient(timeout=self.timeout_ms/1000 if self.timeout_ms>0 else None) as client:
            resp=await client.post(f"{self.base_url}/generate/text/async", headers=headers, json=payload)
            if resp.status_code>=400:
                raise ProviderHttpError(f"AI Horde async error {resp.status_code}: {resp.text}", status=resp.status_code)
            job=resp.json()
            jid=job.get("id")
            if not jid:
                raise RuntimeError("AI Horde: no job id returned")
            # poll
            for _ in range(60):
                await asyncio.sleep(2)
                st=await client.get(f"{self.base_url}/generate/text/status/{jid}", headers=headers)
                js=st.json()
                if js.get("done"):
                    gens=js.get("generations") or []
                    text=gens[0].get("text","") if gens else ""
                    return {"id":f"chatcmpl-{int(time.time())}","object":"chat.completion","created":int(time.time()),"model":model_id or "aihorde","choices":[{"index":0,"message":{"role":"assistant","content": text},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens": len(text)//4,"total_tokens": len(text)//4},"_routed_via":{"platform":"aihorde","model":model_id}}
                if js.get("faulted"):
                    raise RuntimeError(f"AI Horde job faulted: {js}")
            raise TimeoutError("AI Horde generation timed out")

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        # AI Horde has no streaming; synthesize as one chunk stream
        res=await self.chat_completion(api_key, messages, model_id, options)
        base={"id":res["id"],"object":"chat.completion.chunk","created":res["created"],"model":res["model"]}
        text=(res["choices"][0]["message"].get("content") or "")
        yield {**base, "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":None}]}
        if text:
            yield {**base, "choices":[{"index":0,"delta":{"content": text},"finish_reason":None}]}
        yield {**base, "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

    async def validate_key(self, api_key):
        # keyless anonymous is valid; real key: GET /find_user
        if not api_key or api_key=="no-key":
            return True
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp=await client.get(f"{self.base_url}/find_user", headers={"apikey": api_key})
                if resp.status_code in (401,403):
                    return {"valid":False,"error": f"AI Horde key validation failed (HTTP {resp.status_code})"}
                return True
        except Exception as e:
            return {"valid":False,"error": str(e)}
