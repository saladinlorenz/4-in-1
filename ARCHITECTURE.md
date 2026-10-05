# AgentOS (4in1) — How the application actually works today

> Technical document describing the **real** state of the application after the "agent-first" phase
> (complete local configuration UI + protected Admin API + `AgentBackend` abstraction):
> who calls whom, who drives whom, what every arrow returns, and where responsibilities stop.
> All `file:line` references point to the current code of the repository.

***

## 1. In one sentence

`main.py` assembles the application, **`AgentRunner` drives it**, `WorkflowEngine` and `AppScheduler` only **enqueue missions** into it, `SocialService` only **publishes** after an `/approve`, Telegram and the health server (Admin API included) are just **inputs/outputs**, `smolagents` is the **execution brain** of a mission (the only active `AgentBackend`), `freellmapi_adapter` is the **only mouth to the AI models**, `Storage` (SQLite) is the system's **only memory**, and the services (`services/`) only **administer** that state through a locked-down local API (session + CSRF + audit trail).

***

## 2. Who drives whom

| Rank | Component | File | Exact role | What it commands |
|---|---|---|---|---|
| 0 | **User** | Telegram | Gives missions, receives results | The bot (only if listed in `TELEGRAM_ALLOWED_USER_IDS`) |
| 1 | **Composition root** | `main.py` | Creates and wires everything, handles shutdown | Nobody: it orchestrates *startup*, not execution |
| 2 | **AgentRunner** ← **the director** | `agent/core.py:40` | decides mission execution (single queue, statuses, cancellation, confirmations, notification) | the `ThreadPoolExecutor`, the `Storage`, the `LLMRouter`, the `notifier`, its **listeners** |
| 3 | **ToolCallingAgent** | `smolagents` (editable install) | execution brain of **one** mission: plans, calls tools, loops until the answer | its tools + `RoutedModel` |
| 4 | **RoutedModel** | `agent/model_router.py:10` | smolagents → OpenAI wire translator | `LLMRouter.chat()` |
| 5 | **LLMRouter** | `freellmapi_adapter/router.py:20` | endpoint cascades, cooldown, stats | the HTTP endpoints (`provider.call_endpoint`) |
| 6 | **WorkflowEngine** | `workflows/engine.py:25` | enqueues and tracks SQLite runs (`workflow_runs`/`workflow_steps`), retries, pauses on confirmation | **nothing else**: `runner.submit_existing()` + `Storage` |
| 7 | **AppScheduler** | `workflows/scheduler.py:13` | cron APScheduler = **triggers only**: `engine.enqueue()` + keep-alive | `WorkflowEngine`, `runner.expire_stale_confirmations()` |
| 8 | **SocialService** | `social/service.py:26` | listens to decisions (`APPROVED`) and **really publishes** through a `SocialAdapter` (telegram, devto, bluesky) | `Storage` (drafts), adapters |
| 9 | **AdminAuth** | `services/auth.py` | first install (`/api/setup`), rate-limited login, hashed sessions (SHA-256) + TTL | `SecretStore` (`ADMIN_PASSWORD_HASH`), table `admin_sessions` |
| 10 | **Administration services** | `services/*.py` | `SettingsService` (audited key/value), `LLMConfigService` (endpoint CRUD + `router.reload()`), `AgentConfigService` (applied to every mission), `IntegrationTester` (real connection tests), settings schema + boot merge (`settings_schema.py`) | `Storage`, `SecretStore`, `LLMRouter` |
| — | **Storage** | `memory/sqlite_store.py` | single memory, answers every reader | nothing (it is a slave) |
| — | **telegram_bot** | `telegram_bot/bot.py` | **reception**: `runner.submit()`, `/approve`/`/reject` → `runner.resolve_confirmation()` | nothing (passive front-end) |
| — | **HealthServer + LocalApi** | `dashboard/app.py` | **observation** (GET) + **Admin API** (protected POST: session cookie + `X-CSRF-Token`) which all go back through `AgentRunner`/the services | nothing directly: everything via `runner`/`storage`/services |

Golden rules:
- **Telegram never talks to tools nor models** — it only does `runner.submit()`, `runner.status()`, `runner.cancel()`, `runner.resolve_confirmation()` and **SQLite reads** (`list_tasks`, `get_task`, `search_memory`, `list_workflows`, `list_runs`, `list_scheduled_jobs`) — `telegram_bot/bot.py`.
- **Nobody publishes without approval** — `SocialService.on_confirmation` is the **only** publication path, triggered solely by an `APPROVED` decision.
- **The agent never talks to the endpoints directly** — everything goes `RoutedModel` → `LLMRouter`.
- **Nobody writes SQLite outside `Storage`** — it is the single disk access door for state (the services go through it).
- **No secret ever travels in clear** — `SecretStore` is *write-only*: responses only show `Configured (ends ...xxxx)`, and `ADMIN_PASSWORD_HASH` is neither readable nor modifiable through the API.
- **The tool allow-list is immutable** — `ALLOWED_TOOL_NAMES` (17 names) cannot be extended by the API: a forged `agent.tools` payload is neutralized at `effective()`.

***

## 3. Connection scheme (who calls what)

