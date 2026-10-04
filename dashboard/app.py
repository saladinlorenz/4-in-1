from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config.logging import redact

logger = logging.getLogger(__name__)


def _make_handler(snapshot: Callable[[], dict]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentos"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in ("/health", "/api/status"):
                self._send(404, {"status": "not_found"})
                return
            try:
                payload = snapshot()
            except Exception as exc:
                self._send(503, {"status": "error", "detail": redact(str(exc))[:200]})
                return
            if path == "/health":
                body = {
                    "status": payload.get("status"),
                    "version": payload.get("version"),
                    "uptime_seconds": payload.get("uptime_seconds"),
                }
                code = 200 if payload.get("status") == "ok" else 503
                self._send(code, body)
                return
            self._send(200 if payload.get("status") == "ok" else 503, payload)

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("health: " + fmt, *args)

    return Handler


class HealthServer:
    def __init__(self, host: str, port: int, snapshot: Callable[[], dict]) -> None:
        self.host = host
        self.port = port
        self._snapshot = snapshot
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self._snapshot))
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="health-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
