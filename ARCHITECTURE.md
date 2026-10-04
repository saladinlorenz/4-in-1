# AgentOS (4in1) — Comment fonctionne l'application aujourd'hui

> Document technique décrivant l'état **réel** de l'application après la phase « agent-first »
> (interface locale de configuration complète + Admin API protégée + abstraction `AgentBackend`) :
> qui appelle qui, qui dirige qui, ce que chaque flèche renvoie, et où s'arrêtent les responsabilités.
> Toutes les références `file:line` pointent vers le code actuel du dépôt.

***

## 1. En une phrase

`main.py` assemble l'application, **`AgentRunner` la dirige**, `WorkflowEngine` et `AppScheduler` ne font qu'**enfiler des missions** dans lui, `SocialService` ne **publie** qu'après un `/approve`, Telegram et le serveur de santé (Admin API comprise) ne sont que des **entrées/sorties**, `smolagents` est le **cerveau d'exécution** d'une mission (seul `AgentBackend` actif), `freellmapi_adapter` est l'**unique bouche vers les modèles IA**, `Storage` (SQLite) est la **seule mémoire** du système, et les services (`services/`) ne font qu'**administrer** cet état par une API locale verrouillée (session + CSRF + journal d'audit).

***

## 2. Qui dirige qui

| Rang | Composant | Fichier | Rôle exact | Ce qu'il commande |
|---|---|---|---|---|
| 0 | **Utilisateur** | Telegram | Donne les missions, reçoit les résultats | Le bot (uniquement s'il est dans `TELEGRAM_ALLOWED_USER_IDS`) |
| 1 | **Composition root** | `main.py` | Crée et branche tout, gère l'arrêt | Personne : il orchestre le *démarrage*, pas l'exécution |
| 2 | **AgentRunner** ← **le directeur** | `agent/core.py:36` | décide de l'exécution des missions (file unique, statuts, annulation, confirmations, notification) | le `ThreadPoolExecutor`, le `Storage`, le `LLMRouter`, le `notifier`, ses **listeners** |
| 3 | **ToolCallingAgent** | `smolagents` (installe editable) | cerveau d'exécution d'**une** mission : planifie, appelle les outils, boucle jusqu'à la réponse | ses outils + `RoutedModel` |
| 4 | **RoutedModel** | `agent/model_router.py:10` | traducteur smolagents → wire OpenAI | `LLMRouter.chat()` |
| 5 | **LLMRouter** | `freellmapi_adapter/router.py:19` | cascades d'endpoints, cooldown, stats | les endpoints HTTP (`provider.call_endpoint`) |
| 6 | **WorkflowEngine** | `workflows/engine.py` | enfile et suit des runs SQLite (`workflow_runs`/`workflow_steps`), retries, pause sur confirmation | **rien d'autre** : `runner.submit_existing()` + `Storage` |
| 7 | **AppScheduler** | `workflows/scheduler.py` | cron APScheduler = **déclencheurs seulement** : `engine.enqueue()` + maintien en vie | `WorkflowEngine`, `runner.expire_stale_confirmations()` |
| 8 | **SocialService** | `social/service.py` | écoute les décisions (`APPROVED`) et **publie réellement** via un `SocialAdapter` (telegram, devto, bluesky) | `Storage` (drafts), adapters |
| 9 | **AdminAuth** | `services/auth.py` | première installation (`/api/setup`), login rate-limité, sessions hachées (SHA-256) + TTL | `SecretStore` (`ADMIN_PASSWORD_HASH`), table `admin_sessions` |
| 10 | **Services d'administration** | `services/*.py` | `SettingsService` (clé/valeur audité), `LLMConfigService` (CRUD endpoints + `router.reload()`), `AgentConfigService` (appliqué à chaque mission), `IntegrationTester` (tests de connexion réels) | `Storage`, `SecretStore`, `LLMRouter` |
| — | **Storage** | `memory/sqlite_store.py` | mémoire unique, réponse à tous les lecteurs | rien (il est esclave) |
| — | **telegram_bot** | `telegram_bot/bot.py` | **réception** : `runner.submit()`, `/approve`/`/reject` → `runner.resolve_confirmation()` | rien (front-end passif) |
| — | **HealthServer + LocalApi** | `dashboard/app.py` | **observation** (GET) + **Admin API** (POST protégés : session cookie + `X-CSRF-Token`) qui repassent par `AgentRunner`/les services | rien directement : tout via `runner`/`storage`/services |

Règle d'or :
- **Telegram ne parle jamais aux outils ni aux modèles** — il ne fait que `runner.submit()`, `runner.status()`, `storage.list_tasks()`, `runner.cancel()`, `runner.resolve_confirmation()` (`telegram_bot/bot.py`).
- **Nul ne publie sans approval** — `SocialService.on_confirmation` est le **seul** chemin de publication, déclenché uniquement par une décision `APPROVED`.
- **L'agent ne parle jamais directement aux endpoints** — tout passe par `RoutedModel` → `LLMRouter`.
- **Personne n'écrit en SQLite en dehors de `Storage`** — c'est la seule porte d'accès au disque pour l'état (les services passent par lui).
- **Aucun secret ne transite en clair** — `SecretStore` est *write-only* : les réponses n'affichent que `Configured (ends ...xxxx)`, et `ADMIN_PASSWORD_HASH` n'est ni lisible ni modifiable par l'API.
- **La liste blanche d'outils est immuable** — `ALLOWED_TOOL_NAMES` (17 noms) ne peut pas être étendu par l'API : un payload `agent.tools` falsifié est neutralisé au `effective()`.

***

## 3. Schéma des connexions (qui appelle quoi)

```text
        ┌────────────────────────── Telegram ───────────────────────────┐
        │  messages entrants (polling PTB)      réponses / alerts        │
        └───────────────┬───────────────────────────────▲───────────────┘
                        │ Update                        │ reply_text /
                        ▼                               │ send_message
        ┌───────────────────────────────┐   ┌───────────┴─────────────┐
        │ telegram_bot/bot.py           │   │ telegram_bot/notifier.py│
        │  authorized()? ──► handlers   │   │  TelegramNotifier       │
        └───┬───────────┬───────────┬───┘   │  (vers ADMIN_CHAT_ID)   │
            │           │           │       └───────────▲─────────────┘
   submit() │ status()  │ list_tasks│ cancel()          │ send(text)
            ▼           ▼           ▼                   │
        ┌───────────────────────────────────────────────┴───┐
        │              AgentRunner (agent/core.py)          │
        │  submit → create_task(PENDING) → executor(1)      │
        │  _run_task → mark_running → agent.run()           │
        │  finish_task + add_message + _notify              │
        └───┬──────────────────────────────────┬────────────┘
            │ construit                        │ lit/écrit
            ▼                                  ▼
   ┌─────────────────────────┐      ┌────────────────────────────┐
   │ ToolCallingAgent        │      │ Storage (SQLite, WAL)      │
   │  boucle max_steps=12    │      │ tasks / messages /         │
   └───┬────────────────┬────┘      │ incidents / memory /       │
       │ tool_calls     │ generate()│ workflows / runs / steps   │
       ▼                ▼           └────────────▲───────────────┘
┌──────────────┐  ┌──────────────────┐            │
 │ 16 outils    │  │ RoutedModel      │            │ tool calls
│ agent/tools/ │  │ model_router.py  │            │ (remember, files…)
└──────┬───────┘  └────────┬─────────┘            │
       │                   │ chat(payload)        │
       │                   ▼                      │
       │      ┌────────────────────────────────┐  │
       │      │ LLMRouter (freellmapi_adapter) │  │
       │      │  ordre: ready puis cooldown    │  │
       │      └────────────┬───────────────────┘  │
       │                   │ POST base_url/chat/completions
       │                   ▼                      │
       │      ┌────────────────────────────────┐  │
       │      │ Endpoints LLM (LLM_ENDPOINTS)  │  │
       │      │ gateway freellm / Gemini / …   │  │
       │      └────────────────────────────────┘  │
       │                                          │
       └──────────── DDGS / HTTPX / fichiers ──────┘

        ┌─────────────────────────────┐
        │ HealthServer + LocalApi     │  thread daemon, GET /health + /api/*
        │ (dashboard/app.py)          │  GET → services/runner ; POST → session
        │ + SPA dashboard/static/…    │  + CSRF → runner/services (audit partout)
        └─────────────────────────────┘
```

Les flèches signent le contrat :

| Flèche | Ce qui circule |
|---|---|
| Telegram → `bot.py` | `Update` PTB |
| `bot.py` → `AgentRunner.submit()` | `prompt: str`, `chat_id` → retourne **immédiatement** `task_id: int` |
| `AgentRunner` → `Storage` | lignes SQLite (task, message, incident, memory) |
| `AgentRunner` → `ToolCallingAgent.run()` | `prompt` → retourne **à la fin** la réponse finale (`str`) |
| `RoutedModel.generate()` → `LLMRouter.chat()` | dict de paramètres de complétion → retourne le **JSON brut** `{choices:[…], usage:…}` |
| `LLMRouter` → endpoint | `POST {base_url}/chat/completions`, header `Authorization: Bearer` |
| outils → `Storage` / réseau / disque | effets de bord + **chaîne de caractères** de retour (le seul format que smolagents accepte) |
| `AgentRunner._notify()` → `TelegramNotifier.send()` | texte prêt à publier, découpé en blocs de 4000 |
| `bot.py` / `LocalApi` → `runner.resolve_confirmation(id, decision)` | `APPROVED`/`REJECTED` → `(ok, reason)` ; **écouteurs émis avant reprise** |
| `WorkflowEngine` → `runner.submit_existing()` | tâche déjà `PENDING` en base → retour immédiat `task_id` |
| `AppScheduler` (cron) → `engine.enqueue()` | `name`, `idempotency_key` → run `WAITING`/`RUNNING` en SQLite |
| `SocialService.on_confirmation()` → `TelegramAdapter.publish()` | `content` → retourne une **référence réelle** `telegram:<message_id>` (sinon `FAILED`) |
| `HealthServer` → `build_snapshot()` | dict d'état (jamais l'inverse) |

