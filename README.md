# AgentOS / 4in1

> A lightweight, modular, autonomous personal agent system designed to run continuously on a Debian ARM64 environment under PRoot on Android — without Docker, Node.js, PostgreSQL, Redis, or a mandatory Cloudflare Tunnel.

AgentOS combines an AI agent, persistent tasks, a multi-model layer, Internet tools, a local memory, and (in later phases) a social content management module into a single Python application.

***

## Objective

The project aims to build a **personal autonomous assistant** able to receive missions through Telegram or a local interface, research information on the Internet, use multiple AI models, remember the results, run scheduled workflows, and prepare social media content.

Example missions:

- "Find 20 Spanish companies in the AI field, analyze them, and send me a summary on Telegram."
- "Run a daily watch on new GitHub projects about AI agents."
- "Prepare three LinkedIn posts about AI automation and send me the drafts for approval."
- "Check my failed workflows, retry the ones that can be retried, and notify me of the outcome."
- "Use a free model suited to this task and fall back to another one when the quota is reached."

***

## Principles

- **Python-first**: all business logic is written in Python.
- **Lightweight**: built to run on an Android/Debian VPS with limited resources.
- **Local-first**: tasks, memory, drafts, and incidents are stored locally in SQLite.
- **Modular**: every component can evolve or be replaced without rewriting the whole project.
- **Resilient**: persistent tasks, retries, backoff, restart recovery, and an incident log.
- **Secure by default**: no publishing, deletion, system command, or sensitive external action without explicit confirmation.
- **Outbound Internet only**: no open port and no Cloudflare Tunnel required for normal operation.
- **Remotely controllable**: Telegram allows sending missions, checking status, and receiving results.

***

## Architecture

```text
                         ┌─────────────────────┐
                         │      Telegram       │
                         │  commands + alerts  │
                         └──────────┬──────────┘
                                    │
                                    ▼
                    ┌──────────────────────────┐
                    │         AgentOS          │
                    │        main.py           │
                    └────────────┬─────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
┌───────────────┐        ┌────────────────┐       ┌─────────────────┐
│  AI Agent     │        │  Task Store    │       │ freellmapi      │
│  smolagents   │        │  SQLite + retry│       │ Multi-models    │
└───────┬───────┘        └───────┬────────┘       └────────┬────────┘
        │                        │                         │
        └────────────┬───────────┴────────────┬────────────┘
                     │                        │
                     ▼                        ▼
          ┌───────────────────┐    ┌──────────────────────┐
          │      Tools        │    │  Persistent storage   │
          │                   │    │                       │
          │ - Web search      │    │ - SQLite database     │
          │ - HTTP fetch      │    │ - Memory              │
          │ - Files           │    │ - Tasks               │
          │ - Telegram        │    │ - Drafts              │
          │ - Social (V3)     │    │ - Incidents           │
          └───────────────────┘    └──────────────────────┘
```

Runtime flow:

```text
Mission received (Telegram /ask)
      │
      ▼
Authorization check (allowed user IDs)
      │
      ▼
Agent planning (smolagents, bounded max steps)
      │
      ▼
Tool calls (search, fetch, files, memory, status)
      │
      ▼
LLM calls through freellmapi_adapter (cascade + fallback)
      │
      ▼
Result persisted in SQLite + notification sent
```

***

## Main components

### AI agent

The agent is the brain of the system. It receives a mission, analyzes the request, selects the required tools, calls the AI models, executes the steps, saves the results, and returns a response.

The initial engine uses `smolagents` (installed in editable mode from the bundled checkout) with custom Python tools wrapped in a `RoutedModel` that bridges smolagents to the multi-endpoint LLM router.

The agent does **not** get unlimited access to the system. Tools are controlled and bounded (`AGENT_MAX_STEPS`, `AGENT_MAX_OUTPUT_CHARS`, sandboxed file paths).

```text
User mission
      ↓
Analysis / planning
      ↓
Tool selection
      ↓
Step execution (bounded)
      ↓
Result persistence
      ↓
Telegram notification
```

Key modules:

| Module | Role |
|---|---|
| `agent/core.py` | `AgentRunner`: submit, status, cancel, single active task, interrupt switch |
| `agent/model_router.py` | `RoutedModel(smolagents.Model)` bridging to the endpoint router |
| `agent/permissions.py` | Allow-lists and action policies |
| `agent/tools/` | The nine registered tools (see below) |