```text
        ┌────────────────────────── Telegram ───────────────────────────┐
        │  incoming messages (PTB polling)      replies / alerts        │
        └───────────────┬───────────────────────────────▲───────────────┘
                        │ Update                        │ reply_text /
                        ▼                               │ send_message
        ┌───────────────────────────────┐   ┌───────────┴─────────────┐
        │ telegram_bot/bot.py           │   │ telegram_bot/notifier.py│
        │  authorized()? ──► handlers   │   │  TelegramNotifier       │
        └───┬───────────┬───────────┬───┘   │  (to ADMIN_CHAT_ID)     │
            │           │           │       └───────────▲─────────────┘
   submit() │ status()  │ list_tasks│ cancel()          │ send(text)
            ▼           ▼           ▼                   │
        ┌───────────────────────────────────────────────┴───┐
        │              AgentRunner (agent/core.py)          │
        │  submit → create_task(PENDING) → executor(1)      │
        │  _run_task → mark_running → agent.run()           │
        │  finish_task + add_message + _notify              │
        └───┬──────────────────────────────────┬────────────┘
            │ builds                          │ reads/writes
            ▼                                  ▼
   ┌─────────────────────────┐      ┌────────────────────────────┐
   │ ToolCallingAgent        │      │ Storage (SQLite, WAL)      │
   │  loop max_steps=12      │      │ tasks / messages /         │
   └───┬────────────────┬────┘      │ incidents / memory /       │
       │ tool_calls     │ generate()│ workflows / runs / steps   │
       ▼                ▼           └────────────▲───────────────┘
┌──────────────┐  ┌──────────────────┐            │
 │ 16 tools    │  │ RoutedModel      │            │ tool calls
│ agent/tools/ │  │ model_router.py  │            │ (remember, files…)
└──────┬───────┘  └────────┬─────────┘            │
       │                   │ chat(payload)        │
       │                   ▼                      │
       │      ┌────────────────────────────────┐  │
       │      │ LLMRouter (freellmapi_adapter) │  │
       │      │  order: ready then cooldown    │  │
       │      └────────────┬───────────────────┘  │
       │                   │ POST base_url/chat/completions
       │                   ▼                      │
       │      ┌────────────────────────────────┐  │
       │      │ LLM endpoints (LLM_ENDPOINTS)  │  │
       │      │ freellm gateway / Gemini / …   │  │
       │      └────────────────────────────────┘  │
       │                                          │
       └──────────── DDGS / HTTPX / files ─────────┘

        ┌─────────────────────────────┐
        │ HealthServer + LocalApi     │  daemon thread, GET /health + /api/*
        │ (dashboard/app.py)          │  GET → services/runner ; POST → session
        │ + SPA dashboard/static/…    │  + CSRF → runner/services (audit everywhere)
        └─────────────────────────────┘
```

The arrows sign the contract:

| Arrow | What flows |
|---|---|
| Telegram → `bot.py` | PTB `Update` |
| `bot.py` → `AgentRunner.submit()` | `prompt: str`, `chat_id` → **immediately** returns `task_id: int` |
| `AgentRunner` → `Storage` | SQLite rows (task, message, incident, memory) |
| `AgentRunner` → `ToolCallingAgent.run()` | `prompt` → **at the end** returns the final answer (`str`) |
| `RoutedModel.generate()` → `LLMRouter.chat()` | completion parameter dict → returns the **raw JSON** `{choices:[…], usage:…}` |
| `LLMRouter` → endpoint | `POST {base_url}/chat/completions`, header `Authorization: Bearer` |
| tools → `Storage` / network / disk | side effects + a **string** return (the only format smolagents accepts) |
| `AgentRunner._notify()` → `TelegramNotifier.send()` | ready-to-send text, split into blocks of 4000 |
| `bot.py` / `LocalApi` → `runner.resolve_confirmation(id, decision)` | `APPROVED`/`REJECTED` → `(ok, reason)`; **listeners fire before resumption** |
| `WorkflowEngine` → `runner.submit_existing()` | task already `PENDING` in DB → immediately returns `task_id` |
| `AppScheduler` (cron) → `engine.enqueue()` | `name`, `idempotency_key` → run `WAITING`/`RUNNING` in SQLite |
| `SocialService.on_confirmation()` → `TelegramAdapter.publish()` | `content` → returns a **real reference** `telegram:<message_id>` (otherwise `FAILED`) |
| `HealthServer` → `build_snapshot()` | state dict (never the reverse) |

***

## 4. Startup: the exact order (`main.py`)

`main()`:

1. `load_settings()` — reads `.env` (pydantic-settings) ; `settings.ensure_dirs()` creates `storage/` and `logs/`.
2. Boot `Storage(settings.db_path)` + `init_schema()` — creates `storage/agentos.sqlite3` (WAL, busy_timeout 10 s, 13 tables), then **`apply_db_settings(boot_storage, settings)`** copies every typed schema override stored in the `settings` table onto the `Settings` object (invalid stored values are skipped with a warning, never crash the boot); the boot storage is then closed.
3. `setup_logging(settings.log_level, settings.logs_dir)` — installs the `redact` filter (`config/logging.py`); the log level itself may come from the DB merge. Warnings are logged for applied/skipped overrides.
4. Warnings if `LLM_ENDPOINTS`, `TELEGRAM_ALLOWED_USER_IDS` or `TELEGRAM_BOT_TOKEN` are empty.
5. `asyncio.run(run(settings))`.

`run(settings)`:

