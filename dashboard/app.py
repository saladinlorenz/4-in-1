from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from config.logging import redact

logger = logging.getLogger(__name__)

GET_ROUTES = {
    "/health",
    "/api/status",
    "/api/tasks",
    "/api/incidents",
    "/api/memory",
    "/api/drafts",
    "/api/confirmations",
}

DECISIONS = {"approve": "APPROVED", "reject": "REJECTED"}


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


class LocalApi:
    """Read/write controller behind the local HTTP API.

    Bound to 127.0.0.1 only: it exposes task, confirmation and draft state
    to the local dashboard. Mutating calls (cancel, approve, reject) go
    through the same runner code paths as the Telegram commands.
    """

    def __init__(self, runner: Any, storage: Any) -> None:
        self.runner = runner
        self.storage = storage

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

    # --- POST ------------------------------------------------------------

    def cancel_task(self, task_id: int) -> tuple[bool, str]:
        return self.runner.cancel(task_id)

    def decide(self, confirmation_id: int, decision: str) -> tuple[bool, str]:
        return self.runner.resolve_confirmation(confirmation_id, decision)


def _make_handler(
    snapshot: Callable[[], dict[str, Any]], api: LocalApi | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentos"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                self._send(*self._health())
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
            routes = {
                "/api/tasks": api.tasks,
                "/api/incidents": api.incidents,
                "/api/memory": api.memory,
                "/api/drafts": api.drafts,
                "/api/confirmations": api.confirmations,
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

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if api is None:
                self._send(404, {"error": "not_found"})
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

        def _health(self) -> tuple[int, dict[str, Any]]:
            try:
                payload = snapshot()
            except Exception as exc:
                return 503, {"status": "error", "error": redact(str(exc))[:300]}
            return (200, payload) if payload.get("status") == "ok" else (503, payload)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
    ) -> None:
        self._host = host
        self._port = port
        self._snapshot = snapshot
        self._api = api
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
        handler = _make_handler(self._snapshot, self._api)
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
