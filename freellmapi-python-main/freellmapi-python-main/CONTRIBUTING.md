# 🤝 Contributing to FreeLLMAPI (Python Port)

Thank you for your interest in contributing to **FreeLLMAPI Python**! 🚀

This project is a 100% pure Python high-performance port and evolution of the original [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi). We welcome contributions of all kinds: **new provider adapters**, **router algorithms**, **bug fixes**, **documentation improvements**, **performance optimizations**, and **unit/integration tests**.

---

## 🧭 Codebase Architecture Overview

Before jumping in, here is how the repository is structured:

```
freellmapi/
├── freellm/
│   ├── app.py               # FastAPI application setup, middleware, lifecycle & routing inclusion
│   ├── config.py            # Environment variables and global settings configuration
│   ├── crypto.py            # AES-256-GCM encryption for stored API keys & unified token generation
│   ├── db.py                # SQLite database connection & WAL mode pragmas
│   ├── schema.py            # SQLite table definitions & automated schema migrations
│   ├── seeds.py             # Default initial catalog of free models, embedding models & quirks
│   ├── service.py           # Background service lifecycle manager (PID tracking, logs)
│   ├── tray.py              # Windows system tray integration (pystray + PIL)
│   ├── lib/                 # Core utilities
│   │   ├── tokens.py        # Token count estimator (heuristic + tools + images)
│   │   ├── error_classify.py# Error categorizer (rate-limit, timeout, auth, payload)
│   │   ├── tool_rescue.py   # Dialect parser (Kimi, Llama, Qwen XML, raw JSON repairs)
│   │   ├── think_tags.py    # Extracts reasoning tokens / <think> blocks
│   │   ├── budget.py        # Token budget parser (e.g. "~15M", "credits-based")
│   │   └── fallback_loop.py # Multi-attempt resilient fallback orchestration
│   ├── providers/           # Provider adapters
│   │   ├── base.py          # BaseProvider abstract class definition
│   │   ├── openai_compat.py # Generic OpenAI-compatible HTTP client adapter
│   │   ├── google.py        # Native Google Gemini REST adapter
│   │   ├── cohere.py        # Cohere native chat API adapter
│   │   ├── cloudflare.py    # Cloudflare Workers AI adapter
│   │   ├── pollinations.py  # Pollinations keyless adapter
│   │   ├── zhipu.py         # Zhipu AI GLM adapter
│   │   ├── modelscope.py    # ModelScope (Alibaba) adapter
│   │   ├── aihorde.py       # AI Horde community crowd-sourced adapter
│   │   └── registry.py      # Provider catalog, metadata URLs & provider lookup
│   ├── services/            # Intelligent proxy services
│   │   ├── router.py        # Smart routing engine (Bandit scoring, tier matching)
│   │   ├── scoring.py       # Multi-objective scoring (Reliability, Speed, Intelligence)
│   │   ├── ratelimit.py     # Sliding window RPM/RPD/TPM/TPD & cooldown ladder
│   │   ├── fusion.py        # Multi-model fan-out & judge synthesis
│   │   ├── cache.py         # In-memory response caching
│   │   ├── sticky.py        # Session stickiness & profile routing
│   │   ├── quirks.py        # Platform-specific anomalies & behavior adjustments
│   │   ├── catalog_sync.py  # Model catalog synchronization
│   │   └── compression/     # Token prompt compression engines
│   ├── routes/              # API endpoints
│   │   ├── proxy.py         # /v1/chat/completions & /v1/completions (OpenAI compatible)
│   │   ├── anthropic.py     # /v1/messages (Anthropic native protocol)
│   │   ├── gemini.py        # /v1beta/models/* (Gemini native protocol)
│   │   ├── ollama.py        # /api/* (Ollama emulation for IDEs like Cursor/Zed/Continue)
│   │   ├── responses_api.py # /v1/responses (Codex CLI format)
│   │   ├── embeddings.py    # /v1/embeddings (Vector embeddings)
│   │   ├── media.py         # /v1/images/* & /v1/audio/* (FLUX, TTS/STT)
│   │   ├── mcp.py           # /mcp (Model Context Protocol JSON-RPC server)
│   │   ├── admin.py         # /api/* (Dashboard backend, analytics, key management)
│   │   └── middleware.py    # Token auth & IP rate limit middleware
│   └── static/              # Embedded single-page dashboard UI & icons
├── scripts/                 # Build scripts & diagnostics
│   ├── build_exe.py         # PyInstaller standalone executable builder
│   └── verify_free_tiers.py # Script to test and validate free provider availability
└── data/                    # Local SQLite database & logs storage
```