6. `settings.ensure_dirs()` + `Storage(settings.db_path)` + `init_schema()` (idempotent — same file as the boot storage).
7. `LLMRouter(endpoints, attempts, backoff, cooldown)` — prepares the shared HTTP client. Attempts/backoff/cooldown come from the (possibly DB-merged) `Settings`.
8. `SecretStore(storage/.env.runtime)` (write-only secrets), `SettingsService(storage)`, `AgentConfigService(settings_service, settings)` (prompt/steps/temp/dry-run/tools editable from the UI).
9. `AgentRunner(settings, storage, router, LogNotifier(), agent_config=…)` — **no agent exists yet**: it is built *per mission*.
10. `WorkflowEngine(storage, runner, …)` + `register(DEFAULT_WORKFLOWS)`; listeners attached: `runner.add_task_listener(engine.on_task_finished)`, `runner.add_confirmation_listener(engine.on_confirmation_resolved)`, `runner.workflow_engine = engine`.
11. `SocialService(storage, runner)` + **DevtoAdapter** and **BlueskyAdapter** (SecretStore) + `runner.add_confirmation_listener(social.on_confirmation)` — the **Telegram** adapter is only added at step 16, if the bot exists.
12. `LLMConfigService(settings_service, secret_store, router)` + `seed(settings.llm_endpoints)` + `reload()` — endpoints move from `.env` to SQLite (keys moved to the SecretStore).
13. `AppScheduler(storage, engine, runner, …)` — built **before** the health server (injected into `LocalApi`), jobs not yet registered.
14. `AdminAuth(storage, secret_store)` (first setup, login, sessions) + `IntegrationTester(secret_store, settings)` (telegram/ddgs/smtp/github tests).
15. `HealthServer(..., api=LocalApi(runner, storage, settings_service, secret_store, llm_config, agent_config, workflow_engine, scheduler, social, auth, integrations, app_settings=settings), auth=admin_auth)` then `SIGINT`/`SIGTERM` handlers → `stop_event`.
16. `build_application()` + `register_handlers()`; `runner.notifier` replaced by `TelegramNotifier`, `social.add_adapter(TelegramAdapter(...))` (otherwise: `LogNotifier`, results go to the logs — and **no Telegram publication** is possible).
17. Telegram polling started (long-polling, no open port).
18. `health.start()` (`daemon` thread, `HEALTH_HOST:HEALTH_PORT`, default `127.0.0.1:8080`).
19. **Resume**: `runner.resume()` (tasks `RUNNING` → `PENDING`/`CANCELLED`, then `PENDING` resubmitted) then `engine.resume()` (paused runs restarted, half-done runs advanced).
20. `AppScheduler.register_jobs(DEFAULT_SCHEDULED_JOBS)` (08:00 AI news, 18:00 report); started only if `SCHEDULER_ENABLED`.
21. `run()` waits on `stop_event`; on shutdown: `scheduler.stop()` → Telegram stop → `runner.interrupt_current()` → `runner.shutdown(wait=True)` → `health.stop()` → `router.close()` → `storage.close()`.

Important point: **if `TELEGRAM_BOT_TOKEN` is missing the application still runs** (agent + health + workflows + SQLite), without a Telegram entry point nor publication adapter.

***

## 5. One mission end to end (`/ask …` or plain text)

| # | Where | What happens | What is returned / written |
|---|---|---|---|
| 1 | `bot.py:194 on_text` / `bot.py:189 cmd_ask` | checks `authorized()` (`bot.py:162`, `permissions.py:28`); **empty list = everything refused** | `None` or silent refusal (log `rejected Telegram update`) |
| 2 | `bot.py` | `runner.submit(prompt, chat_id)` | — |
| 3 | `core.py:175` | `storage.create_task()` → row `PENDING`; task submitted to the **1-worker** pool | **`task_id` immediately** |
| 4 | `bot.py` | instant reply in the requesting chat | `"Task #<id> queued."` |
| 5 | `core.py:353` | `mark_running()`: `PENDING → RUNNING` (resets `cancel_requested`) | `False` → task dropped (already cancelled) |
| 6 | `core.py:138` (def), `_run_task` (call) | `_build_agent()`: `ToolDeps` + `filter_tools()` (allow-list, `permissions.py:35`) + `RoutedModel(temperature=0.3, tool_choice=required)` + `ToolCallingAgent(max_steps=12)` | a **disposable** agent, specific to this mission |
| 7 | `core.py` | if cancellation already requested → `agent.interrupt_switch = True` | — |
| 8 | `core.py:352` | `agent.run(prompt)` — **the decision loop** (see §6) | final answer (`str`), truncated to `AGENT_MAX_OUTPUT_CHARS` (4000) |
| 9 | `core.py` | `add_message("user", prompt)` + `add_message("assistant", text)` | 2 rows in table `messages` |
| 10 | `core.py` | `finish_task(SUCCESS, result=text)` + `_emit_task` (workflow listeners) | `True` → notification |
| 11 | `core.py` → `notifier.send()` | **deferred notification** to `TELEGRAM_ADMIN_CHAT_ID` (blocks of 4000) | `"Task #<id> completed\n<text>"` |
| 12a | uncaught exception (`core.py`) | `finish_task(FAILED, error=…)` + `add_incident("agent", …)` | notification `"Task #<id> failed: <redacted detail>"` |
| 12b | exception + `cancel_requested` (`core.py`) | `finish_task(CANCELLED)` | notification `"Task #<id> cancelled."` |

