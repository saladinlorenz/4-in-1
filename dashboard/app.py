from __future__ import annotations

import hmac
import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent.permissions import ALLOWED_TOOL_NAMES
from config.logging import redact
from services import describe_schema, validate_setting
from workflows.validation import NAME_RE, parse_run, parse_schedule, parse_workflow

logger = logging.getLogger(__name__)

INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"

MAX_BODY_BYTES = 16_384
SESSION_COOKIE = "agentos_session"

GET_ROUTES = {
    "/health",
    "/api/status",
    "/api/tasks",
    "/api/incidents",
    "/api/memory",
    "/api/drafts",
    "/api/confirmations",
    "/api/settings",
    "/api/settings/schema",
    "/api/llm/endpoints",
    "/api/secrets",
    "/api/audit",
    "/api/agent",
    "/api/workflows",
    "/api/social",
    "/api/security",
    "/api/integrations",
}

# GET routes that expose state: a valid session cookie is required (no CSRF
# needed for reads). /health and /api/status stay open as the supervision
# surface (redacted, minimal).
PROTECTED_GET = GET_ROUTES - {"/health", "/api/status"}

DECISIONS = {"approve": "APPROVED", "reject": "REJECTED"}

KNOWN_SECRETS = [
    "ADMIN_PASSWORD_HASH",
    "BLUESKY_APP_PASSWORD",
    "BLUESKY_HANDLE",
    "DEVTO_API_KEY",
    "GITHUB_TOKEN",
    "NOTION_TOKEN",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "SMTP_PORT",
    "SMTP_USER",
    "TELEGRAM_BOT_TOKEN",
    "WEBHOOK_SECRET",
]

POST_ACTIONS = {
    "/api/tasks/submit": "task_submit",
    "/api/memory/add": "memory_add",
    "/api/memory/delete": "memory_delete",
    "/api/drafts/create": "draft_create",
    "/api/incidents/clear": "incidents_clear",
    "/api/settings": "settings_set",
    "/api/settings/delete": "settings_delete",
    "/api/llm/endpoints": "llm_create",
    "/api/llm/endpoints/update": "llm_update",
    "/api/llm/endpoints/delete": "llm_delete",
    "/api/llm/endpoints/test": "llm_test",
    "/api/llm/reload": "llm_reload",
    "/api/secrets/set": "secret_set",
    "/api/secrets/delete": "secret_delete",
    "/api/secrets/rotate": "secret_rotate",
    "/api/agent": "agent_update",
    "/api/workflows": "workflow_create",
    "/api/workflows/dry_run": "workflow_dry_run",
    "/api/workflows/run": "workflow_run",
    "/api/workflows/cancel": "workflow_cancel",
    "/api/workflows/delete": "workflow_delete",
    "/api/workflows/schedule": "workflow_schedule",
    "/api/workflows/schedule/delete": "workflow_schedule_delete",
    "/api/social/test": "social_test",
    "/api/security/sessions/revoke": "session_revoke",
    "/api/integrations/test": "integration_test",
}


class Unavailable(Exception):
    """Requested service not wired in this process (503)."""


def _query_limit(query: dict[str, list[str]], default: int = 10) -> int:
    raw = (query.get("limit") or [""])[0]
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("limit must be an integer") from None
    if value < 1:
        raise ValueError("limit must be positive")
    return min(100, value)


def _query_status(query: dict[str, list[str]]) -> str | None:
    raw = (query.get("status") or [""])[0].strip().upper()
    return raw or None


def _path_id(segments: list[str], index: int) -> int:
    try:
        value = int(segments[index])
    except ValueError:
        raise ValueError("identifier must be an integer") from None
    if value < 1:
        raise ValueError("identifier must be positive")
    return value


def _body_id(body: dict[str, Any], field: str) -> int:
    try:
        value = int(body.get(field))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None
    if value < 1:
        raise ValueError(f"{field} must be positive")
    return value