---

## 🛠️ Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/freellmapi-python.git
   cd freellmapi-python
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv

   # On Windows:
   .venv\Scripts\activate

   # On Linux / macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -e ".[dev,tray,build]"
   ```

4. **Setup environment variables:**
   ```bash
   cp .env.example .env
   # Generate a 64-char hex key for AES encryption:
   python -c "import secrets; print(secrets.token_hex(32))"
   # Paste into ENCRYPTION_KEY in .env
   ```

5. **Start in development mode with auto-reload:**
   ```bash
   uvicorn freellm.app:create_app --factory --host 127.0.0.1 --port 3001 --reload
   ```
   Open `http://localhost:3001` in your browser to access the dashboard.

---

## 🔌 How to Add a New Provider Adapter

Adding a new free-tier provider is straightforward:

1. **If the provider is OpenAI-compatible:**
   Open `freellm/providers/registry.py` and register it:
   ```python
   _register(OpenAICompatProvider(
       platform="myprovider",
       name="My Provider",
       base_url="https://api.myprovider.com/v1",
       extra_headers={"User-Agent": "FreeLLMAPI/1.0"}
   ))
   ```

2. **If the provider uses a custom wire format:**
   - Create `freellm/providers/myprovider.py` subclassing `BaseProvider` from `freellm/providers/base.py`.
   - Implement `chat_completion(...)`, `stream_chat_completion(...)`, and `validate_key(...)`.
   - Register the instance in `freellm/providers/registry.py`.

3. **Add Seed Models in `freellm/seeds.py`:**
   Add model entries to `MODELS`:
   ```python
   # ("platform", "model_id", "display_name", intelligence_rank, speed_rank, size_label, rpm, rpd, tpm, tpd, budget, ctx, vision, tools)
   ("myprovider", "model-name-v1", "Model Display Name", 10, 5, "Large", 30, 1000, 50000, None, "~15M", 131072, 1, 1),
   ```

4. **Add Provider Metadata in `PROVIDER_META` (`freellm/providers/registry.py`):**
   ```python
   "myprovider": {
       "website": "https://myprovider.com",
       "signup": "https://myprovider.com/signup",
       "docs": "https://myprovider.com/docs"
   }
   ```

---

## 🎯 High-Priority Roadmap & Contribution Ideas

We are actively seeking contributors for:

- [ ] **New Free Providers**: Adapting newly emerging free inference tiers (GLM-4, DeepSeek-V3/R1 providers, OpenRouter free models, local Ollama relays).
- [ ] **Prompt Compression**: Completing lossy/lossless prompt compressors in `freellm/services/compression/` to save precious free token budgets.
- [ ] **Extended Test Suite**: Adding comprehensive pytest mocks for all provider failure and streaming modes.
- [ ] **Web Dashboard Enhancements**: Refining UI visualizations, dark mode, live latency graphs, and token consumption analytics.
- [ ] **Linux / macOS System Tray**: Enhancing cross-platform system tray parity using appindicator / PySide where available.
- [ ] **Model Catalog Auto-Updater**: Continuous background verification of free quota status and deprecated model auto-discovery.

---

## 📝 Pull Request Guidelines

1. **Keep it focused**: One feature or bug fix per Pull Request.
2. **Preserve code quality**: Follow PEP 8 guidelines and type hinting wherever possible.
3. **Test your changes**: Ensure the server boots cleanly and handles requests without breaking existing routes.
4. **Describe your PR**: Explain what you changed and why, linking any relevant issues.

---

## 💬 Community & Questions

- Open an [Issue](https://github.com/your-username/freellmapi-python/issues) for feature discussions or bug reports.
- Join in discussions and help make AI accessible to every developer for free! 🎉
