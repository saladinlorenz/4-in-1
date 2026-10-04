from __future__ import annotations

import smtplib
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from config.logging import redact
from social.adapters.http_json import HttpFn, request_json

INTEGRATIONS = ("telegram", "ddgs", "smtp", "github")

TELEGRAM_TOKEN = "TELEGRAM_BOT_TOKEN"
GITHUB_TOKEN = "GITHUB_TOKEN"
SMTP_HOST = "SMTP_HOST"
SMTP_PORT = "SMTP_PORT"
SMTP_USER = "SMTP_USER"
SMTP_PASSWORD = "SMTP_PASSWORD"


class IntegrationTester:
    """Real connection tests for the configured integrations.

    Every test performs a genuine, read-only network call (getMe, /user,
    SMTP handshake, one DDGS query): nothing is sent, posted or stored.
    Secrets come from the SecretStore (settings fallback for the Telegram
    bot token) and never appear in messages returned to the HTTP API.
    """

    def __init__(
        self,
        secret_store: Any,
        settings: Any | None = None,
        *,
        http: HttpFn | None = None,
        search_fn: Callable[[str, int], list] | None = None,
        smtp_factory: Callable[..., Any] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._secrets = secret_store
        self._settings = settings
        self._http = http
        self._search_fn = search_fn
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._timeout = timeout

    # --- status (local, no network) --------------------------------------

    def _telegram_token(self) -> str | None:
        token = self._secrets.get(TELEGRAM_TOKEN)
        if not token and self._settings is not None:
            token = str(getattr(self._settings, "telegram_bot_token", "") or "") or None
        return token

    @staticmethod
    def _mask(value: str) -> str:
        suffix = value[-4:] if len(value) >= 4 else ""
        return f"Configured (ends ...{suffix})"

    def describe(self) -> list[dict[str, Any]]:
        token = self._telegram_token()
        github = self._secrets.get(GITHUB_TOKEN)
        host = self._secrets.get(SMTP_HOST)
        password = self._secrets.get(SMTP_PASSWORD)
        rows = [
            {
                "name": "telegram",
                "configured": bool(token),
                "status": self._mask(token) if token else f"{TELEGRAM_TOKEN} absent",
            },
            {"name": "ddgs", "configured": True, "status": "aucune clé requise"},
            {
                "name": "smtp",
                "configured": bool(host),
                "status": (
                    f"{SMTP_HOST}: {self._mask(host)}" if host else f"{SMTP_HOST} absent"
                ),
            },
            {
                "name": "github",
                "configured": bool(github),
                "status": self._mask(github) if github else f"{GITHUB_TOKEN} absent",
            },
        ]
        if host and password:
            rows[2]["status"] += f" · {SMTP_PASSWORD}: {self._mask(password)}"
        return rows

    # --- real connection tests -------------------------------------------

    def test(self, kind: str) -> str:
        name = str(kind or "").strip().lower()
        if name == "telegram":
            return self._test_telegram()
        if name == "ddgs":
            return self._test_ddgs()
        if name == "smtp":
            return self._test_smtp()
        if name == "github":
            return self._test_github()
        raise ValueError(f"unknown integration '{kind}'")

    def _test_telegram(self) -> str:
        token = self._telegram_token()
        if not token:
            raise RuntimeError(f"{TELEGRAM_TOKEN} is not configured")
        status, data = request_json(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=self._timeout,
            http=self._http,
        )
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            username = (data.get("result") or {}).get("username") or "?"
            return f"authenticated as @{username}"
        raise RuntimeError(redact(f"telegram getMe failed: HTTP {status}"))

    def _test_github(self) -> str:
        token = self._secrets.get(GITHUB_TOKEN)
        if not token:
            raise RuntimeError(f"{GITHUB_TOKEN} is not configured")
        status, data = request_json(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            http=self._http,
        )
        if status == 200 and isinstance(data, dict) and data.get("login"):
            return f"authenticated as {data['login']}"
        raise RuntimeError(redact(f"github /user failed: HTTP {status}"))

    def _test_ddgs(self) -> str:
        search = self._search_fn or self._default_search
        results = search("AgentOS connection test", 1)
        count = len(results) if isinstance(results, list) else 0
        if count < 1:
            raise RuntimeError("ddgs returned no results (network or engine failure)")
        return f"search ok ({count} result)"

    @staticmethod
    def _default_search(query: str, count: int) -> list:
        from agent.tools.web_search import default_search

        return default_search(query, count, 5.0)

    def _test_smtp(self) -> str:
        host = self._secrets.get(SMTP_HOST)
        if not host:
            raise RuntimeError(f"{SMTP_HOST} is not configured")
        raw_port = self._secrets.get(SMTP_PORT)
        try:
            port = int(raw_port) if raw_port else 587
        except ValueError:
            raise RuntimeError(f"{SMTP_PORT} must be an integer") from None
        user = self._secrets.get(SMTP_USER)
        password = self._secrets.get(SMTP_PASSWORD)
        client = None
        try:
            client = self._smtp_factory(host, port, timeout=self._timeout)
            ehlo = getattr(client, "ehlo", None)
            if callable(ehlo):
                ehlo()
            if user and password:
                client.login(user, password)
        finally:
            quit_fn = getattr(client, "quit", None)
            if callable(quit_fn):
                with suppress(Exception):
                    quit_fn()
        mode = "authenticated" if user and password else "anonymous"
        return f"connected to {host}:{port} ({mode})"
