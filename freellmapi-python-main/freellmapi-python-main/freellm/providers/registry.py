from __future__ import annotations
from .openai_compat import OpenAICompatProvider
from .google import GoogleProvider
from .cohere import CohereProvider
from .cloudflare import CloudflareProvider
from .aihorde import AIHordeProvider
from .modelscope import ModelScopeProvider
from .pollinations import PollinationsProvider
from .zhipu import ZhipuProvider

# Website / signup URLs for UI — affichés à côté du select provider
PROVIDER_META = {
    "google": {"website": "https://ai.google.dev", "signup": "https://aistudio.google.com/apikey", "docs": "https://ai.google.dev/gemini-api/docs"},
    "groq": {"website": "https://groq.com", "signup": "https://console.groq.com/keys", "docs": "https://console.groq.com/docs"},
    "cerebras": {"website": "https://cerebras.ai", "signup": "https://cloud.cerebras.ai/", "docs": "https://inference-docs.cerebras.ai/"},
    "mistral": {"website": "https://mistral.ai", "signup": "https://console.mistral.ai/api-keys/", "docs": "https://docs.mistral.ai/"},
    "openrouter": {"website": "https://openrouter.ai", "signup": "https://openrouter.ai/keys", "docs": "https://openrouter.ai/docs"},
    "cohere": {"website": "https://cohere.com", "signup": "https://dashboard.cohere.com/api-keys", "docs": "https://docs.cohere.com/"},
    "cloudflare": {"website": "https://developers.cloudflare.com/workers-ai/", "signup": "https://dash.cloudflare.com/?to=/:account/workers/ai", "docs": "https://developers.cloudflare.com/workers-ai/"},
    "zhipu": {"website": "https://zhipuai.cn", "signup": "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys", "docs": "https://open.bigmodel.cn/dev/howuse/introduction"},
    "nvidia": {"website": "https://build.nvidia.com", "signup": "https://build.nvidia.com/explore/reasoning", "docs": "https://docs.api.nvidia.com/nim/"},
    "github": {"website": "https://github.com/marketplace/models", "signup": "https://github.com/settings/tokens", "docs": "https://docs.github.com/en/github-models"},
    "huggingface": {"website": "https://huggingface.co", "signup": "https://huggingface.co/settings/tokens", "docs": "https://huggingface.co/docs/inference-providers/"},
    "kilo": {"website": "https://kilo.ai", "signup": "https://kilo.ai/dashboard", "docs": "https://kilo.ai/docs"},
    "ovh": {"website": "https://www.ovhcloud.com/en/public-cloud/ai-endpoints/", "signup": "https://www.ovh.com/manager/public-cloud/", "docs": "https://docs.ovh.com/gb/en/ai/"},
    "pollinations": {"website": "https://pollinations.ai", "signup": "https://pollinations.ai/", "docs": "https://github.com/pollinations/pollinations/blob/master/APIDOCS.md"},
    "ollama": {"website": "https://ollama.com", "signup": "https://ollama.com/settings/keys", "docs": "https://ollama.com/blog"},
    "modelscope": {"website": "https://modelscope.cn", "signup": "https://modelscope.cn/my/myaccesstoken", "docs": "https://modelscope.cn/docs"},
    "qianfan": {"website": "https://qianfan.cloud.baidu.com", "signup": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application", "docs": "https://cloud.baidu.com/doc/WENXINWORKSHOP/"},
    "volcengine": {"website": "https://www.volcengine.com", "signup": "https://console.volcengine.com/ark", "docs": "https://www.volcengine.com/docs/82379"},
    "siliconflow": {"website": "https://siliconflow.cn", "signup": "https://cloud.siliconflow.cn/account/ak", "docs": "https://docs.siliconflow.cn/"},
    "reka": {"website": "https://www.reka.ai", "signup": "https://platform.reka.ai/", "docs": "https://docs.reka.ai/"},
    "aihorde": {"website": "https://aihorde.net", "signup": "https://aihorde.net/register", "docs": "https://aihorde.net/api"},
    "opencode": {"website": "https://opencode.ai", "signup": "https://opencode.ai/auth", "docs": "https://opencode.ai/docs"},
    "anyapi": {"website": "https://anyapi.ai", "signup": "https://anyapi.ai/dashboard", "docs": "https://anyapi.ai/docs"},
    "routeway": {"website": "https://routeway.ai", "signup": "https://routeway.ai/dashboard", "docs": "https://routeway.ai/docs"},
    "bazaarlink": {"website": "https://bazaarlink.ai", "signup": "https://bazaarlink.ai/dashboard", "docs": "https://bazaarlink.ai/docs"},
    "orcarouter": {"website": "https://orcarouter.ai", "signup": "https://orcarouter.ai/dashboard", "docs": "https://orcarouter.ai/docs"},
    "xkiro": {"website": "https://xkiro.com", "signup": "https://xkiro.com/dashboard", "docs": "https://xkiro.com/docs"},
    "sambanova": {"website": "https://sambanova.ai", "signup": "https://cloud.sambanova.ai/", "docs": "https://docs.sambanova.ai/"},
    "custom": {"website": "", "signup": "", "docs": ""},
}

_providers: dict[str, object] = {}

def _register(p):
    _providers[p.platform]=p

_register(GoogleProvider(timeout_ms=60000))
_register(OpenAICompatProvider("groq","Groq","https://api.groq.com/openai/v1"))
_register(OpenAICompatProvider("cerebras","Cerebras","https://api.cerebras.ai/v1"))
_register(OpenAICompatProvider("bai","B.AI","https://api.b.ai/v1"))
_register(OpenAICompatProvider("anyapi","AnyAPI","https://api.anyapi.ai/v1"))
_register(OpenAICompatProvider("nvidia","NVIDIA NIM","https://integrate.api.nvidia.com/v1", force_single_tool_call=True, timeout_ms=180000))
_register(OpenAICompatProvider("sambanova","SambaNova","https://api.sambanova.ai/v1"))
_register(OpenAICompatProvider("mistral","Mistral","https://api.mistral.ai/v1"))
_register(OpenAICompatProvider("openrouter","OpenRouter","https://openrouter.ai/api/v1", extra_headers={"HTTP-Referer":"http://localhost:3001","X-Title":"FreeLLMAPI"}))
_register(OpenAICompatProvider("github","GitHub Models","https://models.github.ai/inference"))
_register(CohereProvider())
_register(CloudflareProvider())
_register(ZhipuProvider(timeout_ms=60000))
_register(OpenAICompatProvider("huggingface","HuggingFace Router","https://router.huggingface.co/v1"))
_register(OpenAICompatProvider("ollama","Ollama Cloud","https://ollama.com/v1", timeout_ms=120000))
_register(OpenAICompatProvider("kilo","Kilo Gateway","https://api.kilo.ai/api/gateway/v1", validate_url="https://api.kilo.ai/api/gateway/models", keyless=True))
_register(PollinationsProvider())
_register(OpenAICompatProvider("llm7","LLM7","https://api.llm7.io/v1"))
_register(OpenAICompatProvider("opencode","OpenCode Zen","https://opencode.ai/zen/v1"))
_register(OpenAICompatProvider("ovh","OVH AI Endpoints","https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", keyless=True))
_register(OpenAICompatProvider("agnes","Agnes AI","https://apihub.agnes-ai.com/v1", timeout_ms=60000))
_register(OpenAICompatProvider("reka","Reka","https://api.reka.ai/v1"))
_register(OpenAICompatProvider("siliconflow","SiliconFlow","https://api.siliconflow.com/v1"))
_register(OpenAICompatProvider("routeway","Routeway","https://api.routeway.ai/v1", extra_headers={"User-Agent":"Mozilla/5.0 FreeLLMAPI/1.0"}))
_register(OpenAICompatProvider("bazaarlink","BazaarLink","https://bazaarlink.ai/api/v1"))
_register(OpenAICompatProvider("ainative","AINative Studio","https://api.ainative.studio/api/v1"))
_register(OpenAICompatProvider("aion","Aion Labs","https://api.aionlabs.ai/v1"))
_register(OpenAICompatProvider("requesty","Requesty","https://router.requesty.ai/v1"))
_register(OpenAICompatProvider("navy","NavyAI","https://api.navy/v1", extra_headers={"User-Agent":"FreeLLMAPI/1.0"}))
_register(OpenAICompatProvider("nara","NaraRouter","https://router.bynara.id/v1"))
_register(OpenAICompatProvider("sealion","SEA-LION","https://api.sea-lion.ai/v1"))
_register(OpenAICompatProvider("orcarouter","OrcaRouter","https://api.orcarouter.ai/v1"))
_register(OpenAICompatProvider("unorouter","UnoRouter","https://api.unorouter.com/v1"))
_register(OpenAICompatProvider("xkiro","xKiro","https://api.xkiro.com/v1", validate_url="https://api.xkiro.com/v1/usage"))
_register(ModelScopeProvider())
_register(OpenAICompatProvider("qianfan","Baidu Qianfan","https://qianfan.baidubce.com/v2"))
_register(OpenAICompatProvider("volcengine","Volcengine Ark","https://ark.cn-beijing.volces.com/api/v3"))
_register(OpenAICompatProvider("longcat","LongCat","https://api.longcat.chat/openai/v1"))
_register(OpenAICompatProvider("xfyun","iFlytek Spark","https://spark-api-open.xf-yun.com/v1"))
_register(AIHordeProvider())
_register(OpenAICompatProvider("custom","Custom (OpenAI-compatible)","", timeout_ms=120000))

CUSTOM_TIMEOUT_MS=120000

def get_provider(platform: str):
    return _providers.get(platform)

def has_provider(platform: str) -> bool:
    return platform in _providers

def get_all_providers():
    return list(_providers.values())

def resolve_provider(platform: str, base_url: str | None = None):
    if platform=="custom":
        trimmed=(base_url or "").strip()
        if not trimmed:
            return None
        return OpenAICompatProvider("custom","Custom (OpenAI-compatible)", trimmed, timeout_ms=CUSTOM_TIMEOUT_MS)
    return _providers.get(platform)

def get_provider_meta(platform: str) -> dict:
    return PROVIDER_META.get(platform, {"website": "", "signup": "", "docs": ""})

def list_providers_meta():
    out=[]
    for p in _providers.values():
        meta = PROVIDER_META.get(p.platform, {})
        out.append({
            "platform": p.platform,
            "name": p.name,
            "baseUrl": getattr(p, "baseUrl", getattr(p, "base_url", "")),
            "keyless": getattr(p, "keyless", False),
            "website": meta.get("website",""),
            "signup": meta.get("signup",""),
            "docs": meta.get("docs",""),
        })
    return out