Keep in mind:
- **Two different destinations**: the *immediate* reply (`queued`) goes to the **chat that asked**; the *final result* goes to **`TELEGRAM_ADMIN_CHAT_ID`**. If that ID is 0, `TelegramNotifier.send()` raises `RuntimeError` and the result only reaches the logs (`notifier.py`, `core.py`).
- `finish_task` only accepts `RUNNING`/`PENDING` states (`sqlite_store.py:280`) — a terminal state can never be overwritten.

The same entry points exist locally: `POST /api/tasks/submit {prompt}` calls the very same `runner.submit()` (same allow-list, same dry-run, same confirmations), and `GET /api/tasks/<id>` returns one task detail (error redacted).

### Cancellation (`/cancel <id>`)

```text
PENDING   → cancelled immediately               → "cancelled_before_start"
RUNNING   → cancel_requested=1 in DB
            + agent.interrupt_switch = True     → smolagents stops at the next step
            → "interrupt_requested"
terminal  → refused                             → "already_success" etc.
```
(`sqlite_store.py`, `agent/core.py`)

### Confirmation (`/approve <id>` / `/reject <id>`)

```text
tool (social_publish) ──► runner.request_confirmation(task_id, kind, payload)
      │   hold_task: RUNNING → WAITING_CONFIRMATION
      │   INSERT confirmations + notification “/approve <id>”
      ▼   the task is PARKED: the pool is free, nothing runs
(approval arrives later, via Telegram or local POST)
      │
      ├─ APPROVED → decide_confirmation + listeners
      │             (engine resumes the run, social PUBLISHES here)
      │             → release_task(PENDING) + resubmit → the mission resumes
      ├─ REJECTED → listeners (social does NOT publish)
      │             → release_task(CANCELLED, "confirmation rejected")
      └─ EXPIRED  (TTL CONFIRMATION_TTL_HOURS, APScheduler sweep)
                    → task CANCELLED
```

Workflow runs use a second door with the same semantics: `WorkflowEngine._hold_for_confirmation` (`workflows/engine.py:317`) parks a run in `WAITING_CONFIRMATION`. **Ordering invariant**: the confirmation row is created **before** the run status flips to `WAITING_CONFIRMATION`, so any reader that observes the parked status is guaranteed to find the pending confirmation (this ordering was fixed after a real race).

Keep in mind:
- `request_confirmation` only works on a `RUNNING` task (otherwise `None`): a sensitive action can never be requested outside a living mission.
- Listeners (social publish, workflow) fire **before** the task resumes — the publication happens while the task is still parked.
- Same path from Telegram (`runner.resolve_confirmation`) and from the local API (`LocalApi.decide`): **one single door**.

***

## 6. The inner loop: `agent.run()` and the LLM chain

For each step (at most 12):

```text
ToolCallingAgent
   │ ① builds the messages (system + history + tools)
   ▼
RoutedModel.generate(messages, tools_to_call_from=…)     agent/model_router.py:22
   │ ② _prepare_completion_kwargs()  (+ tool_choice="required" when tools are exposed)
   ▼
LLMRouter.chat(payload)                                   freellmapi_adapter/router.py:45
   │ ③ _ordered(): endpoints in cooldown AFTER ready endpoints
   ▼
for each endpoint:
   call_endpoint()  POST {base_url}/chat/completions     provider.py:24
     ├─ 200 + JSON with "choices"  → RETURN raw dict      ✔ end of the chain
     ├─ 400 with tool_choice       → drop tool_choice, retry once
     ├─ retryable error            → sleep(backoff 1.5^n + jitter, cap 8 s), up to attempts=2
     └─ otherwise                  → LLMError(kind) → cooldown if retryable/AUTH/NOT_FOUND
   │ ④ all failed → LLMExhausted(failures)
   ▼
RoutedModel returns ChatMessage(content, tool_calls, token_usage)   model_router.py
   │
   ├─ tool_calls → matching tool executes → chain resumes at ①
   └─ no tool_calls → final_answer → end of the mission
```

### Error classification (`fallback.py:54-75`)

| HTTP | `ErrorKind` | Retry in the endpoint | Cooldown (30 s) | Move to the next endpoint |
|---|---|---|---|---|
| timeout / network error | `TIMEOUT` / `NETWORK` | yes | yes | yes |
| 429 | `RATE_LIMIT` | yes | yes | yes |
| 5xx | `SERVER` | yes | yes | yes |
| 200 non-JSON / no `choices` | `INVALID_RESPONSE` | yes | yes | yes |
| 401 / 403 | `AUTH` | no | yes | yes |
| 404 | `NOT_FOUND` | no | yes | yes |
| other 4xx | `CLIENT` | no | no | yes |
| no endpoint / empty config | `CONFIG` | — | — | → `LLMExhausted` |

`LLMExhausted` bubbles up to `agent.run()` → exception → task `FAILED` + `incidents` + notification (step 12a).

The `str(error)` detail is **redacted** (`redact`, `config/logging.py`) before any log, incident or notification: API keys never appear anywhere. The HTTP body is additionally truncated to 400 characters (`provider.py`).

***

## 7. The 16 tools (+ `final_answer`) — inputs, outputs, side effects

