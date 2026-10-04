from __future__ import annotations
import sqlite3

# Catalogue initial — subset représentatif fidèle au TS (quotas réels avril 2026)
# Le catalogue complet est ensuite synchronisé via catalog-sync (freellm.co)
MODELS = [
    # google
    ("google", "gemini-2.5-pro", "Gemini 2.5 Pro", 6, 8, "Frontier", 5, 50, 250000, None, "~6M", 1048576, 1, 0),
    ("google", "gemini-2.5-flash", "Gemini 2.5 Flash", 14, 5, "Large", 10, 20, 250000, None, "~3M", 1048576, 1, 1),
    ("google", "gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", 20, 3, "Medium", 15, 20, 250000, None, "~3M", 1048576, 0, 0),
    ("google", "gemini-3-flash-preview", "Gemini 3 Flash Preview", 11, 5, "Large", 10, 20, 250000, None, "~3M", 1048576, 1, 1),
    ("google", "gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", 1, 8, "Frontier", 5, 20, 250000, None, "~3M", 1048576, 1, 1),
    # openrouter — 20 RPM / 200 RPD / ~6M shared
    ("openrouter", "deepseek/deepseek-v3.1:free", "DeepSeek V3.1 (free)", 2, 10, "Frontier", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("openrouter", "qwen/qwen3-coder:free", "Qwen3 Coder (free)", 2, 9, "Frontier", 20, 200, None, None, "~6M", 262144, 0, 1),
    ("openrouter", "z-ai/glm-4.5-air:free", "GLM-4.5 Air (free)", 4, 9, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super 120B (free)", 2, 9, "Frontier", 20, 200, None, None, "~6M", 262144, 0, 1),
    ("openrouter", "openai/gpt-oss-120b:free", "GPT-OSS 120B (free)", 6, 9, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B (free)", 17, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("openrouter", "inclusionai/ling-2.6-1t:free", "Ling 2.6 1T (free)", 4, 9, "Frontier", 20, 200, None, None, "~6M", 262144, 0, 1),
    ("openrouter", "tencent/hy3-preview:free", "Tencent HY3 Preview (free)", 7, 9, "Frontier", 20, 200, None, None, "~6M", 262144, 0, 1),
    ("openrouter", "google/gemma-4-31b-it:free", "Gemma 4 31B (free)", 19, 9, "Medium", 20, 200, None, None, "~6M", 262144, 0, 0),
    # groq — 30 RPM / 1000 RPD
    ("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B", 17, 2, "Medium", 30, 1000, 12000, 500000, "~15M", 131072, 0, 1),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct", "Llama 4 Scout", 12, 2, "Medium", 30, 1000, 6000, 1000000, "~30M", 131072, 1, 1),
    ("groq", "openai/gpt-oss-120b", "GPT-OSS 120B (Groq)", 6, 2, "Large", 30, 1000, 8000, 200000, "~6M", 131072, 0, 1),
    ("groq", "openai/gpt-oss-20b", "GPT-OSS 20B (Groq)", 18, 2, "Medium", 30, 1000, 8000, 200000, "~6M", 131072, 0, 1),
    ("groq", "qwen/qwen3-32b", "Qwen3 32B (Groq)", 19, 2, "Medium", 60, 1000, 6000, 500000, "~15M", 131072, 0, 1),
    ("groq", "llama-3.1-8b-instant", "Llama 3.1 8B Instant", 28, 2, "Small", 30, 14400, 6000, 500000, "~15M", 131072, 0, 0),
    # cerebras — 30 RPM / 60k TPM / 1M TPD
    ("cerebras", "qwen-3-235b-a22b-instruct-2507", "Qwen3 235B", 6, 1, "Large", 30, None, 60000, 1000000, "~30M", 8192, 0, 1),
    ("cerebras", "zai-glm-4.7", "GLM-4.7 (Cerebras)", 7, 1, "Frontier", 10, 100, None, None, "~3M", 8192, 0, 1),
    # sambanova — 20 RPM / 20 RPD / 200k TPD
    ("sambanova", "DeepSeek-V3.1", "DeepSeek V3.1", 5, 9, "Frontier", 20, 20, None, 200000, "~3M", 131072, 0, 1),
    ("sambanova", "DeepSeek-V3.2", "DeepSeek V3.2", 4, 9, "Frontier", 20, 20, None, 200000, "~3M", 131072, 0, 1),
    ("sambanova", "Meta-Llama-3.3-70B-Instruct", "Llama 3.3 70B", 17, 9, "Large", 20, 20, None, 200000, "~3M", 8192, 0, 1),
    # mistral — 2 RPM / 500k TPM
    ("mistral", "mistral-large-latest", "Mistral Large 3", 14, 8, "Large", 2, None, 500000, None, "~50-100M", 131072, 0, 1),
    ("mistral", "codestral-latest", "Codestral", 16, 6, "Medium", 2, None, 500000, None, "~50-100M", 32000, 0, 1),
    ("mistral", "devstral-latest", "Devstral", 16, 8, "Medium", 2, None, 500000, None, "~50-100M", 131072, 0, 1),
    ("mistral", "magistral-medium-latest", "Magistral Medium", 21, 8, "Large", 2, None, 500000, None, "~50-100M", 40000, 0, 1),
    # cohere — 20 RPM / 33 RPD
    ("cohere", "command-r-plus-08-2024", "Command R+ (08-2024)", 23, 11, "Large", 20, 33, None, None, "~1-2M", 131072, 0, 1),
    ("cohere", "command-a-03-2025", "Command-A (03-2025)", 27, 11, "Large", 20, 33, None, None, "~1-2M", 131072, 0, 1),
    # cloudflare
    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "Llama 3.3 70B fp8-fast (CF)", 17, 11, "Medium", None, None, None, None, "~18-45M", 131072, 0, 1),
    ("cloudflare", "@cf/openai/gpt-oss-120b", "GPT-OSS 120B (CF)", 6, 11, "Large", None, None, None, None, "~18-45M", 131072, 0, 1),
    ("cloudflare", "@cf/meta/llama-4-scout-17b-16e-instruct", "Llama 4 Scout (CF)", 12, 11, "Large", None, None, None, None, "~18-45M", 131072, 1, 1),
    ("cloudflare", "@cf/moonshotai/kimi-k2.5", "Kimi K2.5 (CF)", 3, 11, "Frontier", None, None, None, None, "~10-20M", 262144, 0, 1),
    # zhipu
    ("zhipu", "glm-4.5-flash", "GLM-4.5 Flash", 15, 4, "Large", None, None, None, 1000000, "~30M", 131072, 0, 1),
    ("zhipu", "glm-4.7-flash", "GLM-4.7 Flash", 18, 4, "Large", None, None, None, 1000000, "~30M", 131072, 0, 1),
    # github
    ("github", "gpt-4o", "GPT-4o", 25, 7, "Large", 10, 50, None, None, "~18M", 128000, 0, 1),
    ("github", "openai/gpt-4.1", "GPT-4.1 (GitHub)", 20, 7, "Large", 10, 50, None, None, "~9M", 8000, 0, 1),
    # nvidia (credit-based, disabled)
    ("nvidia", "meta/llama-3.1-70b-instruct", "Llama 3.1 70B (NV)", 22, 6, "Large", 40, None, None, None, "credits-based", 131072, 0, 0),
    # huggingface
    ("huggingface", "meta-llama/Llama-3.3-70B-Instruct", "Llama 3.3 70B (HF)", 14, 11, "Medium", None, None, None, None, "~1-3M", 131072, 0, 1),
    # kilo (keyless)
    ("kilo", "openai/gpt-oss-120b:free", "GPT-OSS 120B (Kilo free)", 6, 9, "Large", None, None, None, None, "~6M", 131072, 0, 1),
    # ovh (keyless)
    ("ovh", "gpt-oss-120b", "GPT-OSS 120B (OVH)", 6, 11, "Large", 2, None, None, None, "~2M", 131072, 0, 1),
    # pollinations
    ("pollinations", "openai", "OpenAI (Pollinations)", 10, 11, "Medium", None, None, None, None, "~5M", 131072, 0, 1),
    # llm7
    ("llm7", "gpt-4o-mini", "GPT-4o Mini (LLM7)", 20, 11, "Medium", None, None, None, None, "~5M", 131072, 0, 1),
    # opencode
    ("opencode", "glm-4.5-air:free", "GLM-4.5 Air (OpenCode free)", 8, 11, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    # anyapi, routeway, bazaarlink, etc. (catalog-managed, 1 rep each)
    ("anyapi", "meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B (AnyAPI free)", 17, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("routeway", "deepseek/deepseek-v3:free", "DeepSeek V3 (Routeway free)", 2, 9, "Frontier", 5, 200, None, None, "~6M", 131072, 0, 1),
    ("bazaarlink", "auto:free", "Auto Free (BazaarLink)", 10, 9, "Large", None, None, None, None, "~6M", 131072, 0, 1),
    ("ainative", "qwen3-32b:free", "Qwen3 32B (AINative free)", 19, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("aion", "llama-3.3-70b:free", "Llama 3.3 70B (Aion free)", 17, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("requesty", "mistral/leanstral-1-5", "Leanstral 1.5 (Requesty)", 20, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("navy", "llama-3.3-70b", "Llama 3.3 70B (Navy)", 17, 9, "Medium", 20, None, None, 150000, "~4M", 131072, 0, 1),
    ("nara", "mistral-large", "Mistral Large (Nara)", 14, 9, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("sealion", "sealion-v3", "SEA-LION v3", 15, 9, "Large", 10, None, None, None, "~3M", 131072, 0, 1),
    ("orcarouter", "orcarouter/free", "OrcaRouter Free", 10, 9, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("unorouter", "qwen/qwen3-coder:free", "Qwen3 Coder (UnoRouter free)", 2, 9, "Frontier", 20, 200, None, None, "~6M", 262144, 0, 1),
    ("xkiro", "mistral/mistral-large:free", "Mistral Large (xKiro free)", 14, 9, "Large", 20, 200, None, None, "~6M", 131072, 0, 1),
    ("modelscope", "Qwen/Qwen3-Coder-480B-A35B-Instruct", "Qwen3-Coder 480B (ModelScope)", 2, 6, "Frontier", None, 2000, None, None, "~60M", 262144, 0, 1),
    ("qianfan", "ernie-speed-128k", "ERNIE Speed 128K", 20, 9, "Large", 60, None, None, None, "~10M", 131072, 0, 1),
    ("volcengine", "doubao-1.5-pro-32k", "Doubao 1.5 Pro 32K", 10, 8, "Large", None, None, None, None, "~60M", 32768, 0, 1),
    ("longcat", "longcat-flash-chat", "LongCat Flash Chat", 12, 9, "Large", 60, None, None, None, "~10M", 131072, 0, 1),
    ("xfyun", "lite", "Spark Lite (iFlytek)", 20, 9, "Small", 20, None, None, None, "~10M", 8192, 0, 1),
    ("aihorde", "koboldcpp/LLaMA2-13B-Tiefighter", "Tiefighter 13B (AI Horde)", 20, 11, "Medium", None, None, None, None, "~5M", 4096, 0, 1),
    ("ollama", "gpt-oss:120b-cloud", "GPT-OSS 120B (Ollama Cloud)", 6, 9, "Large", None, None, None, None, "~5M", 131072, 0, 1),
    ("agnes", "agnes-2.0-flash", "Agnes 2.0 Flash", 10, 9, "Large", None, None, None, None, "~5M", 131072, 0, 1),
    ("reka", "reka-flash-3", "Reka Flash 3", 12, 9, "Large", 60, None, None, None, "~10M", 131072, 0, 1),
    ("siliconflow", "Qwen/Qwen3-8B", "Qwen3 8B (SiliconFlow)", 20, 9, "Small", 60, None, None, None, "~10M", 32768, 0, 1),
    ("bai", "gpt-4o-mini", "GPT-4o Mini (B.AI)", 20, 9, "Medium", 20, 200, None, None, "~6M", 131072, 0, 1),
]

EMBEDDING_MODELS = [
    ("openai", "openai", "text-embedding-3-small", "Text Embedding 3 Small", 1536, 0, 1, ""),
    ("openai", "openai", "text-embedding-3-large", "Text Embedding 3 Large", 3072, 1, 1, ""),
    ("cohere", "cohere", "embed-english-v3.0", "Embed English v3", 1024, 0, 1, ""),
    ("mistral", "mistral", "mistral-embed", "Mistral Embed", 1024, 0, 1, ""),
]

MEDIA_MODELS = [
    ("image", "siliconflow", "FLUX.1-schnell", "FLUX.1 Schnell", 0, 1, ""),
    ("audio", "siliconflow", "FunAudioLLM/CosyVoice2-0.5B", "CosyVoice2", 0, 1, ""),
]

QUIRKS = [
    ("keyless-anonymous", "Accès anonyme", "Kilo, LLM7 et OVH permettent un accès sans clé (sentinelle).", "info", [("kilo", None), ("llm7", None), ("ovh", None)]),
    ("pollinations-degraded", "Pollinations dégradé", "Capacité partagée, latence variable.", "warning", [("pollinations", None)]),
    ("or-free-cap-account-wide", "OpenRouter free cap", "20 RPM / 200 RPD partagés sur tous les :free.", "info", [("openrouter", "*:free")]),
]

def seed_db(conn: sqlite3.Connection):
    cur = conn.execute("SELECT COUNT(*) FROM models")
    if cur.fetchone()[0] > 0:
        return
    ins = conn.execute
    for m in MODELS:
        platform, model_id, display_name, intel, speed, size_label, rpm, rpd, tpm, tpd, budget, ctx, vision, tools = m
        enabled = 0 if budget == "credits-based" else 1
        ins("INSERT INTO models(platform, model_id, display_name, intelligence_rank, speed_rank, size_label, rpm_limit, rpd_limit, tpm_limit, tpd_limit, monthly_token_budget, context_window, enabled, supports_vision, supports_tools) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (platform, model_id, display_name, intel, speed, size_label, rpm, rpd, tpm, tpd, budget, ctx, enabled, vision, tools))
    # fallback_config
    rows = conn.execute("SELECT id FROM models ORDER BY intelligence_rank ASC, id ASC").fetchall()
    for i, r in enumerate(rows):
        conn.execute("INSERT INTO fallback_config(model_db_id, priority, enabled) VALUES(?,?,1)", (r[0], i+1))
    # embedding/media
    for fam, plat, mid, disp, dim, prio, en, ql in EMBEDDING_MODELS:
        conn.execute("INSERT OR IGNORE INTO embedding_models(family, platform, model_id, display_name, dimensions, priority, enabled, quota_label) VALUES(?,?,?,?,?,?,?,?)",
                     (fam, plat, mid, disp, dim, prio, en, ql))
    for modality, plat, mid, disp, prio, en, ql in MEDIA_MODELS:
        conn.execute("INSERT OR IGNORE INTO media_models(modality, platform, model_id, display_name, priority, enabled, quota_label) VALUES(?,?,?,?,?,?,?)",
                     (modality, plat, mid, disp, prio, en, ql))
    # quirks
    import time
    now = int(time.time()*1000)
    for slug, title, body, sev, targets in QUIRKS:
        conn.execute("INSERT OR IGNORE INTO quirks(slug, title, body, severity, created_at_ms, updated_at_ms) VALUES(?,?,?,?,?,?)",
                     (slug, title, body, sev, now, now))
        for plat, glob in targets:
            conn.execute("INSERT INTO quirk_targets(quirk_id, platform, model_glob) VALUES(?,?,?)", (slug, plat, glob))
    # default profile
    conn.execute("INSERT OR IGNORE INTO profiles(id, name, emoji, color, type, is_favorite, sort_order) VALUES(1,'Default','⭐','#6366f1','default',1,-1)")
    conn.commit()
