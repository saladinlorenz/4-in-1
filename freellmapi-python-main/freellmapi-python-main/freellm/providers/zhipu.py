from __future__ import annotations
import httpx
from .base import BaseProvider, provider_timeout_ms

class ZhipuProvider(BaseProvider):
    def __init__(self, timeout_ms: int = 60000):
        self.platform="zhipu"
        self.name="Zhipu"
        self.timeout_ms=provider_timeout_ms("zhipu", timeout_ms)
        self.base_url="https://open.bigmodel.cn/api/paas/v4"

    async def chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("zhipu","Zhipu", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        # try domestic host first; on auth failure try global host
        try:
            return await tmp.chat_completion(api_key, messages, model_id, options)
        except Exception as e:
            s=getattr(e,"status",None)
            if s in (401,403):
                alt=OpenAICompatProvider("zhipu","Zhipu","https://api.z.ai/api/paas/v4")
                alt.timeout_ms=self.timeout_ms
                return await alt.chat_completion(api_key, messages, model_id, options)
            raise

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("zhipu","Zhipu", self.base_url)
        tmp.timeout_ms=self.timeout_ms
        async for c in tmp.stream_chat_completion(api_key, messages, model_id, options):
            yield c

    async def validate_key(self, api_key):
        for base in ["https://open.bigmodel.cn/api/paas/v4","https://api.z.ai/api/paas/v4"]:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp=await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
                    if resp.status_code not in (401,403):
                        return True
            except Exception:
                continue
        return {"valid":False,"error": "Zhipu key validation failed on both hosts"}
