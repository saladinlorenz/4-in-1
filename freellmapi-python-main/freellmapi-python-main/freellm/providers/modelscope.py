from __future__ import annotations
import httpx
from .base import BaseProvider, provider_timeout_ms

class ModelScopeProvider(BaseProvider):
    def __init__(self):
        self.platform="modelscope"
        self.name="ModelScope"
        self.timeout_ms=provider_timeout_ms("modelscope", 60000)
        self.base_url="https://api-inference.modelscope.cn/v1"

    async def chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("modelscope","ModelScope", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        return await tmp.chat_completion(api_key, messages, model_id, options)

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("modelscope","ModelScope", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        async for c in tmp.stream_chat_completion(api_key, messages, model_id, options):
            yield c

    async def validate_key(self, api_key):
        # GET /v1/models returns 200 even for garbage — probe with 1-token chat
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp=await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}","Content-Type":"application/json"}, json={"model": "Qwen/Qwen3-8B","messages":[{"role":"user","content":"hi"}],"max_tokens":1})
                if resp.status_code in (401,403):
                    return {"valid":False,"error": f"ModelScope key validation failed (HTTP {resp.status_code}): {resp.text[:300]}"}
                return True
        except Exception as e:
            return {"valid":False,"error": str(e)}