| Tool | Input | Return (str, formatted for the model) | Side effect |
|---|---|---|---|
| `web_search` | `query`, `max_results` (1-10, def. 5) | numbered list `title / url / snippet(300)` or `"No results found."` | outgoing DDGS call (timeout 15 s) |
| `web_fetch` | `url` | extracted page text (HTML→text), truncated to `WEB_FETCH_MAX_CHARS` (20 000) | outgoing GET, max 1.5 MB |
| `write_file` | relative `path`, `content` | `"Wrote N bytes to <path>"` or refusal | writes inside `storage/files/` (sandbox) |
| `read_file` | relative `path` | file content or refusal | sandbox read, max 200 KB |
| `list_files` | `path` (def. `.`) | tree `path (N bytes)`, max 200 entries | read-only |
| `remember` | `text` | `"Saved to memory as #<id>."` | INSERT table `memory` |
| `search_memory` | `query`, `limit` (1-20) | lines `- [date] text` or `"No memories matched."` | SELECT `LIKE` (whole query, otherwise OR over terms > 2 chars), ordered `id DESC` |
| `send_notification` | `message` | `"Notification sent."` / failure reason | **Telegram send to the admin** (the most direct path to the user) |
| `get_status` | — | indented JSON of `runner.status()` + `time` | read-only |
| `model_generate` | `prompt` (1-20 000 chars) | model answer or `Generation failed: …` (redacted) | **direct** completion via `deps.generate_fn` → `LLMRouter` (outside the tool loop) |
| `workflow_create` | `name` (1-64 chars `letters digits _ - .`), `steps` (1-20 rows, ≤4000 chars each) | `"Workflow '<n>' saved as #<id> with <k> steps."` or reasoned refusal | strict validation (`parse_workflow`) then INSERT `workflows` — same path as the API |
| `workflow_run` | `name`, `idempotency_key?` | `"Run #<id> of '<n>' started…"` or `Workflow '<n>' not found…` | `engine.enqueue()` (idempotency key: never two identical active runs) |
| `workflow_status` | `workflow?`, `limit` | lines `run #<id> [STATUS] step k/n …` or `"No workflow runs found."` | SELECT `workflow_runs` |
| `social_create_draft` | `platform`, `content`, `scheduled_for?` | `"Draft #<id> created for <p> (status DRAFT)."` or reuse on exact duplicate | INSERT `drafts` (dedup via `find_draft`) |
| `social_publish` | `draft_id` | `"Confirmation #<id> required … Nothing was published."` / state of an existing confirmation | **requests a confirmation**; never publishes directly |
| `social_list_drafts` | `status?`, `limit` (1-50) | lines `#id [STATUS] platform date …` or `"No drafts found."` | SELECT `drafts` |
| `final_answer` | — | (built into smolagents) closes the mission | — |

All of them return **text messages in English**: that is the single currency the model understands. An error never interrupts the mission, it is **reported in the return** so the agent can react (except a fatal LLM error).

The sandbox (`resolve_in_sandbox`, `agent/tools/files.py:10`) refuses absolute paths, `..`, and anything escaping `storage/files/`.

***

## 8. What returns what — summary table

### Telegram commands (instant replies, requesting chat)

| Command | Data source | Reply |
|---|---|---|
| `/start`, `/help` | `HELP_TEXT` constant | command list |
| `/status` | `runner.status()` → `format_status()` (`bot.py:33`) | `app/version`, `uptime`, `current task`, `tasks` counters, `telegram`, and **one line per LLM endpoint** with `state` + `last_error` |
| `/ask …`, plain text | `runner.submit()` | `Task #<id> queued.` then **nothing else** (the result arrives by notification) |
| `/tasks` | `storage.list_tasks(10)` | 10 lines `#id [STATUS] date prompt(60)` |
| `/task <id>` | `storage.get_task(id)` | detail: status, created/started/finished, prompt(300), result(700), **redacted** error(300), `cancel_requested`; `not found` for unknown id |
| `/memory <query>` | `storage.search_memory(query, 5)` | lines `- [date] text(200)` or `No memories matched.` |
| `/workflows` | `storage.list_workflows()` + `list_runs(5)` + `list_scheduled_jobs()` | 3 sections: definitions `name (N steps)`, runs `run #id [STATUS] step=k created=…`, cron `name -> workflow [cron] enabled` (empty sections = `- none`) |
| `/cancel <id>` | `runner.cancel()` | `Cancel task #<id>: accepted/refused (reason)` |
| `/approve <id>` | `runner.resolve_confirmation(id, "APPROVED")` (via `asyncio.to_thread`) | `Confirmation #<id> approved` (+ parked task resumed) |
| `/reject <id>` | `runner.resolve_confirmation(id, "REJECTED")` | `Confirmation #<id> rejected` (task cancelled) |

### Proactive notifications (to `TELEGRAM_ADMIN_CHAT_ID`)

- `Task #<id> completed\n<answer>` (success)
- `Task #<id> failed: <redacted error ≤1000>`
- `Task #<id> cancelled.`
- `Confirmation #<id> required (<kind>): <payload>` + `Approve with /approve <id>`
- `Confirmation #<id> approved: resuming task #<id>.` / `… rejected: task #<id> cancelled.`
- `Social: draft #<id> published on <platform> — telegram:<message_id>` or `… publish FAILED: <detail>`
- `Workflow <name> run #<id> …` (run summaries) and scheduled reports (AI news 08:00, report 18:00)
- whatever the `send_notification` tool decides to send mid-mission

