<div align="center">

# ⚡ FreeLLMAPI — Python Edition

### *The Ultimate 100% Pure Python Free LLM Aggregator & Intelligent Proxy*

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](CONTRIBUTING.md)
[![Providers](https://img.shields.io/badge/Providers-34+-blueviolet?style=for-the-badge)](#-supported-providers--ecosystem)
[![Models](https://img.shields.io/badge/Free_Models-470+-success?style=for-the-badge)](#-supported-providers--ecosystem)

<p align="center">
  <b>Route, balance, and aggregate dozens of free-tier AI providers behind a single, unified, OpenAI-compatible local gateway.</b><br>
  <i>Never hit a rate-limit dead end again. Infinite free inference powered by smart Thompson-Sampling bandit routing.</i>
</p>

---

[🚀 Quick Start](#-quick-start) •
[✨ Key Features](#-features--capabilities) •
[🏛️ Architecture](#%EF%B8%8F-deep-architecture) •
[🔌 Supported Providers](#-supported-providers--ecosystem) •
[📡 API Surfaces](#-supported-api-surfaces) •
[🖥️ Desktop & Tray](#-desktop-system-tray--background-service) •
[🤝 Contributing](#-community--contributing)

---

</div>

> [!NOTE]
> **Acknowledgement & Origins**: This project is an independent, **100% Pure Python implementation & evolution** inspired by the original TypeScript [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) by Tashfeen Ahmed. It brings the full feature set to the Python ecosystem with asynchronous **FastAPI**, **HTTPX**, **AES-256-GCM encryption**, and zero heavy runtime dependencies.

---

## 🌟 Why FreeLLMAPI Python?

Individual AI providers offer generous free tiers (Groq, Cerebras, Google Gemini, OpenRouter, Mistral, SambaNova, Cloudflare, GitHub Models, Zhipu, etc.). However, building production or hobby apps on free tiers quickly runs into:
- 🛑 **Strict Rate Limits** (RPM / RPD / TPM caps causing `429 Too Many Requests`)
- 🧩 **Incompatible Wire Formats** (Anthropic vs. Gemini vs. OpenAI vs. Ollama)
- 📉 **Provider Outages & Transient Errors** (500s, timeouts, socket hangups)
- 🔑 **Fragmented API Key Management**

**FreeLLMAPI Python solves all of this.** It sits between your apps/IDEs (Cursor, Zed, Continue, Codex, Claude Code, LangChain, LlamaIndex, LiteLLM) and all upstream providers, orchestrating an intelligent fallback chain that automatically routes around rate limits, timeouts, and quota exhaustion.

---

## ✨ Features & Capabilities

```
+-----------------------------------------------------------------------------------+
|                                YOUR APPLICATION                                   |
|   (OpenAI SDK / LangChain / Continue / Zed / Cursor / Codex CLI / Claude Code)   |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v  Unified API Key (freellmapi-...)
+-----------------------------------------------------------------------------------+
|                             FreeLLMAPI Python Proxy                               |
|                                                                                   |
|  +----------------------+ +-------------------------+ +------------------------+  |
|  | Multi-Surface APIs   | | Intelligent Router      | | Rate Limiting Engine   |  |
|  | - OpenAI /v1         | | - Multi-Armed Bandit    | | - Sliding RPM/RPD      |  |
|  | - Anthropic /messages| | - Thompson Sampling     | | - Sliding TPM/TPD      |  |
|  | - Gemini /v1beta     | | - 6 Routing Strategies  | | - Cooldown Escalation  |  |
|  | - Ollama /api/*      | | - Dynamic Tool Rescue   | | - Concurrency Leases   |  |
|  | - MCP Tool Server    | | - Fusion Model Ensemble | | - Quota Auto-Learning  |  |
|  +----------------------+ +-------------------------+ +------------------------+  |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | Security & Storage: AES-256-GCM Encrypted Keys | SQLite WAL Database        |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------+-----------------------------------------+
                                          |
       +-------------------+--------------+-------------+--------------------+
       |                   |                            |                    |
       v                   v                            v                    v
+--------------+   +---------------+            +---------------+    +---------------+
| Google Gemini|   | Groq / Cerebr |            | OpenRouter    |    | 30+ Providers |
| 1M+ Ctx Free |   | Ultra Fast LPUs            | 400+ Free Mods|    | Mistral, Zhipu|
+--------------+   +---------------+            +---------------+    +---------------+
```

- 🐍 **100% Pure Python**: Lightweight, fast, transparent, and easy to hack or deploy anywhere.
- 🎯 **6 Intelligent Routing Strategies**:
  - `auto` / `auto:balanced`: Optimal blend of reliability, speed, and intelligence.
  - `auto:smart` / `auto:smartest`: Routes to the highest-reasoning frontier models (Gemini 2.5 Pro, DeepSeek V3, Qwen3 Coder).
  - `auto:fast` / `auto:fastest`: Routes to ultra-low latency providers (Groq, Cerebras LPUs, SambaNova).
  - `auto:reliable`: Maximizes uptime and success rate using Bayesian beta distributions.
  - `auto:cheap`: Minimizes quota burn across shared provider pools.
  - `fusion`: Parallel multi-model fan-out with an automated judge synthesis model!
- 🛡️ **Autonomous Cooldown & Limit Learning**: Automatically catches `429` headers and error payloads, placing models on a progressive cooldown ladder (`90s -> 2m -> 10m -> 1h -> 24h`) without crashing your app.
- 🛠️ **Dynamic Tool-Calling Rescue**: Automatically intercepts and repairs tool-calling dialects across models that output non-standard formats (Kimi XML, Llama/Groq `<function>`, Qwen XML, or raw JSON strings).
- 🧠 **Thought Tag Extraction**: Seamlessly extracts and parses `<think>` reasoning tags from DeepSeek R1 / GLM / Qwen models.
- 🔒 **Enterprise-Grade Security**: Stored provider keys are encrypted with **AES-256-GCM** using unique per-key IVs and authentication tags. Zero telemetry.
- 🌐 **All Major Wire Protocols Supported**:
  - `OpenAI` (`/v1/chat/completions`, `/v1/completions`)
  - `Anthropic Claude` (`/v1/messages`)
  - `Google Gemini` (`/v1beta/models/*`)
  - `Ollama Emulation` (`/api/chat`, `/api/generate`, `/api/tags`)
  - `Codex CLI` (`/v1/responses`)
  - `Model Context Protocol (MCP)` (`/mcp`)
  - `Embeddings & Media` (`/v1/embeddings`, `/v1/images/generations`, `/v1/audio/*`)
- 🖥️ **Windows Tray & Background Daemon**: Native system tray icon, auto-start on boot, single-click background service, and PyInstaller `.exe` generator.
- 📊 **Built-in Web Dashboard**: Rich analytics, real-time request logs, token counter, latency metrics, and API key manager.

---

## 📸 Aperçu

<p align="center">
  <img src="screenshots/dashboard.png" width="850" alt="Dashboard - Vue d'ensemble"><br>
  <em>Dashboard — Vue d'ensemble : stats live, routing intelligent, catalogue 69 modèles</em>
</p>
<p align="center">
  <img src="screenshots/providers.png" width="850" alt="Fournisseurs"><br>
  <em>Fournisseurs — 40 providers gratuits, 1-clic Obtenir une clé + test instantané</em>
</p>

---

## 🚀 Quick Start

### 1. Prerequisites
- **Python 3.10+** (Python 3.11 or 3.12 recommended)
- `git`

### 2. Installation

```bash
# Clone the repository
git clone https://github.com/your-username/freellmapi-python.git
cd freellmapi-python

# Create and activate virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

# Install dependencies (core)
pip install -r requirements.txt

# Or install editable with optional extras (tray & build tools)
pip install -e ".[tray,dev,build]"
```

### 3. Configure Environment

Copy the example environment file and generate a secure 64-character encryption key:

```bash
cp .env.example .env

# Generate a strong AES-256 key
python -c "import secrets; print(secrets.token_hex(32))"
```
*Open `.env` and paste the generated string into `ENCRYPTION_KEY`.*

### 4. Start the Server

You have multiple convenient ways to run FreeLLMAPI:

```bash
# Option A: System Tray mode (Recommended on Windows Desktop)
python -m freellm --tray
# Or double click: freellm-start.bat

# Option B: Headless Background Service
python -m freellm --background
python -m freellm --status      # Check status
python -m freellm --stop        # Stop background service

# Option C: Standard CLI / Server mode
python -m freellm --no-tray --port 3001

# Option D: Development mode with Hot-Reload
uvicorn freellm.app:create_app --factory --host 0.0.0.0 --port 3001 --reload
```

### 5. Access the Dashboard & Configure Keys
Open **[http://localhost:3001](http://localhost:3001)** in your browser:
1. Complete the initial quick setup (create your local admin password).
2. Head to the **Keys** tab and add free API keys from your favorite providers (Google AI Studio, Groq, Cerebras, OpenRouter, Mistral, etc.).
3. Copy your Master **Unified API Key** (`freellmapi-...`).
4. You're ready to go! 🚀

---

## 🐳 Docker Deployment

Run FreeLLMAPI with a single command using Docker or Docker Compose:

```bash
# Using Docker Compose
docker compose up -d --build

# Or standard Docker
docker build -t freellmapi-py .
docker run -d -p 3001:3001 -v $(pwd)/data:/app/data --env-file .env freellmapi-py
```

---

## 💻 API Usage Examples

FreeLLMAPI acts as a drop-in replacement for OpenAI, Anthropic, Gemini, or Ollama. Simply point `base_url` to `http://localhost:3001/v1` and use your master unified key.

### 🐍 Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3001/v1",
    api_key="freellmapi-your-unified-key"
)

# 1. Automatic Smart Routing
response = client.chat.completions.create(
    model="auto",  # Or "auto:fast", "auto:smart", "auto:reliable", "fusion"
    messages=[
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Write a high-performance LRU Cache in Python."}
    ],
    temperature=0.7,
    stream=True
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
```

### ⚡ cURL (Streaming & Tool Calling)

```bash
curl -X POST http://localhost:3001/v1/chat/completions \
  -H "Authorization: Bearer freellmapi-your-unified-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto:fast",
    "messages": [
      {"role": "user", "content": "Explain Quantum Computing in 3 bullet points."}
    ],
    "stream": false
  }'
```

### 🧠 Anthropic Claude Wire Protocol (`/v1/messages`)

```bash
curl -X POST http://localhost:3001/v1/messages \
  -H "x-api-key: freellmapi-your-unified-key" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello Claude! (Routed via FreeLLMAPI)"}
    ]
  }'
```

### 💎 Google Gemini Wire Protocol (`/v1beta`)

```bash
curl -X POST "http://localhost:3001/v1beta/models/auto:generateContent?key=freellmapi-your-unified-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {"role": "user", "parts": [{"text": "Hello Gemini!"}]}
    ]
  }'
```

### 🦙 Ollama Emulation (`/api/chat` for Cursor / Zed / Continue)

Enable Ollama loopback in settings and connect your favorite editor to `http://localhost:3001`:

```bash
curl http://localhost:3001/api/tags
curl -X POST http://localhost:3001/api/chat -d '{
  "model": "auto",
  "messages": [{"role": "user", "content": "Ping!"}]
}'
```

### 🔮 MCP (Model Context Protocol) Server

Connect your AI agent (Claude Desktop, Cursor, Windsurf) to FreeLLMAPI's built-in MCP server at `http://localhost:3001/mcp` to give agents access to dynamic tool calling: `list_models`, `provider_health`, `routing_info`, `usage_summary`, and `set_routing_strategy`.

---

## 🏛️ Deep Architecture

### 1. Smart Thompson-Sampling Bandit Router
Instead of naive round-robin, FreeLLMAPI uses a multi-armed bandit algorithm with Bayesian updating:
- **Reliability Axis**: $\text{Beta}(\alpha, \beta)$ distribution updated continuously based on success, failure, and timeout events.
- **Speed Axis**: Weighted formula blending Token Throughput (tokens/sec) and Time-To-First-Byte (TTFB):
  $$\text{SpeedScore} = 0.6 \cdot (1 - e^{-\text{tok/s} / 60}) + 0.4 \cdot \text{TTFB}_{\text{normalized}}$$
- **Intelligence Axis**: Normalized composite derived from model parameter tiers (*Frontier*, *Large*, *Medium*, *Small*) and intelligence ranking.
- **Headroom & Concurrency Guardrails**: Penalizes models near their RPM / TPM thresholds and acquires concurrency leases to prevent race-condition throttling.

### 2. Multi-Tier Cooldown Escalation Ladder
When upstream providers return `429 Too Many Requests` or transient errors:
1. **Local / Heuristic**: Immediate 5s to 90s cooldown.
2. **Escalation Ladder**: Step-up sequence `[2m -> 10m -> 1h -> 24h]` on repeated triggers.
3. **Authoritative Expiry**: Honors exact `Retry-After` headers or resets at midnight UTC for daily quotas (`RPD` / `TPD`).
4. **Ceiling Learning**: Dynamically parses quota error strings (`limit: 30 rpm`) and updates local constraints automatically.

### 3. Fusion Virtual Model (`model: "fusion"`)
When invoking the `fusion` model:
1. **Fan-Out**: FreeLLMAPI broadcasts the prompt simultaneously to the top $K$ fastest distinct providers.
2. **Execution**: Gathers independent responses concurrently.
3. **Judge Synthesis**: Passes all candidate drafts to a frontier model judge to synthesize the definitive, optimal answer.

---

## 🔌 Supported Providers & Ecosystem

FreeLLMAPI Python integrates adapters for **34+ platforms** and **470+ models**:

| Provider | Top Free Models | Quotas / Limits | Native Adapter |
| :--- | :--- | :--- | :---: |
| 🟢 **Google AI Studio** | Gemini 2.5 Pro, 2.5 Flash, Gemini 3 Flash | 15 RPM / 1M+ Ctx | ✅ Native |
| ⚡ **Groq** | Llama 3.3 70B, Llama 4 Scout, GPT-OSS 120B | 30 RPM / 1,000 RPD | ✅ OpenAI-Compat |
| 🚀 **Cerebras** | Qwen3 235B, GLM-4.7 | 30 RPM / 60k TPM | ✅ OpenAI-Compat |
| 🌐 **OpenRouter** | DeepSeek V3.1, Qwen3 Coder, GLM-4.5, 50+ `:free` | 20 RPM / 200 RPD | ✅ OpenAI-Compat |
| 🌪️ **SambaNova** | DeepSeek V3.2, Llama 3.3 70B | 20 RPM / 200k TPD | ✅ OpenAI-Compat |
| 🇫🇷 **Mistral AI** | Mistral Large 3, Codestral, Devstral | 2 RPM / 500k TPM | ✅ OpenAI-Compat |
| ☁️ **Cloudflare Workers AI** | Llama 3.3 70B, Kimi K2.5, GPT-OSS 120B | Free Daily Neurons | ✅ Custom Workers |
| 💬 **Cohere** | Command R+, Command-A | 20 RPM / 33 RPD | ✅ Native Cohere |
| 🇨🇳 **Zhipu AI** | GLM-4.5 Flash, GLM-4.7 Flash | 1M Free Tokens | ✅ Native Zhipu |
| 🐙 **GitHub Models** | GPT-4o, GPT-4.1 Preview | 10 RPM / 50 RPD | ✅ Azure AI |
| 🤗 **HuggingFace Router** | Llama 3.3 70B Instruct | Rate limited | ✅ OpenAI-Compat |
| 🛡️ **ModelScope (Alibaba)** | Qwen3-Coder 480B | 2,000 RPD Free | ✅ Native ModelScope |
| 🌸 **Pollinations** | OpenAI, Claude models | Keyless Public Tier | ✅ Keyless Adapter |
| 🗝️ **Kilo / OVH / LLM7** | GPT-OSS 120B, GPT-4o Mini | Keyless Sentinels | ✅ Keyless Adapter |
| 🎨 **SiliconFlow** | FLUX.1 Schnell (Image), CosyVoice2 (Audio) | 60 RPM | ✅ Media Adapter |
| 🤖 **AI Horde** | Tiefighter 13B, Community Models | Keyless / Kudos | ✅ AI Horde Async |
| ➕ **And 15+ more** | *Baidu Qianfan, Volcengine, Reka, AnyAPI, Routeway, etc.* | Various | ✅ Extensible |

---

## 🖥️ Desktop System Tray & Background Service

On Windows, FreeLLMAPI includes a dedicated System Tray application and background daemon:

- **Launch with Tray Icon**:
  ```bash
  python -m freellm --tray
  ```
- **System Tray Features**:
  - 🌐 One-click access to Dashboard & Swagger docs.
  - 📊 Live status indicator (Active Models, Configured Keys, Requests Served).
  - 🚀 Toggle Windows Autostart on boot with a single click.
  - 🛑 Clean shutdown and background process management.

- **Standalone Windows `.exe` Builder**:
  Build an executable that runs on any PC without needing Python installed:
  ```bash
  pip install pyinstaller Pillow
  python scripts/build_exe.py
  # Generates dist/FreeLLMAPI.exe (~30 MB)
  ```

---

## ⚙️ Configuration Reference

Configure FreeLLMAPI via `.env` or system environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `ENCRYPTION_KEY` | *(Required)* | 64-character hex key for AES-256-GCM credential encryption |
| `PORT` | `3001` | Port to bind the server on |
| `HOST` | `0.0.0.0` | Host interface to listen on (`0.0.0.0` or `127.0.0.1`) |
| `FREEAPI_DB_PATH` | `./data/freeapi.db` | Path to the SQLite database file |
| `PROXY_RATE_LIMIT_RPM` | `120` | Max client requests per minute allowed on `/v1` routes |
| `ADMIN_RATE_LIMIT_RPM` | `600` | Max requests per minute allowed on admin/dashboard routes |
| `REQUEST_BODY_LIMIT_MB`| `25` | Maximum incoming request body size in megabytes |
| `FALLBACK_TIME_BUDGET_MS`| `45000` | Max overall fallback timeout before returning an error |

---

## 🤝 Community & Contributing

We want to build the most comprehensive, bulletproof free AI infrastructure in the open source world, and **we need your help!**

### 🎯 We are actively looking for contributors to:
- 🔌 **Add new free-tier AI providers and model mappings**
- 🧠 **Enhance prompt compression algorithms** (`freellm/services/compression/`)
- 🧪 **Write unit and integration tests**
- 🎨 **Improve dashboard analytics and visualizations**
- 🐧 **Enhance Linux/macOS desktop tray parity**

👉 **Check out our [Contributing Guide (CONTRIBUTING.md)](CONTRIBUTING.md)** to get started in minutes!

---

## 📜 License & Credits

- **License**: Distributed under the [MIT License](LICENSE).
- **Inspiration**: Heavily inspired by the concept and design of [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) by Tashfeen Ahmed.
- **Python Port & Enhancements**: Maintained with ❤️ by the FreeLLMAPI Python community.

<div align="center">
  <sub>Built with Python, FastAPI, and open-source love. Star ⭐ this repository if you find it helpful!</sub>
</div>