### freellmapi-python adapter

`freellmapi_adapter` is the only layer allowed to call AI models. No other module may call Gemini, Groq, OpenRouter, Qwen, DeepSeek, or any other provider directly.

```text
Agent / Runner
      │
      ▼
freellmapi_adapter
      ├── provider.py   HTTP call, retries, redaction of keys,
      │                 drops unsupported parameters on 400
      ├── router.py     ordered cascade over endpoints + cooldown
      └── fallback.py   failure classification, LLMExhausted
```

Configuration is a single ordered list, `LLM_ENDPOINTS`, expressed as JSON: each entry carries `base_url`, `api_key`, `model`, and optional `timeout`. The first healthy endpoint wins; failures cool down and the next endpoint is tried.

This layer provides:

- model selection per task;
- automatic fallback to the next endpoint;
- quota and rate-limit handling;
- timeouts;
- token limits;
- centralized API keys;
- key redaction so secrets never appear in logs.

> Phase 1 note: the bundled `freellmapi-python` gateway is missing its `freellm/lib/` package (spec in `docs/specs/spec lib.txt`), so the adapter speaks the OpenAI-compatible wire format directly against any endpoint, including the gateway once restored.

### Task store

Long-running or scheduled tasks are recorded in SQLite and survive a restart. The phase 1 store implements the task lifecycle (`PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`) plus retry counters, errors, and timestamps. Scheduling, backoff retries, and restart re-queue arrive in V2.

Each task carries at least:

```text
task_id
workflow_name
status
current_step
progress
retry_count
last_error
created_at
updated_at
result
idempotency_key
```

### Agent tools

Tools are plain Python functions independent of the framework, registered through `agent/tools/build_tools()`.

| Tool | Status | Function |
|---|---|---|
| `web_search` | implemented | Web search with DDGS |
| `web_fetch` | implemented | Page download and text extraction with HTTPX |
| `read_file` | implemented | Read files inside the sandbox |
| `write_file` | implemented | Write files inside the sandbox |
| `list_files` | implemented | List sandbox contents |
| `remember` | implemented | Store information in persistent memory |
| `search_memory` | implemented | Query persistent memory |
| `send_notification` | implemented | Push a notification to Telegram |
| `get_status` | implemented | Report agent, task, and endpoint status |
| `model_generate` | planned (V2) | Direct model call via freellmapi |
| `workflow_create` / `workflow_run` / `workflow_status` | planned (V2) | Persistent workflow control |
| `social_create_draft` / `social_schedule` / `social_publish` | planned (V3) | Social draft and publishing flow |

***

## Social management (V3)

The first versions do not try to reinvent Buffer, n8n, or BrightBean Studio. The social module is minimal and controlled:

```text
Idea or mission
      ↓
Research and AI generation
      ↓
Draft creation
      ↓
SQLite storage
      ↓
Telegram delivery
      ↓
User validation
      ↓
Publication through a connected platform adapter
      ↓
API response verification
      ↓
Status update + notification
```

Planned features:

- draft creation;
- SQLite editorial calendar;
- content templates;
- publication history;
- validation through Telegram;
- publication through adapters;
- status tracking;
- duplicate prevention.

Integration priority:

1. Telegram;
2. DEV.to;
3. Bluesky;
4. LinkedIn;
5. Facebook / Instagram / Threads;
6. TikTok / YouTube / Pinterest.

Platforms requiring OAuth, app validation, or advanced permissions will be added progressively.

***

## Telegram

Telegram is the primary control interface in the first version.

Implemented commands:

| Command | Description |
|---|---|
| `/start`, `/help` | Introduce the bot and list commands |
| `/status` | Agent, database, and endpoint health |
| `/ask <mission>` | Submit a mission to the agent |
| `/tasks` | List recent tasks with their states |
| `/cancel <id>` | Cancel a running or pending task |

Planned commands:

```text
/task <id>
/approve <id>
/reject <id>
/memory <query>
/workflows
```

Examples:

```text
/ask Find the best GitHub AI-agent projects published this week.

/tasks

/cancel 7
```

Telegram is also used for results, alerts, errors, confirmation requests, workflow summaries, and daily reports.

