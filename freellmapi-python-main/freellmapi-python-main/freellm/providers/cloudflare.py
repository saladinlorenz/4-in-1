from __future__ import annotations
import httpx
from .base import BaseProvider, provider_timeout_ms

class CloudflareProvider(BaseProvider):
    def __init__(self):
        self.platform="cloudflare"
        self.name="Cloudflare"
        self.timeout_ms=provider_timeout_ms("cloudflare", 60000)

    def _parse_key(self, api_key: str):
        # key = "account_id:token" or just token with account in env
        if ":" in api_key:
            acc, tok=api_key.split(":",1)
            return acc.strip(), tok.strip()
        return None, api_key

    async def chat_completion(self, api_key, messages, model_id, options=None):
        acc, tok=self._parse_key(api_key)
        # Cloudflare OpenAI-compatible endpoint: https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions
        # Fallback to ai gateway if no account
        if acc:
            base=f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1"
        else:
            base="https://api.cloudflare.com/client/v4/accounts/auto/ai/v1"
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("cloudflare","Cloudflare", base)
        tmp.timeout_ms=self.timeout_ms
        # Cloudflare uses Bearer token
        return await tmp.chat_completion(tok, messages, model_id, options)

    async def stream_chat_completion(self, api_key, messages, model_id, options=None):
        acc, tok=self._parse_key(api_key)
        base=f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1" if acc else "https://api.cloudflare.com/client/v4/accounts/auto/ai/v1"
        from .openai_compat import OpenAICompatProvider
        tmp=OpenAICompatProvider("cloudflare","Cloudflare", base)
        tmp.timeout_ms=self.timeout_ms
        async for c in tmp.stream_chat_completion(tok, messages, model_id, options):
            yield c

    async def validate_key(self, api_key):
        acc, tok=self._parse_key(api_key)
        if not acc:
            return {"valid":False,"error":"Cloudflare key format: account_id:token"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp=await client.get(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/models/search", headers={"Authorization": f"Bearer {tok}"})
                if resp.status_code in (401,403):
                    return {"valid":False,"error": f"Cloudflare key validation failed (HTTP {resp.status_code}): {resp.text[:200]}"}
                return True
        except Exception as e:
            return {"valid":False,"error": str(e)}
