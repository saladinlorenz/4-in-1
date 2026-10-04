from __future__ import annotations
import httpx
from .base import BaseProvider, provider_timeout_ms

class PollinationsProvider(BaseProvider):
    def __init__(self):
        self.platform="pollinations"
        self.name="Pollinations"
        self.timeout_ms=provider_timeout_ms("pollinations", 60000)
        self.base_url="https://gen.pollinations.ai/v1"

    async def chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("pollinations","Pollinations", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        return await tmp.chat_completion(api_key, messages, model_id, options)

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("pollinations","Pollinations", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        async for c in tmp.stream_chat_completion(api_key, messages, model_id, options):
            yield c

    async def validate_key(self, api_key):
        # GET /v1/models is public (200 for revoked key) — probe /account/key
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp=await client.get("https://gen.pollinations.ai/account/key", headers={"Authorization": f"Bearer {api_key}"})
                if resp.status_code in (401,403):
                    return {"valid":False,"error": f"Pollinations key validation failed (HTTP {resp.status_code})"}
                return True
        except Exception as e:
            return {"valid":False,"error": str(e)}