Access is restricted: only user IDs listed in `TELEGRAM_ALLOWED_USER_IDS` can command the agent, and every outbound message is routed through a notifier that redacts secrets.

***

## Persistence and memory

SQLite is used by default to avoid PostgreSQL and Redis in the first versions.

```text
storage/
├── agentos.sqlite3
├── logs/
├── files/
├── media/
└── exports/
```

The database currently stores:

- messages (conversation history);
- tasks (status, retries, results);
- incidents (errors and diagnostics);
- confirmations (approval workflow);
- drafts (social drafts, V3);
- memory (long-term knowledge).

Secrets are never stored in plain text in SQLite. They are provided through environment variables or a protected local store. The whole `storage/` directory is git-ignored.

***

## Resilience

AgentOS is designed to run for long periods on an Android VPS and must survive network cuts, API errors, and restarts.

Implemented:

- bounded retries with exponential backoff across LLM endpoints;
- timeouts on HTTP calls and model requests;
- endpoint cooldown after repeated failures;
- failure classification (`retryable`, `auth`, `quota`, `fatal`);
- persistent tasks and results in SQLite;
- clean shutdown on `SIGINT` and `SIGTERM`;
- health checks (`/health`, `/api/status`);
- incident logging with redacted messages.

Planned:

- restart re-queue of `RUNNING` tasks;
- idempotency keys to prevent duplicates;
- stuck-task detection;
- external supervision through the Android VPS engine (`scripts/run.sh`).

***

## Security

Dangerous actions are blocked by default.

| Action | Policy |
|---|---|
| Web search | Allowed |
| Read files in sandbox | Allowed |
| Write files in sandbox | Allowed |
| API / model call | Allowed according to configuration |
| Telegram send | Allowed (authorized users only) |
| Social publishing | Confirmation mandatory |
| Scheduling a post | Confirmation mandatory |
| Data deletion | Confirmation mandatory |
| Arbitrary shell execution | Disabled by default |
| Package installation | Disabled by default |
| Public dashboard exposure | Disabled by default (binds to `127.0.0.1`) |

API keys and tokens must never be printed in logs, Telegram replies, or the local interface — all error paths go through a redaction helper.

Repository hygiene:

- `.env` is git-ignored (never commit real tokens);
- `storage/` is git-ignored (database, logs, files);
- `.encryption-key` files are git-ignored;
- `.env.example` documents every variable with placeholder values only.

***

## Target environment

AgentOS targets the following environment:

```text
Android
└── Android VPS application
    └── Debian bookworm-slim under PRoot
        ├── Python 3.11+
        ├── ARM64
        ├── Python virtual environment
        ├── SQLite
        └── outbound Internet connection
```

Constraints of the first versions:

```text
No Docker
No Node.js
No Redis
No PostgreSQL
No Kubernetes
No mandatory Cloudflare Tunnel
No public port opened by default
```

Cloudflare Tunnel can be added later for a remote dashboard or public webhooks, but it is not required for the agent's outbound calls.

***

## Quick start

```bash
# 1. Create and activate a virtual environment (Python 3.11+)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install pinned dependencies (smolagents is installed editable)
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env               # then edit .env

# 4. Verify the environment
python scripts/check_env.py

# 5. Run the test suite and the linter
python -m pytest                   # 66 tests, no network, no API keys
python -m ruff check . tests scripts

# 6. Start AgentOS
python main.py
```

Once running:

```text
Health:   http://127.0.0.1:8080/health
Status:   http://127.0.0.1:8080/api/status
Telegram: send /status or /ask <mission> to your bot
Stop:     Ctrl+C (SIGINT) or SIGTERM for a clean shutdown
```

### Main environment variables

