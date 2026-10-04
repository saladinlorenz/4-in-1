# AgentOS / 4in1

> A lightweight, modular, autonomous personal agent system designed to run continuously on a Debian ARM64 environment under PRoot on Android — without Docker, Node.js, PostgreSQL, Redis, or a mandatory Cloudflare Tunnel.

AgentOS combines an AI agent, persistent tasks, a multi-model layer, Internet tools, a local memory, a social content management module, and a browser-based local dashboard (first-run setup, login, full configuration) into a single Python application.

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
          │ - Social          │    │ - Confirmations       │
          │ - Status          │    │ - Workflows           │
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

The agent does **not** get unlimited access to the system. Tools are controlled and bounded (`AGENT_MAX_STEPS`, `AGENT_MAX_OUTPUT_CHARS`, sandboxed file paths) and exposed through an immutable allow-list of 17 names (`ALLOWED_TOOL_NAMES`).

Execution is abstracted behind an `AgentBackend` registry (`agent/backends.py`): `smolagents` is the active backend; `SmolClaw` is registered with `availability() == (False, reason)` after a real inspection of the bundled `smolclaw-main` (an MIT Bun/TypeScript Telegram bot with an unlimited shell), so it is never selectable nor executable.

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
| `agent/core.py` | `AgentRunner`: submit, status, cancel, confirmations, resume, single active task, listeners |
| `agent/backends.py` | `AgentBackend` registry (`smolagents` active, `SmolClaw` registered unavailable) |
| `agent/model_router.py` | `RoutedModel(smolagents.Model)` bridging to the endpoint router |
| `agent/permissions.py` | Allow-lists and action policies (`ALLOWED_TOOL_NAMES`, dry-run blocked tools) |
| `agent/tools/` | The sixteen registered tools (see below) |

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

Long-running or scheduled tasks are recorded in SQLite and survive a restart. The store implements the task lifecycle (`PENDING`, `RUNNING`, `WAITING_CONFIRMATION`, `SUCCESS`, `FAILED`, `CANCELLED`) plus retry counters, errors, and timestamps. Phase 2 added restart recovery (interrupted tasks are re-queued on boot), confirmation holds, workflow runs with step retries, and scheduled jobs.

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
| `model_generate` | implemented | Direct standalone model completion through the endpoint router |
| `workflow_create` / `workflow_run` / `workflow_status` | implemented | Persistent workflow control (validated registration, idempotent runs) |
| `social_create_draft` | implemented | Create or deduplicate a social draft |
| `social_publish` | implemented | Request publication, approved via Telegram |
| `social_list_drafts` | implemented | List drafts with status and schedule |

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

Implemented:

- draft creation with duplicate prevention;
- editorial scheduling fields (`scheduled_for`) with a calendar-style view in the dashboard;
- publication history and statuses (`DRAFT`, `PUBLISHED`, `FAILED`);
- validation through Telegram (`/approve`, `/reject`);
- publication through connected platform adapters: **Telegram, DEV.to, Bluesky**;
- connection tests per adapter (`POST /api/social/test`) — real API calls, errors returned redacted;
- credentials stored write-only in the local secret store (`DEVTO_API_KEY`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`), masked in the interface (`Configured (ends ...xxxx)`);
- status tracking and redacted failure reporting;
- no simulated publication: no adapter, no publication.

Planned:

- content templates;
- LinkedIn and further adapters.

Integration priority:

1. Telegram — done;
2. DEV.to — done;
3. Bluesky — done;
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
| `/approve <id>` | Approve a pending confirmation (publication, workflow step) |
| `/reject <id>` | Reject a confirmation, cancelling the parked task |

Planned commands:

```text
/task <id>
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

## Local dashboard and Admin API

The whole configuration is operable from a browser at `http://127.0.0.1:8080/` (vanilla HTML/JS SPA, no build step, no Node.js).

First run: `POST /api/setup` creates the admin password (PBKDF2-SHA256, 150 000 iterations, stored in the local secret store — never in SQLite, never returned). Every later visit uses `POST /api/login` → `agentos_session` cookie.

Pages:

| Page | What it does |
|---|---|
| Dashboard | health, task counts, endpoint states |
| Tasks / Incidents / Memory | read-only views over the local API |
| Settings | key/value settings (`SettingsService`, audit-logged) |
| LLM endpoints | CRUD on the ordered endpoint list + test, reload of the router |
| Secrets | list (masked), set, delete, rotate — write-only, `ADMIN_PASSWORD_HASH` excluded |
| Agent | prompt, max steps, temperature, dry-run, tool selection, backend |
| Workflows | definitions, JSON editor + dry-run, runs, cancel, cron schedule/delete |
| Social | drafts, calendar, adapters + connection tests |
| Security | active sessions (revoke), tool allow-list, dry-run blocked tools, audit log |
| Integrations | secrets + real connection tests (Telegram, DDGS, SMTP, GitHub) |

Security model of the Admin API:

- every `POST` requires a valid session cookie **and** an `X-CSRF-Token` header (fail-closed), except `setup`, `login`, `logout`;
- `GET /api/security` additionally requires a valid session (reads are otherwise local-only: the server binds to `127.0.0.1`);
- request bodies are closed schemas (`extra="forbid"`, ≤16 Ko) and unknown fields return `400`;
- all exceptions are redacted (`redact`) before leaving the process; audit trail in the `audit_log` table (`settings.*`, `secret.*`, `llm.*`, `agent.update`, `workflow.*`, `social.test`, `integration.test`, `auth.*`);
- secrets are write-only: responses show only `Configured (ends ...xxxx)`.

Endpoints (summary):

```text
GET  /api/session | /api/settings | /api/llm/endpoints | /api/secrets | /api/audit
     /api/agent | /api/workflows | /api/social | /api/security | /api/integrations
     /api/tasks | /api/incidents | /api/memory | /api/drafts | /api/confirmations
POST /api/setup | /api/login | /api/logout
     /api/settings(/delete) | /api/llm/endpoints(/update|/delete|/test) | /api/llm/reload
     /api/secrets/set | /api/secrets/delete | /api/secrets/rotate
     /api/agent | /api/workflows(/dry_run|/run|/cancel|/schedule|/schedule/delete)
     /api/social/test | /api/integrations/test | /api/security/sessions/revoke
     /api/tasks/<id>/cancel | /api/confirmations/<id>/approve|reject
```

Integration connection tests (`POST /api/integrations/test`) perform genuine read-only handshakes — Telegram `getMe`, GitHub `/user`, SMTP `ehlo`+`login` (no mail sent), one DDGS query — never printing credential values.

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

The database currently stores (13 tables):

- messages (conversation history);
- tasks (status, retries, results);
- incidents (errors and diagnostics);
- confirmations (approval workflow with TTL);
- drafts (social drafts);
- memory (long-term knowledge);
- workflows, workflow_runs, workflow_steps (persistent workflow engine);
- scheduled_jobs (idempotency keys for cron triggers);
- settings (dashboard-editable key/value configuration);
- audit_log (who changed what through the Admin API);
- admin_sessions (hashed session tokens + expiry).

Secrets are never stored in plain text in SQLite. Dashboard secrets live in `storage/.env.runtime` (git-ignored, write-only through the API, masked in responses); the admin password is a PBKDF2 hash. The whole `storage/` directory is git-ignored.

***

## Resilience

AgentOS is designed to run for long periods on an Android VPS and must survive network cuts, API errors, and restarts.

Implemented:

- bounded retries with exponential backoff across LLM endpoints;
- timeouts on HTTP calls and model requests;
- endpoint cooldown after repeated failures;
- failure classification (`retryable`, `auth`, `quota`, `fatal`);
- persistent tasks and results in SQLite;
- restart recovery: interrupted tasks re-queued, half-finished runs reported;
- idempotency keys for workflows and scheduled runs (no duplicates after restart);
- stuck-task supervisor (`STUCK_TASK_HOURS`) with incident logging;
- confirmation TTL (`CONFIRMATION_TTL_HOURS`) with expiry sweep;
- workflow step retries with backoff (`WORKFLOW_RETRY_LIMIT`);
- clean shutdown on `SIGINT` and `SIGTERM`;
- health checks (`/health`, `/api/status`);
- incident logging with redacted messages.

Planned:

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

Admin API controls:

- first-run password setup, PBKDF2-SHA256 (150 000 iterations), login rate-limiting;
- session cookies hashed (SHA-256) with expiry, revocable from the Security page;
- CSRF token required on every state-changing request (fail-closed);
- the agent tool allow-list (`ALLOWED_TOOL_NAMES`) is immutable through the API — a forged `agent.tools` payload is ignored at `effective()`;
- tools flagged as dry-run blocked still refuse irreversible actions even when dry-run mode is off;
- rotating or reading `ADMIN_PASSWORD_HASH` through the API is refused.

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
python -m pytest                   # 170 tests, no network, no API keys
python -m ruff check . tests scripts

# 6. Start AgentOS
python main.py
```

Once running:

```text
Health:    http://127.0.0.1:8080/health
Status:    http://127.0.0.1:8080/api/status
Dashboard: http://127.0.0.1:8080/          (first run: create the admin password)
Local API: http://127.0.0.1:8080/api/tasks|incidents|memory|drafts|confirmations
Telegram:  send /status or /ask <mission> to your bot
Stop:      Ctrl+C (SIGINT) or SIGTERM for a clean shutdown
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
| `WORKFLOW_RETRY_LIMIT`, `WORKFLOW_RETRY_BACKOFF_BASE` | Step retries for workflow runs |
| `SCHEDULER_ENABLED`, `SCHEDULER_TIMEZONE` | Cron triggers on/off and timezone |
| `CONFIRMATION_TTL_HOURS` | Pending confirmations expire after this TTL (default 24) |
| `STUCK_TASK_HOURS` | Supervisor reports tasks running longer than this (default 2) |
| `LOG_LEVEL` | Logging verbosity |

The admin password is **not** an environment variable: it is created at the dashboard's first run and stored as a PBKDF2 hash in `storage/.env.runtime` (git-ignored).

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
APScheduler           cron triggers (execution stays in SQLite)
pytest, ruff          tests and linting
```

Planned for later phases:

```text
aiosqlite       async SQLite access (V2)
tenacity        retry helpers (V2)
```

***

## Project structure

```text
4in1/
├── README.md
├── ARCHITECTURE.md            technical reference (French)
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
│   ├── secrets.py              SecretStore (write-only, masked)
│   └── logging.py              logging setup with file + console (redact filter)
│
├── agent/
│   ├── core.py                 AgentRunner: submit/status/cancel/confirmations
│   ├── backends.py             AgentBackend registry (smolagents / SmolClaw)
│   ├── model_router.py         RoutedModel bridge to smolagents
│   ├── permissions.py          action policies, ALLOWED_TOOL_NAMES (17 names)
│   └── tools/                  the registered agent tools
│       ├── web_search.py  web_fetch.py  files.py  memory.py
│       ├── model.py            model_generate
│       ├── workflows.py        workflow_create / workflow_run / workflow_status
│       ├── social.py           social drafts and publishing flow
│       └── status.py  base.py
│
├── workflows/
│   ├── engine.py               SQLite workflow runs/steps + retries + cancel
│   ├── scheduler.py            APScheduler cron triggers -> enqueue
│   ├── validation.py           declarative workflow/run/schedule schemas
│   └── definitions.py          daily news / daily report workflows
│
├── social/
│   ├── service.py              draft lifecycle, approval-driven publishing
│   └── adapters/               telegram, devto, bluesky + http_json helper
│
├── services/
│   ├── auth.py                 AdminAuth: PBKDF2, sessions, rate-limit
│   ├── settings_service.py     settings table (audit-logged)
│   ├── llm_config.py           endpoint CRUD + write-only keys + router reload
│   ├── agent_config.py         prompt/steps/temp/dry-run/tools applied to runner
│   └── integrations.py         IntegrationTester (telegram/ddgs/smtp/github)
│
├── freellmapi_adapter/
│   ├── provider.py             single endpoint call + retries
│   ├── router.py               cascade, cooldown, endpoint state
│   └── fallback.py             failure classification
│
├── memory/
│   ├── sqlite_store.py         schema + CRUD (13 tables)
│   └── search.py               memory search helpers
│
├── telegram_bot/
│   ├── bot.py                  command handlers + authorization filter
│   └── notifier.py             Telegram and log notifiers
│
├── dashboard/
│   ├── app.py                  health server + Admin API (auth, CSRF, routes)
│   └── static/index.html       dashboard SPA (10 pages, vanilla JS)
│
├── scripts/
│   ├── check_env.py            environment verification
│   └── run.sh                  process supervision helper
│
├── tests/                      170 unit tests (mocked network)
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
- [x] Unit tests and linting (ruff)

### V2 — Durable workflows

- [x] SQLite task queue with scheduler (APScheduler)
- [x] Retries and backoff for tasks (workflow step retries)
- [x] Recovery after restart
- [x] Cancellation of scheduled work
- [x] Incidents and diagnostics endpoints
- [x] Scheduled Telegram reports (daily AI news, daily report)
- [x] `model_generate`, `workflow_*` tools

### V3 — Social management

- [x] Social drafts
- [x] Telegram validation flow (`/approve`, `/reject`)
- [x] Editorial calendar view (scheduling fields + dashboard view)
- [x] Telegram adapter (first real publication platform)
- [x] DEV.to adapter
- [x] Bluesky adapter
- [ ] LinkedIn adapter
- [x] Publication history and statuses

### V4 — Local interface

- [x] Local API routes (tasks, incidents, memory, drafts, confirmations, cancel, decisions)
- [x] Admin API with sessions, CSRF, audit log (stdlib HTTP server, no FastAPI dependency)
- [x] Localhost dashboard (10-page SPA: dashboard, tasks, incidents, memory, drafts, settings, LLM, agent, workflows, social, security, integrations)
- [x] Task list UI
- [x] Incidents view
- [x] Memory view
- [x] Drafts view / editorial calendar
- [x] Model management (endpoint CRUD + test + reload)
- [x] Agent configuration UI (prompt, steps, dry-run, tools, backend)
- [x] Workflow UI (dry-run, runs, cancel, cron schedule)
- [x] Security UI (sessions, tool allow-list, audit, secret rotation)
- [x] Integration connection tests (Telegram, DDGS, SMTP, GitHub)

### V5 — Optional extensions

- [ ] BrightBean Studio through REST/MCP
- [ ] Prefect for advanced workflows
- [ ] Optional Cloudflare Tunnel
- [ ] Public webhooks
- [ ] Isolated automated browser
- [ ] Advanced social integrations
- [ ] Document RAG