class LocalApi:
    """Read/write controller behind the local HTTP API.

    Bound to 127.0.0.1 only: it exposes task, confirmation and draft state
    to the local dashboard. Mutating calls (cancel, approve, reject) go
    through the same runner code paths as the Telegram commands.
    """

    def __init__(
        self,
        runner: Any,
        storage: Any,
        *,
        settings_service: Any | None = None,
        secret_store: Any | None = None,
        llm_config: Any | None = None,
        agent_config: Any | None = None,
        workflow_engine: Any | None = None,
        scheduler: Any | None = None,
        social: Any | None = None,
        auth: Any | None = None,
        integrations: Any | None = None,
        app_settings: Any | None = None,
    ) -> None:
        self.runner = runner
        self.storage = storage
        self.settings_service = settings_service
        self.secret_store = secret_store
        self.llm_config = llm_config
        self.agent_config = agent_config
        self.workflow_engine = workflow_engine
        self.scheduler = scheduler
        self.social = social
        self.auth = auth
        self.integrations = integrations
        self.app_settings = app_settings

    def _require(self, service: Any) -> Any:
        if service is None:
            raise Unavailable("service not configured")
        return service

    # --- GET -------------------------------------------------------------

    def tasks(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {"items": self.storage.list_tasks(limit=_query_limit(query))}

    def incidents(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {"items": self.storage.recent_incidents(limit=_query_limit(query))}

    def memory(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {"items": self.storage.recent_facts(limit=_query_limit(query))}

    def drafts(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "items": self.storage.list_drafts(
                limit=_query_limit(query), status=_query_status(query)
            )
        }

    def confirmations(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "items": self.storage.list_confirmations(
                limit=_query_limit(query), status=_query_status(query)
            )
        }

    def settings_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        service = self._require(self.settings_service)
        category = (query.get("category") or [""])[0].strip() or None
        return {"items": service.list(category)}

    def settings_schema(self, query: dict[str, list[str]]) -> dict[str, Any]:
        service = self._require(self.settings_service)
        app_settings = self._require(self.app_settings)
        rows = describe_schema(service, app_settings)
        store = self.secret_store
        for row in rows:
            if row["secret"] and store is not None:
                row["status"] = store.mask(row["key"]) or "absent"
        return {"items": rows}

    def audit(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {"items": self.storage.recent_audit(limit=_query_limit(query, 25))}

    def agent_info(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self._require(self.agent_config).describe()

    def llm_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        service = self._require(self.llm_config)
        return {"items": service.list(), "health": service.health()}

    def secrets_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        store = self._require(self.secret_store)
        known = sorted(set(KNOWN_SECRETS) | set(store.names()))
        return {
            "items": [
                {"key": name, "status": store.mask(name) or "absent"}
                for name in known
            ]
        }

    def workflows_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        self._require(self.workflow_engine)
        items = []
        for row in self.storage.list_workflows():
            try:
                steps = json.loads(row["steps"])
            except ValueError:
                steps = []
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "created_at": row.get("created_at"),
                    "steps": steps if isinstance(steps, list) else [],
                    "step_count": len(steps) if isinstance(steps, list) else 0,
                }
            )
        jobs = []
        for job in self.storage.list_scheduled_jobs():
            try:
                cron = json.loads(job["cron"])
            except ValueError:
                cron = {}
            jobs.append(
                {
                    "name": job["name"],
                    "workflow_name": job["workflow_name"],
                    "hour": cron.get("hour"),
                    "minute": cron.get("minute"),
                    "enabled": bool(job["enabled"]),
                    "last_enqueued_at": job.get("last_enqueued_at"),
                }
            )
        return {
            "items": items,
            "runs": self.storage.list_runs(
                limit=_query_limit(query), status=_query_status(query)
            ),
            "jobs": jobs,
        }

    def social_info(self, query: dict[str, list[str]]) -> dict[str, Any]:
        service = self._require(self.social)
        return {
            "adapters": service.describe(),
            "drafts": self.storage.list_drafts(limit=_query_limit(query)),
        }

    def security_info(self, query: dict[str, list[str]]) -> dict[str, Any]:
        auth = self._require(self.auth)
        dry_run_blocked: list[str] = []
        if self.agent_config is not None:
            dry_run_blocked = list(self.agent_config.describe().get("dry_run_blocked", []))
        return {
            "sessions": auth.list_sessions(),
            "tools": sorted(ALLOWED_TOOL_NAMES),
            "dry_run_blocked": dry_run_blocked,
        }

    def integrations_info(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {"items": self._require(self.integrations).describe()}

    # --- POST ------------------------------------------------------------

    def cancel_task(self, task_id: int) -> tuple[bool, str]:
        return self.runner.cancel(task_id)

    def decide(self, confirmation_id: int, decision: str) -> tuple[bool, str]:
        return self.runner.resolve_confirmation(confirmation_id, decision)

    def settings_set(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._require(self.settings_service)
        key = str(body.get("key", ""))
        value = body.get("value")
        category = str(body.get("category", "general"))
        checked = validate_setting(key, value)
        if checked is not None:
            value, category = checked
        service.set(key, value, category=category)
        return {"ok": True}

    def settings_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._require(self.settings_service)
        return {"ok": service.delete(str(body.get("key", "")))}

    def llm_create(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "endpoint": self._require(self.llm_config).create(body)}

    def llm_update(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint_id = _body_id(body, "id")
        config = self._require(self.llm_config)
        return {"ok": True, "endpoint": config.update(endpoint_id, body)}

    def llm_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint_id = _body_id(body, "id")
        return {"ok": self._require(self.llm_config).delete(endpoint_id)}

    def llm_test(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint_id = _body_id(body, "id")
        return self._require(self.llm_config).test(endpoint_id)

    def llm_reload(self, body: dict[str, Any]) -> dict[str, Any]:
        count = self._require(self.llm_config).reload()
        return {"ok": True, "count": count}

    def secret_set(self, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require(self.secret_store)
        store.set(str(body.get("key", "")), str(body.get("value", "")))
        self.storage.add_audit("dashboard", "secret.set", f"key={body.get('key', '')}")
        return {"ok": True}

    def secret_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require(self.secret_store)
        removed = store.delete(str(body.get("key", "")))
        if removed:
            self.storage.add_audit(
                "dashboard", "secret.delete", f"key={body.get('key', '')}"
            )
        return {"ok": removed}

    def agent_update(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self._require(self.agent_config)
        return {"ok": True, "settings": settings.update(body)}

    # --- workflows (declarative, closed schema) --------------------------

    def workflow_create(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._require(self.workflow_engine)
        name, steps = parse_workflow(body)
        workflow_id = engine.register(name, steps)
        self.storage.add_audit(
            "dashboard", "workflow.create", f"name={name} steps={len(steps)}"
        )
        return {"ok": True, "id": workflow_id, "name": name, "step_count": len(steps)}

    def workflow_dry_run(self, body: dict[str, Any]) -> dict[str, Any]:
        name, steps = parse_workflow(body)
        return {
            "ok": True,
            "name": name,
            "steps": steps,
            "step_count": len(steps),
            "persisted": False,
        }

    def workflow_run(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._require(self.workflow_engine)
        request = parse_run(body)
        run_id = engine.enqueue(
            request["name"],
            context=request["context"],
            idempotency_key=request["idempotency_key"],
        )
        if run_id is None:
            raise ValueError(f"unknown workflow '{request['name']}'")
        self.storage.add_audit(
            "dashboard", "workflow.run", f"name={request['name']} run={run_id}"
        )
        return {"ok": True, "run_id": run_id}

    def workflow_cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._require(self.workflow_engine)
        run_id = _body_id(body, "run_id")
        ok, reason = engine.cancel(run_id)
        if ok:
            self.storage.add_audit("dashboard", "workflow.cancel", f"run={run_id}")
        return {"ok": ok, "reason": reason}

    def workflow_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, {"name"})
        name = self._text(body, "name", limit=64)
        ok = self.storage.delete_workflow(name)
        if ok:
            self.storage.add_audit("dashboard", "workflow.delete", f"name={name}")
        return {"ok": ok}

    def workflow_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        scheduler = self._require(self.scheduler)
        request = parse_schedule(body)
        job_id = scheduler.schedule_set(
            request["name"],
            request["workflow_name"],
            request["hour"],
            request["minute"],
            enabled=request["enabled"],
        )
        self.storage.add_audit(
            "dashboard",
            "workflow.schedule",
            f"name={request['name']} at {request['hour']:02d}:{request['minute']:02d}",
        )
        return {"ok": True, "id": job_id, "name": request["name"]}

    def workflow_schedule_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        scheduler = self._require(self.scheduler)
        name = str(body.get("name", "")).strip()
        if not NAME_RE.fullmatch(name):
            raise ValueError("job name must be 1-64 chars (letters, digits, _ - .)")
        removed = scheduler.schedule_delete(name)
        if removed:
            self.storage.add_audit("dashboard", "workflow.unschedule", f"name={name}")
        return {"ok": removed}

    # --- social (connection tests only, never a publication path) --------

    def social_test(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._require(self.social)
        extra = set(body) - {"platform"}
        if extra:
            raise ValueError(f"unknown fields: {', '.join(sorted(extra))}")
        platform = str(body.get("platform", "")).strip().lower()
        if not platform:
            raise ValueError("platform is required")
        if platform not in service.adapters:
            raise ValueError(f"unknown platform '{platform}'")
        try:
            detail = service.test(platform)
        except ValueError:
            raise
        except Exception as exc:
            return {"ok": False, "error": redact(str(exc))[:300]}
        self.storage.add_audit("dashboard", "social.test", f"platform={platform}")
        return {"ok": True, "detail": detail}

    # --- security administration -----------------------------------------

    def session_revoke(self, body: dict[str, Any]) -> dict[str, Any]:
        auth = self._require(self.auth)
        session_id = _body_id(body, "id")
        return {"ok": auth.revoke_session(session_id)}

    def secret_rotate(self, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require(self.secret_store)
        key = str(body.get("key", ""))
        if key == "ADMIN_PASSWORD_HASH":
            raise ValueError("ADMIN_PASSWORD_HASH cannot be rotated through the API")
        store.set(key, str(body.get("value", "")))
        self.storage.add_audit("dashboard", "secret.rotate", f"key={key}")
        return {"ok": True}

    # --- integration connection tests ------------------------------------

    def integration_test(self, body: dict[str, Any]) -> dict[str, Any]:
        tester = self._require(self.integrations)
        extra = set(body) - {"kind"}
        if extra:
            raise ValueError(f"unknown fields: {', '.join(sorted(extra))}")
        kind = str(body.get("kind", "")).strip().lower()
        if not kind:
            raise ValueError("kind is required")
        try:
            detail = tester.test(kind)
        except ValueError:
            raise
        except Exception as exc:
            return {"ok": False, "error": redact(str(exc))[:300]}
        self.storage.add_audit("dashboard", "integration.test", f"kind={kind}")
        return {"ok": True, "detail": detail}

    # --- console actions ----------------------------------------------------

    @staticmethod
    def _closed(body: dict[str, Any], allowed: set[str]) -> None:
        extra = set(body) - allowed
        if extra:
            raise ValueError(f"unknown fields: {', '.join(sorted(extra))}")

    @staticmethod
    def _text(body: dict[str, Any], field: str, *, limit: int) -> str:
        value = str(body.get(field, "")).strip()
        if not value:
            raise ValueError(f"{field} is required")
        if len(value) > limit:
            raise ValueError(f"{field} must be at most {limit} chars")
        return value

    def task_submit(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, {"prompt"})
        prompt = self._text(body, "prompt", limit=4000)
        task_id = self.runner.submit(prompt, kind="chat")
        self.storage.add_audit("dashboard", "task.submit", f"task_id={task_id}")
        return {"ok": True, "task_id": task_id}

    def task_detail(self, task_id: int) -> dict[str, Any]:
        task = self.storage.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        if task.get("error"):
            task["error"] = redact(str(task["error"]))[:500]
        return {"item": task}

    def memory_add(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, {"text"})
        text = self._text(body, "text", limit=2000)
        fact_id = self.storage.remember(text, source="web")
        self.storage.add_audit("dashboard", "memory.add", f"fact_id={fact_id}")
        return {"ok": True, "id": fact_id}

    def memory_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, {"id"})
        fact_id = _body_id(body, "id")
        ok = self.storage.delete_fact(fact_id)
        if ok:
            self.storage.add_audit("dashboard", "memory.delete", f"fact_id={fact_id}")
        return {"ok": ok}

    def draft_create(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, {"platform", "content", "scheduled_for"})
        platform = self._text(body, "platform", limit=32).lower()
        if not all(char.isalnum() or char in "-_" for char in platform):
            raise ValueError("platform must contain only letters, digits, - or _")
        content = self._text(body, "content", limit=5000)
        scheduled = body.get("scheduled_for")
        if scheduled is not None:
            scheduled = str(scheduled).strip() or None
        if scheduled is not None and len(scheduled) > 64:
            raise ValueError("scheduled_for must be at most 64 chars")
        draft_id = self.storage.create_draft(platform, content, scheduled)
        self.storage.add_audit("dashboard", "draft.create", f"draft_id={draft_id}")
        return {"ok": True, "id": draft_id}

    def incidents_clear(self, body: dict[str, Any]) -> dict[str, Any]:
        self._closed(body, set())
        deleted = self.storage.clear_incidents()
        self.storage.add_audit("dashboard", "incidents.clear", f"deleted={deleted}")
        return {"ok": True, "deleted": deleted}


def _make_handler(
    snapshot: Callable[[], dict[str, Any]],
    api: LocalApi | None,
    auth: Any | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentos"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if not self._host_allowed():
                self._send(403, {"error": "bad_host"})
                return
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path == "/health":
                self._send(*self._health())
                return
            if path == "/api/session":
                self._handle_session()
                return
            if path == "/api/status":
                try:
                    self._send(200, snapshot())
                except Exception as exc:
                    self._send(503, {"status": "error", "error": redact(str(exc))[:300]})
                return
            if api is None:
                self._send(404, {"error": "not_found"})
                return
            if path in PROTECTED_GET or path.startswith("/api/tasks/"):
                session = None if auth is None else auth.session_valid(self._session_token())
                if session is None:
                    self._send(401, {"error": "unauthorized"})
                    return
            segments = [part for part in path.split("/") if part]
            if len(segments) == 3 and segments[0] == "api" and segments[1] == "tasks":
                try:
                    task_id = _path_id(segments, 2)
                    payload = api.task_detail(task_id)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)[:300]})
                except Exception as exc:
                    logger.warning("GET %s failed: %s", path, redact(str(exc)))
                    self._send(503, {"error": "unavailable"})
                else:
                    self._send(200, payload)
                return
            routes = {
                "/api/tasks": api.tasks,
                "/api/incidents": api.incidents,
                "/api/memory": api.memory,
                "/api/drafts": api.drafts,
                "/api/confirmations": api.confirmations,
                "/api/settings": api.settings_list,
                "/api/settings/schema": api.settings_schema,
                "/api/llm/endpoints": api.llm_list,
                "/api/secrets": api.secrets_list,
                "/api/audit": api.audit,
                "/api/agent": api.agent_info,
                "/api/workflows": api.workflows_list,
                "/api/social": api.social_info,
                "/api/security": api.security_info,
                "/api/integrations": api.integrations_info,
            }
            if path not in routes:
                self._send(404, {"error": "not_found"})
                return
            try:
                payload = routes[path](parse_qs(parsed.query))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                logger.warning("GET %s failed: %s", path, redact(str(exc)))
                self._send(503, {"error": "unavailable"})
            else:
                self._send(200, payload)

        def _serve_index(self) -> None:
            try:
                content = INDEX_PATH.read_bytes()
            except OSError:
                self._send(404, {"error": "not_found"})
                return
            self._drain_body()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)

        def _handle_session(self) -> None:
            if auth is None:
                self._send(503, {"error": "unavailable"})
                return
            session = auth.session_valid(self._session_token())
            if session is None:
                self._send(
                    401,
                    {
                        "error": "unauthorized",
                        "setup_required": not auth.password_configured(),
                    },
                )
                return
            self._send(
                200,
                {"authenticated": True, "csrf_token": str(session["csrf_token"])},
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if not self._host_allowed():
                self._send(403, {"error": "bad_host"})
                return
            if api is None:
                self._send(404, {"error": "not_found"})
                return
            if path == "/api/setup":
                self._handle_setup()
                return
            if path == "/api/login":
                self._handle_login()
                return
            if path == "/api/logout":
                self._handle_logout()
                return
            gate = self._require_session()
            if gate is not None:
                self._send(*gate)
                return
            action = POST_ACTIONS.get(path)
            if action is not None:
                body = self._read_json()
                if body is None:
                    return
                handler = getattr(api, action, None)
                if handler is None:
                    self._send(503, {"error": "unavailable"})
                    return
                try:
                    payload = handler(body)
                except Unavailable:
                    self._send(503, {"error": "unavailable"})
                except ValueError as exc:
                    self._send(400, {"error": str(exc)[:300]})
                except Exception as exc:
                    logger.warning("POST %s failed: %s", path, redact(str(exc)))
                    self._send(503, {"error": "unavailable"})
                else:
                    self._send(200, payload)
                return
            segments = [part for part in path.split("/") if part]
            try:
                if (
                    len(segments) == 4
                    and segments[0] == "api"
                    and segments[1] == "tasks"
                    and segments[3] == "cancel"
                ):
                    task_id = _path_id(segments, 2)
                    ok, reason = api.cancel_task(task_id)
                    self._send(200, {"ok": ok, "reason": reason})
                    return
                if (
                    len(segments) == 4
                    and segments[0] == "api"
                    and segments[1] == "confirmations"
                    and segments[3] in DECISIONS
                ):
                    confirmation_id = _path_id(segments, 2)
                    ok, reason = api.decide(confirmation_id, DECISIONS[segments[3]])
                    self._send(200, {"ok": ok, "reason": reason})
                    return
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            except Exception as exc:
                logger.warning("POST %s failed: %s", path, redact(str(exc)))
                self._send(503, {"error": "unavailable"})
                return
            if path in GET_ROUTES:
                self._send(405, {"error": "method_not_allowed"})
                return
            self._send(404, {"error": "not_found"})

        # --- auth routes ---------------------------------------------------

        def _client_ip(self) -> str:
            return str(self.client_address[0]) if self.client_address else "unknown"

        def _host_allowed(self) -> bool:
            """Local-only surface: reject foreign Host headers (DNS rebinding)."""
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True
            if host.startswith("["):
                close = host.find("]")
                name = host[1:close] if close > 1 else ""
            else:
                head, sep, tail = host.rpartition(":")
                name = head if sep and tail.isdigit() else host
            return name in {"127.0.0.1", "localhost", "::1"}

        def _session_token(self) -> str | None:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator and name == SESSION_COOKIE:
                    return value or None
            return None

        def _require_session(self) -> tuple[int, dict[str, Any]] | None:
            if auth is None:
                return 401, {"error": "unauthorized"}
            session = auth.session_valid(self._session_token())
            if session is None:
                return 401, {"error": "unauthorized"}
            csrf = self.headers.get("X-CSRF-Token", "")
            if not csrf or not hmac.compare_digest(csrf, str(session["csrf_token"])):
                return 403, {"error": "csrf_failed"}
            return None

        def _handle_setup(self) -> None:
            if auth is None:
                self._send(503, {"error": "unavailable"})
                return
            if auth.password_configured():
                self._send(403, {"error": "already_configured"})
                return
            body = self._read_json()
            if body is None:
                return
            try:
                auth.set_password(str(body.get("password", "")))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"ok": True})

        def _handle_login(self) -> None:
            if auth is None:
                self._send(503, {"error": "unavailable"})
                return
            body = self._read_json()
            if body is None:
                return
            token, csrf, reason = auth.login(
                str(body.get("password", "")), self._client_ip()
            )
            if token is None or csrf is None:
                status = {
                    "not_configured": 403,
                    "rate_limited": 429,
                }.get(reason, 401)
                self._send(status, {"error": reason})
                return
            cookie = (
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
            )
            self._send(
                200,
                {"ok": True, "csrf_token": csrf},
                headers={"Set-Cookie": cookie},
            )

        def _handle_logout(self) -> None:
            if auth is not None:
                auth.logout(self._session_token())
            self._send(
                200,
                {"ok": True},
                headers={
                    "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly"
                },
            )

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(400, {"error": "invalid_body"})
                return None
            raw = self.rfile.read(length)
            self._body_consumed = True
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "invalid_json"})
                return None
            if not isinstance(body, dict):
                self._send(400, {"error": "invalid_json"})
                return None
            return body

        def _health(self) -> tuple[int, dict[str, Any]]:
            try:
                payload = snapshot()
            except Exception as exc:
                return 503, {"status": "error", "error": redact(str(exc))[:300]}
            return (200, payload) if payload.get("status") == "ok" else (503, payload)

        def _drain_body(self) -> None:
            """Consume an unread request body before responding.

            Answering while bytes remain unread makes Windows reset the
            socket and destroy the response (WinError 10053).
            """
            if getattr(self, "_body_consumed", False):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            self._body_consumed = True
            if length > 0:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)

        def _send(
            self,
            status: int,
            payload: dict[str, Any],
            headers: dict[str, str] | None = None,
        ) -> None:
            self._drain_body()
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug(format, *args)

    return Handler


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        snapshot: Callable[[], dict[str, Any]],
        api: LocalApi | None = None,
        auth: Any | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._snapshot = snapshot
        self._api = api
        self._auth = auth
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            return
        handler = _make_handler(self._snapshot, self._api, self._auth)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