| Variable | Purpose |
|---|---|
| `LLM_ENDPOINTS` | Ordered JSON list of `{base_url, api_key, model, timeout}` endpoints |
| `LLM_MAX_ATTEMPTS`, `LLM_BACKOFF_BASE`, `LLM_COOLDOWN_SECONDS` | Retry and cooldown policy |
| `LLM_TOOL_CHOICE` | Tool enforcement mode (`required` by default) |
| `AGENT_MAX_STEPS`, `AGENT_MAX_OUTPUT_CHARS` | Agent execution bounds |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ADMIN_CHAT_ID` | Default chat for alerts |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated or JSON list of authorized user IDs |
| `HEALTH_HOST`, `HEALTH_PORT` | Local health endpoint (defaults `127.0.0.1:8080`) |
| `LOG_LEVEL` | Logging verbosity |

***

## Main dependencies

```text
smolagents            agent engine (editable install)
httpx                 HTTP client
ddgs                  web search
python-telegram-bot   Telegram interface
pydantic              validation
pydantic-settings     settings / .env loading
python-dotenv         dotenv support
pytest, ruff          tests and linting
```

Planned for later phases:

```text
APScheduler     scheduling (V2)
aiosqlite       async SQLite access (V2)
tenacity        retry helpers (V2)
```

***

## Project structure

```text
4in1/
├── README.md
├── requirements.in
├── requirements.txt
├── .env.example
├── .gitignore
├── pytest.ini
├── ruff.toml
├── main.py                     entry point, wiring, clean shutdown
│
├── config/
│   ├── settings.py             pydantic settings + .env loading
│   └── logging.py              logging setup with file + console
│
├── agent/
│   ├── core.py                 AgentRunner: submit/status/cancel
│   ├── model_router.py         RoutedModel bridge to smolagents
│   ├── permissions.py          action policies and allow-lists
│   └── tools/                  the registered agent tools
│       ├── web_search.py
│       ├── web_fetch.py
│       ├── files.py
│       ├── memory.py
│       ├── status.py
│       └── base.py
│
├── freellmapi_adapter/
│   ├── provider.py             single endpoint call + retries
│   ├── router.py               cascade, cooldown, endpoint state
│   └── fallback.py             failure classification
│
├── memory/
│   ├── sqlite_store.py         schema + CRUD (6 tables)
│   └── search.py               memory search helpers
│
├── telegram_bot/
│   ├── bot.py                  command handlers + authorization filter
│   └── notifier.py             Telegram and log notifiers
│
├── dashboard/
│   └── app.py                  local health HTTP server
│
├── scripts/
│   ├── check_env.py            environment verification
│   └── run.sh                  process supervision helper
│
├── tests/                      66 unit tests (mocked network)
│
├── storage/                    runtime data (git-ignored)
│
└── <vendored reference repos>  smolagents, freellmapi-python, httpx,
                                ddgs, python-telegram-bot, apscheduler,
                                prefect, brightbean-studio, smolclaw
```

***

## Roadmap

### V1 — Agentic core

- [x] Configuration and secrets management (`.env`, pydantic settings)
- [x] SQLite schema and storage layer
- [x] freellmapi-python integration (OpenAI-compatible adapter + cascade router)
- [x] Agent with smolagents (editable install, `RoutedModel`)
- [x] Web search with DDGS
- [x] HTTP fetch with HTTPX
- [x] Persistent memory
- [x] Telegram bot (`/start`, `/help`, `/status`, `/ask`, `/tasks`, `/cancel`)
- [x] Health check (`/health`, `/api/status`)
- [x] Unit tests (66) and linting (ruff)

### V2 — Durable workflows

- [ ] SQLite task queue with scheduler (APScheduler)
- [ ] Retries and backoff for tasks
- [ ] Recovery after restart
- [ ] Cancellation of scheduled work
- [ ] Incidents and diagnostics endpoints
- [ ] Scheduled Telegram reports
- [ ] `model_generate`, `workflow_*` tools

### V3 — Social management

- [ ] Social drafts
- [ ] Telegram validation flow (`/approve`, `/reject`)
- [ ] Editorial calendar
- [ ] Telegram adapter
- [ ] DEV.to adapter
- [ ] Bluesky adapter
- [ ] LinkedIn adapter
- [ ] Publication history and statuses

### V4 — Local interface

- [ ] Local FastAPI API
- [ ] Localhost dashboard
- [ ] Task list UI
- [ ] Incidents view
- [ ] Memory view
- [ ] Drafts view
- [ ] Model management

### V5 — Optional extensions

- [ ] BrightBean Studio through REST/MCP
- [ ] Prefect for advanced workflows
- [ ] Optional Cloudflare Tunnel
- [ ] Public webhooks
- [ ] Isolated automated browser
- [ ] Advanced social integrations
- [ ] Document RAG