***

## 4. Démarrage : l'ordre exact (`main.py`)

1. `load_settings()` — lit `.env` (pydantic-settings) ; `setup_logging()` installe le filtre `redact` (`config/logging.py`).
2. `Storage(db_path)` + `init_schema()` — crée `storage/agentos.sqlite3` (WAL, busy_timeout 10 s, 13 tables).
3. `LLMRouter(endpoints, attempts, backoff, cooldown)` — prépare le client HTTP partagé.
4. `SecretStore(storage/.env.runtime)` (secrets *write-only*), `SettingsService(storage)`, `AgentConfigService(settings_service, settings)` (prompt/steps/temp/dry-run/tools éditables depuis l'UI).
5. `AgentRunner(settings, storage, router, LogNotifier(), agent_config=…)` — **aucun agent n'existe encore** : il est construit *par mission*.
6. `WorkflowEngine(storage, runner, …)` + `register(DEFAULT_WORKFLOWS)` ; listeners branchés : `runner.add_task_listener(engine.on_task_finished)`, `runner.add_confirmation_listener(engine.on_confirmation_resolved)`, `runner.workflow_engine = engine`.
7. `SocialService(storage, runner)` + adapters **DevtoAdapter** et **BlueskyAdapter** (SecretStore) + `runner.add_confirmation_listener(social.on_confirmation)` — l'adapter **Telegram** n'est ajouté qu'au point 11, si le bot existe.
8. `LLMConfigService(settings_service, secret_store, router)` + `seed(settings.llm_endpoints)` + `reload()` — les endpoints passent de `.env` à SQLite (clés déplacées vers le SecretStore).
9. `AppScheduler(storage, engine, runner, …)` — construit **avant** le serveur de santé (injecté dans `LocalApi`), jobs non encore enregistrés.
10. `AdminAuth(storage, secret_store)` (premier setup, login, sessions) + `IntegrationTester(secret_store, settings)` (tests telegram/ddgs/smtp/github).
11. `HealthServer(..., api=LocalApi(runner, storage, settings_service, secret_store, llm_config, agent_config, workflow_engine, scheduler, social, auth, integrations), auth=admin_auth)` puis handlers `SIGINT`/`SIGTERM` → `stop_event`.
12. Avertissements si `LLM_ENDPOINTS` ou `TELEGRAM_ALLOWED_USER_IDS` vides.
13. `build_application()` + `register_handlers()` ; `runner.notifier` remplacé par `TelegramNotifier`, `social.add_adapter(TelegramAdapter(...))` (sinon : `LogNotifier`, les résultats vont dans les logs — et **aucune publication Telegram** possible).
14. Polling Telegram démarré (long-polling, aucun port ouvert).
15. `health.start()` (thread `daemon`, `HEALTH_HOST:HEALTH_PORT`, défaut `127.0.0.1:8080`).
16. **Reprise** : `runner.resume()` (tasks `RUNNING` → `PENDING`/`CANCELLED`, puis `PENDING` resoumises) puis `engine.resume()` (runs en pause relancés, runs à moitié terminés avancés).
17. `AppScheduler.register_jobs(DEFAULT_SCHEDULED_JOBS)` (08:00 actualité IA, 18:00 rapport) ; démarré seulement si `SCHEDULER_ENABLED`.
18. `run()` attend `stop_event` ; à l'arrêt : `scheduler.stop()` → Telegram stop → `runner.interrupt_current()` → `runner.shutdown(wait=True)` → `health.stop()` → `router.close()` → `storage.close()`.

Point important : **si `TELEGRAM_BOT_TOKEN` est absent, l'application tourne quand même** (agent + santé + workflows + SQLite), sans entrée Telegram ni adapter de publication.

***

## 5. Une mission de bout en bout (`/ask …` ou message texte)

| # | Où | Ce qui se passe | Ce qui est renvoyé / écrit |
|---|---|---|---|
| 1 | `bot.py:117 on_text` / `bot.py:112 cmd_ask` | contrôle `authorized()` (`permissions.py:24`) ; **liste vide = tout refusé** | `None` ou refus silencieux (log `rejected Telegram update`) |
| 2 | `bot.py:109` | `runner.submit(prompt, chat_id)` | — |
| 3 | `core.py:99` | `storage.create_task()` → ligne `PENDING` ; tâche soumise au pool **1 seul worker** | **`task_id` immédiat** |
| 4 | `bot.py:110` | réponse instantanée dans le chat demandeur | `"Task #<id> queued."` |
| 5 | `core.py:242` | `mark_running()` : `PENDING → RUNNING` (reset de `cancel_requested`) | `False` → la tâche est abandonnée (déjà annulée) |
| 6 | `core.py:67` (déf.), `core.py:248` (appel) | `_build_agent()` : `ToolDeps` + `filter_tools()` (liste blanche, `permissions.py:5`) + `RoutedModel(temperature=0.3, tool_choice=required)` + `ToolCallingAgent(max_steps=12)` | un agent **jetable**, propre à cette mission |
| 7 | `core.py:252-253` | si annulation déjà demandée → `agent.interrupt_switch = True` | — |
| 8 | `core.py:254` | `agent.run(prompt)` — **la boucle de décision** (voir §6) | réponse finale (`str`), tronquée à `AGENT_MAX_OUTPUT_CHARS` (4000) |
| 9 | `core.py:259-260` | `add_message("user", prompt)` + `add_message("assistant", text)` | 2 lignes table `messages` |
| 10 | `core.py:261` | `finish_task(SUCCESS, result=text)` + `_emit_task` (listeners workflow) | `True` → notification |
| 11 | `core.py:263` → `notifier.send()` | **notification différée** vers `TELEGRAM_ADMIN_CHAT_ID` (blocs de 4000) | `"Task #<id> completed\n<text>"` |
| 12a | exception non annulée (`core.py:275-278`) | `finish_task(FAILED, error=…)` + `add_incident("agent", …)` | notification `"Task #<id> failed: <detail redigé>"` |
| 12b | exception + `cancel_requested` (`core.py:269-272`) | `finish_task(CANCELLED)` | notification `"Task #<id> cancelled."` |

À retenir :
- **Deux destinations différentes** : la réponse *immédiate* (`queued`) va au **chat qui a demandé** ; le *résultat final* part dans **`TELEGRAM_ADMIN_CHAT_ID`**. Si cet ID vaut 0, `TelegramNotifier.send()` lève `RuntimeError` et le résultat n'arrive que dans les logs (`notifier.py:30-32`, `core.py:330-334`).
- `finish_task` n'accepte que les états `RUNNING`/`PENDING` (`sqlite_store.py:257`) — impossible d'écraser un état terminal.

### Annulation (`/cancel <id>`)

```text
PENDING   → annulée immédiatement            → "cancelled_before_start"
RUNNING   → cancel_requested=1 en base
            + agent.interrupt_switch = True  → smolagents s'arrête à l'étape suivante
            → "interrupt_requested"
terminal  → refus                            → "already_success" etc.
```
(`sqlite_store.py`, `agent/core.py`)

### Confirmation (`/approve <id>` / `/reject <id>`)

```text
outil (social_publish) ──► runner.request_confirmation(task_id, kind, payload)
      │   hold_task: RUNNING → WAITING_CONFIRMATION
      │   INSERT confirmations + notification « /approve <id> »
      ▼   la tâche est GARÉE : le pool est libre, rien ne tourne
(approbation arrive plus tard, par Telegram ou POST local)
      │
      ├─ APPROVED → decide_confirmation + listeners
      │             (engine reprend le run, social PUBLICHE ici)
      │             → release_task(PENDING) + resoumission → la mission reprend
      ├─ REJECTED → listeners (social ne publie PAS)
      │             → release_task(CANCELLED, "confirmation rejected")
      └─ EXPIRED  (TTL CONFIRMATION_TTL_HOURS, sweep APScheduler)
                    → tâche CANCELLED
```

À retenir :
- `request_confirmation` ne fonctionne que sur une tâche `RUNNING` (sinon `None`) : une action sensible ne peut jamais être demandée hors d'une mission vivante.
- Les listeners (publish social, workflow) sont émis **avant** la reprise de la tâche — la publication a lieu pendant que la tâche est encore garée.
- Même chemin depuis Telegram (`runner.resolve_confirmation`) et depuis l'API locale (`LocalApi.decide`) : **une seule porte**.

***

## 6. La boucle interne : `agent.run()` et la chaîne LLM

Pour chaque pas (au plus 12) :

```text
ToolCallingAgent
   │ ① construit les messages (systeme + historique + outils)
   ▼
RoutedModel.generate(messages, tools_to_call_from=…)     agent/model_router.py:22
   │ ② _prepare_completion_kwargs()  (+ tool_choice="required" si des outils sont exposés)
   ▼
LLMRouter.chat(payload)                                   freellmapi_adapter/router.py:44
   │ ③ _ordered() : endpoints en cooldown APRÈS les endpoints prêts
   ▼
pour chaque endpoint :
   call_endpoint()  POST {base_url}/chat/completions     provider.py:24
     ├─ 200 + JSON avec "choices"  → RETURN raw dict      ✔ fin de la chaîne
     ├─ 400 avec tool_choice       → retire tool_choice, réessaie une fois
     ├─ erreur réessayable         → sleep(backoff 1.5^n + jitter, cap 8 s), jusqu'à attempts=2
     └─ sinon                      → LLMError(kind) → cooldown si retryable/AUTH/NOT_FOUND
   │ ④ tous en échec → LLMExhausted(failures)
   ▼
RoutedModel retourne ChatMessage(content, tool_calls, token_usage)   model_router.py:51
   │
   ├─ tool_calls → exécution par le tool correspondant → chaîne reprise au ①
   └─ pas de tool_calls → final_answer → fin de la mission
```

### Classification des erreurs (`fallback.py:54-75`)

| HTTP | `ErrorKind` | Réessai dans l'endpoint | Cooldown (30 s) | Passer à l'endpoint suivant |
|---|---|---|---|---|
| timeout / erreur réseau | `TIMEOUT` / `NETWORK` | oui | oui | oui |
| 429 | `RATE_LIMIT` | oui | oui | oui |
| 5xx | `SERVER` | oui | oui | oui |
| 200 non JSON / sans `choices` | `INVALID_RESPONSE` | oui | oui | oui |
| 401 / 403 | `AUTH` | non | oui | oui |
| 404 | `NOT_FOUND` | non | oui | oui |
| autre 4xx | `CLIENT` | non | non | oui |
| aucun endpoint / config vide | `CONFIG` | — | — | → `LLMExhausted` |

`LLMExhausted` remonte jusqu'à `agent.run()` → exception → tâche `FAILED` + `incidents` + notification (étapes 12a).

Le détail `str(error)` est **redigé** (`redact`, `config/logging.py`) avant tout log, incident ou notification : les clés API n'apparaissent nulle part. Le corps HTTP est de plus tronqué à 400 caractères (`provider.py:17`).

***

## 7. Les 16 outils (+ `final_answer`) — entrées, sorties, effets

| Outil | Entrée | Retour (str, formaté pour le modèle) | Effet de bord |
|---|---|---|---|
| `web_search` | `query`, `max_results` (1-10, déf. 5) | liste numérotée `titre / url / snippet(300)` ou `"No results found."` | appel DDGS sortant (timeout 15 s) |
| `web_fetch` | `url` | texte de la page extrait (HTML→texte), tronqué à `WEB_FETCH_MAX_CHARS` (20 000) | GET sortant, max 1.5 Mo |
| `write_file` | `path` relatif, `content` | `"Wrote N bytes to <path>"` ou refus | écrit dans `storage/files/` (sandbox) |
| `read_file` | `path` relatif | contenu du fichier ou refus | lecture sandbox, max 200 Ko |
| `list_files` | `path` (déf. `.`) | arborescence `chemin (N bytes)`, max 200 entrées | lecture seule |
| `remember` | `text` | `"Saved to memory as #<id>."` | INSERT table `memory` |
| `search_memory` | `query`, `limit` (1-20) | lignes `- [date] texte` ou `"No memories matched."` | SELECT `LIKE` (query entier, sinon OR sur les termes > 2 car.), tri `id DESC` |
| `send_notification` | `message` | `"Notification sent."` / raison d'échec | **envoi Telegram vers l'admin** (le chemin le plus direct vers l'utilisateur) |
| `get_status` | — | JSON indenté de `runner.status()` + `time` | lecture seule |
| `model_generate` | `prompt` (1-20 000 car.) | réponse du modèle ou `Generation failed: …` (redigé) | complétion **directe** via `deps.generate_fn` → `LLMRouter` (hors boucle d'outils) |
| `workflow_create` | `name` (1-64 car. `letters digits _ - .`), `steps` (1 à 20 lignes, ≤4000 car. chacune) | `"Workflow '<n>' saved as #<id> with <k> steps."` ou refus motivé | validation stricte (`parse_workflow`) puis INSERT `workflows` — même chemin que l'API |
| `workflow_run` | `name`, `idempotency_key?` | `"Run #<id> of '<n>' started…"` ou `Workflow '<n>' not found…` | `engine.enqueue()` (clé d'idempotence : jamais deux runs actifs identiques) |
| `workflow_status` | `workflow?`, `limit` | lignes `run #<id> [STATUS] étape k/n …` ou `"No workflow runs found."` | SELECT `workflow_runs` |
| `social_create_draft` | `platform`, `content`, `scheduled_for?` | `"Draft #<id> created for <p> (status DRAFT)."` ou réutilisation si doublon exact | INSERT `drafts` (dédup par `find_draft`) |
| `social_publish` | `draft_id` | `"Confirmation #<id> required … Nothing was published."` / état d'une confirmation existante | **demande une confirmation** ; ne publie **jamais** directement |
| `social_list_drafts` | `status?`, `limit` (1-50) | lignes `#id [STATUS] plateforme date …` ou `"No drafts found."` | SELECT `drafts` |
| `final_answer` | — | (intégré à smolagents) clôt la mission | — |

Tous renvoient des **messages texte en anglais** : c'est la monnaie unique que le modèle comprend. Une erreur n'interrompt jamais la mission, elle est **rapportée dans le retour** pour que l'agent réagisse (sauf erreur LLM fatale).

Le bac à sable (`resolve_in_sandbox`, `files.py:10`) refuse les chemins absolus, les `..` et toute sortie de `storage/files/`.

***

## 8. Que renvoie quoi — tableau récapitulatif

### Commandes Telegram (réponses immédiates, chat demandeur)

| Commande | Source de la donnée | Réponse |
|---|---|---|
| `/start`, `/help` | constante `HELP_TEXT` | liste des commandes |
| `/status` | `runner.status()` → `format_status()` (`bot.py:28`) | `app/version`, `uptime`, `current task`, compteurs `tasks`, `telegram`, et **une ligne par endpoint LLM** avec `state` + `last_error` |
| `/ask …`, texte libre | `runner.submit()` | `Task #<id> queued.` puis **plus rien** (le résultat arrive par notification) |
| `/tasks` | `storage.list_tasks(10)` | 10 lignes `#id [STATUS] date prompt(60)` |
| `/cancel <id>` | `runner.cancel()` | `Cancel task #<id>: accepted/refused (reason)` |
| `/approve <id>` | `runner.resolve_confirmation(id, "APPROVED")` (via `asyncio.to_thread`) | `Confirmation #<id> approved` (+ reprise de la tâche garee) |
| `/reject <id>` | `runner.resolve_confirmation(id, "REJECTED")` | `Confirmation #<id> rejected` (tâche annulée) |

### Notifications proactives (vers `TELEGRAM_ADMIN_CHAT_ID`)

- `Task #<id> completed\n<réponse>` (succès)
- `Task #<id> failed: <erreur redigée ≤1000>`
- `Task #<id> cancelled.`
- `Confirmation #<id> required (<kind>): <payload>` + `Approve with /approve <id>`
- `Confirmation #<id> approved: resuming task #<id>.` / `… rejected: task #<id> cancelled.`
- `Social: draft #<id> published on <platform> — telegram:<message_id>` ou `… publish FAILED: <détail>`
- `Workflow <name> run #<id> …` (résumés de runs) et rapports planifiés (actualités IA 08:00, rapport 18:00)
- tout ce que l'outil `send_notification` décide d'envoyer en cours de mission

### HTTP local (127.0.0.1:8080)

`GET /health` → `200` si la base répond, `503` sinon :

```json
{"status": "ok", "version": "0.1.0", "uptime_seconds": 12.4}
```

`GET /api/status` → instantané complet (`main.py:21-36`) :

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

- `runner.status()` (`agent/core.py`) : `app, version, uptime_seconds, current_task, task_counts, llm_endpoints, telegram_configured`.
- `router.health()` (`router.py`) : par endpoint `state = ready|cooldown`, `cooldown_seconds`, `last_error`, `last_latency_ms`, `success_count`.
- Toute exception dans le snapshot → `503 {"status":"error","detail": <redigé>}`.

### Modèle d'authentification (Admin API)

- **Installation** : `POST /api/setup` n'est accepté **qu'une fois** (sinon `403 already_configured`) — il hache le mot de passe (PBKDF2-SHA256, 150 000 itérations) dans `SecretStore`.
- **Login** : `POST /api/login {password}` → cookie `agentos_session` + `csrf_token` (rate-limité par IP, échecs → `401`).
- **Tous les POST** exigent cookie valide **et** `X-CSRF-Token` (comparaison `hmac`, échec → `403 csrf_failed`), sauf `setup`/`login`/`logout` — *fail-closed* : sans `AdminAuth` câblé, `401`.
- **GET** : lecture locale (surface `127.0.0.1`), sauf `GET /api/security` qui exige une session valide.
- Corps fermés (`extra="forbid"`, ≤16 Ko) : champ inconnu → `400` ; exceptions métier → `400`, service absent → `503`, reste → `503` (détail redigé).

### Routes

| Route | Réponse |
|---|---|
| `GET /health`, `GET /api/status` | santé / instantané (jamais d'auth) |
| `GET /api/session` | `{"authenticated": bool, "csrf_token": …}` (si session) |
| `GET /api/tasks?limit=` | `{"items": [<tasks>]}` (`storage.list_tasks`) |
| `GET /api/incidents?limit=` | `{"items": [<incidents>]}` |
| `GET /api/memory?limit=` | `{"items": [<facts>]}` |
| `GET /api/drafts?limit=&status=` | `{"items": [<drafts>]}` |
| `GET /api/confirmations?limit=&status=` | `{"items": [<confirmations>]}` |
| `GET /api/settings` | réglages clé/valeur audités |
| `GET /api/llm/endpoints` | endpoints **sans clés** (état `Configured (ends …xxxx)`) |
| `GET /api/secrets` | liste des `KNOWN_SECRETS`, état masqué (jamais la valeur) |
| `GET /api/audit?limit=` | journal `audit_log` |
| `GET /api/agent` | config appliquée (prompt, steps, temp, dry-run, outils, backend) |
| `GET /api/workflows` | définitions normalisées + runs + jobs cron |
| `GET /api/social` | drafts + état des adapters (sans clés) |
| `GET /api/security` | sessions actives, `tools = sorted(ALLOWED_TOOL_NAMES)`, dry-run bloqués (**session requise**) |
| `GET /api/integrations` | états telegram/ddgs/smtp/github (masqués, sans réseau) |
| `POST /api/setup`, `/api/login`, `/api/logout` | cycle d'authentification (seules routes POST publiques) |
| `POST /api/settings`, `/api/settings/delete` | écrit `settings` via `SettingsService` + audit |
| `POST /api/llm/endpoints` (+`/update`/`/delete`/`/test`), `/api/llm/reload` | CRUD endpoints, `router.reload()`/`test_endpoint()` |
| `POST /api/secrets/set`/`delete`/`rotate` | écrit `SecretStore` + audit (`ADMIN_PASSWORD_HASH` refusé en rotation) |
| `POST /api/agent` | met à jour la config agent (appliquée à la prochaine mission) |
| `POST /api/workflows` (+`/dry_run`/`/run`/`/cancel`/`/schedule`/`/schedule/delete`) | `WorkflowEngine.register/cancel`, dry-run, `AppScheduler.schedule_set/delete` |
| `POST /api/social/test` | test d'adapter réel (200 `{ok:true,detail}` ou `{ok:false,error}` redigé) |
| `POST /api/integrations/test` | `IntegrationTester.test(kind)` : getMe / DDGS / handshake SMTP / GitHub `/user` |
| `POST /api/security/sessions/revoke` | révoque une session (`401` ensuite) |
| `POST /api/tasks/<id>/cancel` | `{"ok": bool, "reason": str}` → `runner.cancel()` |
| `POST /api/confirmations/<id>/approve\|reject` | `{"ok": bool, "reason": str}` → `runner.resolve_confirmation()` |

Erreurs : `400` identifiant/limit/champ invalide ou schéma ouvert, `401` pas de session, `403` CSRF/`already_configured`, `404` route inconnue, `405` mauvaise méthode sur une route GET, `503` service absent ou exception interne (détail redigé).

***

## 9. Persistance : qui écrit, qui lit

Fichier `storage/agentos.sqlite3` — **créé et protégé uniquement par `Storage`**, WAL actif, connexions **par thread** (`sqlite_store.py:143-153`).

| Table | Écrit par | Lu par | État |
|---|---|---|---|
| `tasks` | `AgentRunner` (create/mark_running/hold/release/finish/cancel) | `/tasks`, `status()`, API `GET /api/tasks` | active |
| `messages` | `AgentRunner` (prompt + réponse) | `recent_messages()` (API V4) | active |
| `incidents` | `AgentRunner`, superviseur « stuck », `WorkflowEngine` | `GET /api/incidents` | active |
| `memory` | outil `remember` | outil `search_memory`, `GET /api/memory` | active |
| `confirmations` | `AgentRunner.request/resolve/expire` | `GET /api/confirmations`, TTL sweep | **câblée** (validation) |
| `drafts` | outil `social_create_draft`, `SocialService` (statuts) | `GET /api/drafts`, `social_list_drafts` | **câblée** (social) |
| `workflows` | `WorkflowEngine.register()` | `engine.enqueue()` | active |
| `workflow_runs` / `workflow_steps` | `WorkflowEngine` (run + étapes, retries) | reprise `engine.resume()`, notifications | active |
| `scheduled_jobs` | `AppScheduler` | idempotency des cron (`name:YYYY-MM-DD`) | active |
| `settings` | `SettingsService` (API dashboard) | `AgentConfigService`, `LLMConfigService`, snapshot | active |
| `audit_log` | chaque action Admin API (`settings.*`, `secret.*`, `llm.*`, `agent.update`, `workflow.*`, `social.test`, `integration.test`, `auth.*`) | page Sécurité (`GET /api/audit`) | active |
| `admin_sessions` | `AdminAuth` (login/révocation, jetons hachés SHA-256) | vérification de chaque requête, purge des expirées | active |

`storage/` est entièrement git-ignoré : aucune donnée ni clé ne part vers GitHub.

***

## 10. Modèle de concurrence

| Zone | Mécanisme | Conséquence |
|---|---|---|
| Boucle principale | `asyncio` (PTB polling + handlers) | les handlers Telegram ne bloquent jamais (`resolve_confirmation` passe par `asyncio.to_thread`) |
| Exécution des missions | `ThreadPoolExecutor(max_workers=1)` (`agent/core.py`) | **une seule mission à la fois**, les autres restent `PENDING` dans la file ; une tâche garée en `WAITING_CONFIRMATION` **libère** le pool |
| SQLite | 1 connexion **par thread** + verrou `Storage._lock` pour la liste | sûr entre thread d'agent, thread santé (`LocalApi`), threads APScheduler et thread asyncio |
| Agent en cours | `AgentRunner._lock` sur `_current` | `/status` et `/cancel` savent toujours quelle tâche tourne |
| Santé HTTP | `ThreadingHTTPServer`, thread `daemon` | GET lecture ; POST appelle directement `runner`/`storage` (thread-safe, notifs via `run_coroutine_threadsafe`) |
| Workflows | `WorkflowEngine._lock` (RLock) + dédup `idempotency_key` | un cron ne lance jamais deux fois le même run, même après un redémarrage |
| APScheduler | `BackgroundScheduler` (threads `daemon`) | **déclencheurs seulement** : ils écrivent en SQLite, l'exécution reste dans le pool du runner |
| Annulation | flag `interrupt_switch` posé **depuis un autre thread** | s'arrête à l'étape suivante, jamais de kill violent |

***

## 11. Points de contrôle sécurité (tous actifs)

1. **Front-door** : `is_authorized_user()` sur **chaque** handler ; liste vide = bot inerte (`telegram_bot/bot.py`).
2. **Liste blanche d'outils** : `filter_tools()` ne laisse passer que les 17 noms de `ALLOWED_TOOL_NAMES` (`permissions.py`) — un outil ajouté ailleurs ne serait pas exposé, et l'API ne peut pas élargir la liste.
3. **Sandbox fichiers** : chemins relatifs uniquement, résolution + contrôle `is_relative_to`, tailles limitées.
4. **Bornes agent** : `AGENT_MAX_STEPS=12`, `AGENT_MAX_OUTPUT_CHARS=4000` — pas de boucle infinie ; config agent éditable mais bornée.
5. **Redaction** : filtre sur tous les logs + sur chaque message d'erreur (`redact`) avant log/incident/notification/réponse HTTP.
6. **Surface réseau** : serveur local sur `127.0.0.1`, bot en **polling sortant** — aucun port public, aucun tunnel requis.
7. **Secrets** : `SecretStore` *write-only* (`storage/.env.runtime`, git-ignoré), jamais dans SQLite ni dans les réponses (masque `Configured (ends ...xxxx)`).
8. **Publication sociale sous confirmation** : `social_publish` ne crée qu'une confirmation ; `SocialService` ne publie qu'après `APPROVED` ; pas d'adapter → `FAILED`, jamais de publication simulée ; `POST /api/social/test` ne teste qu'une connexion en lecture.
9. **Cron idempotents** : `scheduled_jobs` porte la clé `name:YYYY-MM-DD` — un redémarrage dans la journée ne rejoue pas le rapport de 08:00.
10. **Admin auth** : setup en première installation uniquement, PBKDF2-SHA256 150 000 itérations, login rate-limité, sessions hachées (SHA-256) avec TTL et révocation, `ADMIN_PASSWORD_HASH` verrouillé côté API.
11. **CSRF** : `X-CSRF-Token` exigé sur **chaque** POST (comparaison à temps constant) — échec `403`, *fail-closed* si `AdminAuth` absent.
12. **Schémas fermés** : chaque entrée API validée par pydantic (`extra="forbid"`), corps ≤16 Ko, drainé avant réponse (pas de fuite de connexion).
13. **Backends** : `SmolClawBackend` est enregistré avec `availability() == (False, raison)` après inspection du dépôt fourni — jamais sélectionnable, jamais exécuté (le projet livré est un bot Telegram Bun/TypeScript avec shell illimité).
14. **Dry-run** : les outils listés dans `DRY_RUN_BLOCKED` refusent les actions irréversibles même hors mode dry-run.

***

## 12. Arrêt propre (signal)

```text
SIGINT / SIGTERM
  → stop_event (main.py)
  → scheduler.stop()                                 (stoppe les cron APScheduler)
  → updater.stop() / application.stop() / shutdown()   (fin du polling)
  → runner.interrupt_current()                          (coupe l'agent en cours)
  → runner.shutdown(wait=True)                          (vide la file)
  → health.stop()                                       (ferme le port local)
  → router.close()                                      (ferme le client HTTP)
  → storage.close()                                     (ferme les connexions SQLite)
  → "AgentOS stopped"
```

***

## 13. Ce qui existe en structures mais n'est pas encore dirigé

| Élément | Présent | câblé quand |
|---|---|---|
| Table `messages` : lecture API dédiée | oui (écriture partout) | API conversations (non planifiée) |
| Dashboard HTML + Admin API | **oui** (SPA 10 pages, session + CSRF, audit) | — (livré) |
| FastAPI | volontairement **non** (serveur stdlib suffit, aucune dépendance Node) | non prévu |
| Adapters DEV.to / Bluesky | **oui** (testés, write-only) | — (livré) |
| Adapter LinkedIn / OAuth (Facebook, TikTok…) | interface `SocialAdapter` prête | phases sociales suivantes |
| Backend `SmolClawBackend` | enregistré **indisponible** (inspection du dépôt fourni : bot Bun/TS + shell illimité) | adoption d'un backend audité |
| Gateway freellm complète (`freellm/lib` absent) | adaptateur OpenAI-compatible fonctionnel | restauration du lib |

***

## 14. Résumé du flux en trois lignes

1. **Entrée** : un utilisateur autorisé parle à Telegram (ou `POST` local) → `bot.py` crée une tâche `PENDING` et répond `queued` ; les cron ne font qu'enfiler des runs de workflow.
2. **Direction** : `AgentRunner` (fil unique) passe la tâche en `RUNNING`, fabrique un `ToolCallingAgent` qui boucle jusqu'à 12 fois entre **outils** et **`RoutedModel` → `LLMRouter` → endpoints LLM** ; une action sensible **gare** la tâche en `WAITING_CONFIRMATION` jusqu'à `/approve`.
3. **Sortie** : la réponse finit dans `messages` + `tasks.result`, la tâche passe à `SUCCESS`/`FAILED`/`CANCELLED`, une **notification** part vers l'admin Telegram, et une publication sociale n'a lieu qu'après validation ; `/health` et les routes `/api/*` (dashboard local, session + CSRF) racontent et administrent le tout.