### Local HTTP (127.0.0.1:8080)

`GET /health` → `200` when the DB answers, `503` otherwise:

```json
{"status": "ok", "version": "0.1.0", "uptime_seconds": 12.4}
```

`GET /api/status` → full snapshot (`main.py:30`) :

```json
{
  "status": "ok", "version": "0.1.0", "uptime_seconds": 12.4,
  "db": true,
  "current_task": null,
  "task_counts": {"SUCCESS": 3, "FAILED": 1},
  "llm_endpoints": [
    {"endpoint": "http://127.0.0.1:3001/v1", "base_url": "…", "state": "ready",
     "cooldown_seconds": 0, "last_error": null, "last_latency_ms": 412.0, "success_count": 7}
  ],
  "telegram_configured": true
}
```

- `runner.status()` (`agent/core.py`): `app, version, uptime_seconds, current_task, task_counts, llm_endpoints, telegram_configured`.
- `router.health()` (`router.py`): per endpoint `state = ready|cooldown`, `cooldown_seconds`, `last_error`, `last_latency_ms`, `success_count`.
- Any exception in the snapshot → `503 {"status":"error","detail": <redacted>}`.

### Authentication model (Admin API)

- **Install**: `POST /api/setup` is accepted **only once** (otherwise `403 already_configured`) — it hashes the password (PBKDF2-SHA256, 150 000 iterations) into the `SecretStore`.
- **Login**: `POST /api/login {password}` → cookie `agentos_session` + `csrf_token` (rate-limited per IP, failures → `401`).
- **All POSTs** require a valid cookie **and** `X-CSRF-Token` (`hmac` comparison, failure → `403 csrf_failed`), except `setup`/`login`/`logout` — *fail-closed*: without `AdminAuth` wired, `401`.
- **GET**: a valid session is required on **every** `/api/*` route (no CSRF for reads); `/health` and `/api/status` stay open (redacted supervision surface); foreign `Host` headers are refused (`403 bad_host`, anti DNS-rebinding protection on both GET and POST).
- Closed bodies (`extra="forbid"`, ≤16 KB): unknown field → `400`; business exceptions → `400`, missing service → `503`, the rest → `503` (redacted detail).

### Routes

| Route | Answer |
|---|---|
| `GET /health`, `GET /api/status` | health / snapshot (never authenticated) |
| `GET /api/session` | `{"authenticated": bool, "csrf_token": …}` (if session) |
| `GET /api/tasks?limit=` | `{"items": [<tasks>]}` (`storage.list_tasks`) |
| `GET /api/tasks/<id>` | `{"item": <task>}` — one task detail, error redacted; `400 task not found` |
| `GET /api/incidents?limit=` | `{"items": [<incidents>]}` |
| `GET /api/memory?limit=` | `{"items": [<facts>]}` |
| `GET /api/drafts?limit=&status=` | `{"items": [<drafts>]}` |
| `GET /api/confirmations?limit=&status=` | `{"items": [<confirmations>]}` |
| `GET /api/settings` | audited key/value settings |
| `GET /api/settings/schema` | typed schema driving the Réglages page: category, label, type, range, choices, default, stored value, `override`, `restart` (+ masked status for the `secret` entry) |
| `GET /api/llm/endpoints` | endpoints **without keys** (state `Configured (ends …xxxx)`) |
| `GET /api/secrets` | `KNOWN_SECRETS` list, masked state (never the value) |
| `GET /api/audit?limit=` | `audit_log` journal |
| `GET /api/agent` | applied config (prompt, steps, temp, dry-run, tools, backend) |
| `GET /api/workflows` | normalized definitions + runs + cron jobs |
| `GET /api/social` | drafts + adapter states (no keys) |
| `GET /api/security` | active sessions, `tools = sorted(ALLOWED_TOOL_NAMES)`, dry-run blocked tools |
| `GET /api/integrations` | telegram/ddgs/smtp/github states (masked, no network) |
| `POST /api/setup`, `/api/login`, `/api/logout` | authentication cycle (the only public POST routes) |
| `POST /api/settings`, `/api/settings/delete` | writes `settings` via `SettingsService` + audit; schema keys are **typed-validated** (type/range/choice → `400`, category forced to the spec) |
| `POST /api/llm/endpoints` (+`/update`/`/delete`/`/test`), `/api/llm/reload` | endpoint CRUD, `router.reload()`/`test_endpoint()` |
| `POST /api/secrets/set`/`delete`/`rotate` | writes `SecretStore` + audit (`ADMIN_PASSWORD_HASH` refused on rotation) |
| `POST /api/agent` | updates the agent config (applied to the next mission) |
| `POST /api/workflows` (+`/dry_run`/`/run`/`/cancel`/`/delete`/`/schedule`/`/schedule/delete`) | `WorkflowEngine.register/cancel`, dry-run, `storage.delete_workflow`, `AppScheduler.schedule_set/delete` |
| `POST /api/tasks/submit` | `{prompt}` (≤4000 chars) → `runner.submit()` → `{"ok": true, "task_id": …}` + audit `task.submit` |
| `POST /api/memory/add` / `/api/memory/delete` | `storage.remember(text, source="web")` / `storage.delete_fact(id)` + audit |
| `POST /api/drafts/create` | manual draft creation (`platform` charset-validated, content ≤5000) + audit `draft.create` — publication still requires agent + human confirmation |
| `POST /api/incidents/clear` | `storage.clear_incidents()` → `{"ok": true, "deleted": n}` + audit |
| `POST /api/social/test` | real adapter test (200 `{ok:true,detail}` or `{ok:false,error}` redacted) |
| `POST /api/integrations/test` | `IntegrationTester.test(kind)`: getMe / DDGS / SMTP handshake / GitHub `/user` |
| `POST /api/security/sessions/revoke` | revokes a session (`401` afterwards) |
| `POST /api/tasks/<id>/cancel` | `{"ok": bool, "reason": str}` → `runner.cancel()` |
| `POST /api/confirmations/<id>/approve\|reject` | `{"ok": bool, "reason": str}` → `runner.resolve_confirmation()` |

