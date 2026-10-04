from __future__ import annotations
import json, time
import httpx
from .base import BaseProvider, provider_http_error, provider_timeout_ms

class CohereProvider(BaseProvider):
    def __init__(self):
        self.platform="cohere"
        self.name="Cohere"
        self.timeout_ms=provider_timeout_ms("cohere", 60000)
        self.base_url="https://api.cohere.com/compatibility/v1"

    def _auth(self, k): return {"Authorization": f"Bearer {k}"}

    async def chat_completion(self, api_key, messages, model_id, options=None):
        # Cohere compat endpoint speaks OpenAI wire — reuse same as openai_compat
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("cohere","Cohere", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        return await tmp.chat_completion(api_key, messages, model_id, options)

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("cohere","Cohere", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        async for chunk in tmp.stream_chat_completion(api_key, messages, model_id, options):
            yield chunk

    async def validate_key(self, api_key):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp=await client.get(f"{self.base_url}/models", headers={**self._auth(api_key)})
                if resp.status_code in (401,403):
                    try:
                        b=resp.json()
                        d=b.get("message") or resp.text
                    except Exception:
                        d=resp.text
                    return {"valid":False,"error":f"Cohere key validation failed (HTTP {resp.status_code}): {d}"}
                return True
        except Exception as e:
            return {"valid":False,"error": str(e)}