Errors: `400` invalid identifier/limit/field or open schema, `401` no session, `403` CSRF/`already_configured`/`bad_host` (Host outside `127.0.0.1`/`localhost`/`::1`), `404` unknown route, `405` wrong method on a GET route, `503` missing service or internal exception (redacted detail).

### Settings schema and boot merge

- `services/settings_schema.py` defines `SETTINGS_SPECS` — 26 typed keys across categories `general`, `telegram`, `scheduler`, `limits`, `models`, `workflows`, `health` (LLM retries, web/files limits, confirmation TTL, scheduler timezone, health host/port, log level, `telegram_allowed_user_ids`, …), each with label, help, bounds/choices and a `restart` flag.
- `POST /api/settings` validates schema keys through `validate_setting()` (type coercion, range, choice; `secret`-type keys are refused → use `/api/secrets/set`); unknown keys stay free-form (compatibility with `instance_name`, `agent.tools`, …).
- `apply_db_settings(storage, settings)` runs at boot (`main()`): stored overrides are merged onto the `Settings` object **before** `setup_logging` and any service construction; invalid stored values are skipped with a logged warning.
- The UI shows a per-field `modifié` badge and a banner when overrides exist: everything in the schema applies **at the next restart**, while `agent.*` (AgentConfigService) and LLM endpoints (`router.reload()`) stay live.

***

## 9. Persistence: who writes, who reads

File `storage/agentos.sqlite3` — **created and protected only by `Storage`**, WAL on, **per-thread** connections (`sqlite_store.py:166`).

| Table | Written by | Read by | State |
|---|---|---|---|
| `tasks` | `AgentRunner` (create/mark_running/hold/release/finish/cancel) | `/tasks`, `status()`, API `GET /api/tasks`, `GET /api/tasks/<id>` | active |
| `messages` | `AgentRunner` (prompt + answer) | `recent_messages()` | active |
| `incidents` | `AgentRunner`, "stuck" supervisor, `WorkflowEngine` | `GET /api/incidents`; cleared by `POST /api/incidents/clear` | active |
| `memory` | `remember` tool, API `POST /api/memory/add` | `search_memory` tool, `GET /api/memory`; deleted via `POST /api/memory/delete` | active |
| `confirmations` | `AgentRunner.request/resolve/expire` | `GET /api/confirmations`, TTL sweep | **wired** (validation) |
| `drafts` | `social_create_draft` tool, `SocialService` (statuses), API `POST /api/drafts/create` | `GET /api/drafts`, `social_list_drafts` | **wired** (social) |
| `workflows` | `WorkflowEngine.register()` | `engine.enqueue()`; removed via `POST /api/workflows/delete` (also removes its cron jobs) | active |
| `workflow_runs` / `workflow_steps` | `WorkflowEngine` (run + steps, retries) | `engine.resume()`, notifications | active |
| `scheduled_jobs` | `AppScheduler` | cron idempotency (`name:YYYY-MM-DD`) | active |
| `settings` | `SettingsService` (dashboard API) | `AgentConfigService`, `LLMConfigService`, snapshot, **boot merge `apply_db_settings`** | active |
| `audit_log` | every Admin API action (`settings.*`, `secret.*`, `llm.*`, `agent.update`, `workflow.*`, `task.submit`, `memory.*`, `draft.create`, `incidents.clear`, `social.test`, `integration.test`, `auth.*`) | Security page (`GET /api/audit`) | active |
| `admin_sessions` | `AdminAuth` (login/revocation, hashed tokens) | per-request verification, expired purge | active |

`storage/` is entirely git-ignored: no data nor key ever reaches GitHub.

***

## 10. Concurrency model

| Area | Mechanism | Consequence |
|---|---|---|
| Main loop | `asyncio` (PTB polling + handlers) | Telegram handlers never block (`resolve_confirmation` goes through `asyncio.to_thread`) |
| Mission execution | `ThreadPoolExecutor(max_workers=1)` (`agent/core.py`) | **one mission at a time**, the others stay `PENDING` in the queue; a task parked in `WAITING_CONFIRMATION` **frees** the pool |
| SQLite | 1 connection **per thread** + `Storage._lock` for the list | safe across agent thread, health thread (`LocalApi`), APScheduler threads and the asyncio thread |
| Current agent | `AgentRunner._lock` over `_current` | `/status` and `/cancel` always know which task is running |
| HTTP health | `ThreadingHTTPServer`, `daemon` thread | GET reads; POST calls `runner`/`storage` directly (thread-safe, notifications via `run_coroutine_threadsafe`) |
| Workflows | `WorkflowEngine._lock` (RLock) + `idempotency_key` dedup | a cron never launches the same run twice, even after a restart |
| APScheduler | `BackgroundScheduler` (`daemon` threads) | **triggers only**: they write to SQLite, execution stays in the runner's pool |
| Cancellation | `interrupt_switch` flag set **from another thread** | stops at the next step, never a violent kill |

***

## 11. Security checkpoints (all active)

1. **Front-door**: `is_authorized_user()` on **every** handler; empty list = inert bot (`telegram_bot/bot.py`).
2. **Tool allow-list**: `filter_tools()` only lets through the 17 names of `ALLOWED_TOOL_NAMES` (`permissions.py`) — a tool added elsewhere would not be exposed, and the API cannot widen the list.
3. **File sandbox**: relative paths only, resolution + `is_relative_to` check, bounded sizes.
4. **Agent bounds**: `AGENT_MAX_STEPS=12`, `AGENT_MAX_OUTPUT_CHARS=4000` — no infinite loop; agent config editable but bounded.
5. **Redaction**: filter on all logs + on every error message (`redact`) before log/incident/notification/HTTP response.
6. **Network surface**: local server on `127.0.0.1`, bot in **outbound polling** — no public port, no tunnel required; `Host` accepted only for `127.0.0.1`/`localhost`/`::1` (anti DNS-rebinding), and a session is required on every GET `/api/*`.
7. **Secrets**: write-only `SecretStore` (`storage/.env.runtime`, git-ignored), never in SQLite nor in responses (mask `Configured (ends ...xxxx)`).
8. **Social publication under confirmation**: `social_publish` only creates a confirmation; `SocialService` publishes only after `APPROVED`; no adapter → `FAILED`, never a simulated publication; `POST /api/social/test` only tests a read-only connection.
9. **Idempotent crons**: `scheduled_jobs` carries the key `name:YYYY-MM-DD` — restarting the same day does not replay the 08:00 report.
10. **Admin auth**: setup only on first install, PBKDF2-SHA256 150 000 iterations, rate-limited login, hashed sessions (SHA-256) with TTL and revocation, `ADMIN_PASSWORD_HASH` locked on the API side.
11. **CSRF**: `X-CSRF-Token` required on **every** POST (constant-time comparison) — failure `403`, *fail-closed* when `AdminAuth` is absent.
12. **Closed schemas**: every API entry validated by pydantic (`extra="forbid"`), bodies ≤16 KB, drained before answering (no connection leak).
13. **Backends**: `SmolClawBackend` is registered with `availability() == (False, reason)` after inspecting the provided repo — never selectable, never executed (the delivered project is a Bun/TypeScript Telegram bot with unlimited shell).
14. **Dry-run**: the tools listed in `DRY_RUN_BLOCKED` refuse irreversible actions even outside dry-run mode.
15. **Typed settings**: schema keys are validated on write (type/range/choice) and merged only at boot; secrets referenced by the schema are refused there and can only go through `/api/secrets/set`.

***

## 12. Clean shutdown (signal)

```text
SIGINT / SIGTERM
  → stop_event (main.py)
  → scheduler.stop()                                 (stops APScheduler crons)
  → updater.stop() / application.stop() / shutdown()   (end of polling)
  → runner.interrupt_current()                          (cuts the current agent)
  → runner.shutdown(wait=True)                          (drains the queue)
  → health.stop()                                       (closes the local port)
  → router.close()                                      (closes the HTTP client)
  → storage.close()                                     (closes SQLite connections)
  → "AgentOS stopped"
```

***

## 13. What exists as structure but is not yet driven

| Item | Present | wired when |
|---|---|---|
| `messages` table: dedicated API read | yes (written everywhere) | conversations API (not planned) |
| HTML dashboard + Admin API | **yes** (10-page SPA, session + CSRF, audit, schema-driven settings page, console actions) | — (shipped) |
| FastAPI | deliberately **no** (stdlib server is enough, no Node dependency) | not planned |
| DEV.to / Bluesky adapters | **yes** (tested, write-only) | — (shipped) |
| LinkedIn / OAuth adapters (Facebook, TikTok…) | `SocialAdapter` interface ready | next social phases |
| `SmolClawBackend` | registered **unavailable** (inspected repo: Bun/TS bot + unlimited shell) | adoption of an audited backend |
| Complete freellm gateway (`freellm/lib` absent) | OpenAI-compatible adapter works | restoration of the lib |

***

## 14. The flow in three lines

1. **Input**: an authorized user talks to Telegram (or a local `POST`) → `bot.py` creates a `PENDING` task and answers `queued`; crons only enqueue workflow runs.
2. **Direction**: `AgentRunner` (single worker) flips the task to `RUNNING`, builds a `ToolCallingAgent` that loops up to 12 times between **tools** and **`RoutedModel` → `LLMRouter` → LLM endpoints**; a sensitive action **parks** the task in `WAITING_CONFIRMATION` until `/approve`.
3. **Output**: the answer lands in `messages` + `tasks.result`, the task becomes `SUCCESS`/`FAILED`/`CANCELLED`, a **notification** goes to the Telegram admin, social publication only happens after validation; `/health` and the `/api/*` routes (local dashboard, session + CSRF) narrate and administer all of it.
