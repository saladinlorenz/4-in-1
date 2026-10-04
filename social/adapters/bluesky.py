from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .http_json import HttpFn, request_json

BLUESKY_HANDLE = "BLUESKY_HANDLE"
BLUESKY_APP_PASSWORD = "BLUESKY_APP_PASSWORD"
MAX_POST_CHARS = 300


class BlueskyAdapter:
    """Publishes a draft as a real Bluesky post (AT Protocol, bsky.social).

    Login (createSession) happens on every call: the app password stays in
    the SecretStore and is never returned or logged. A missing credential or
    a non-2xx response raises and the service marks the draft FAILED.
    """

    platform = "bluesky"
    session_url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    record_url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"

    def __init__(self, secret_store: Any, *, timeout: float = 20.0, http: HttpFn | None = None):
        self._secrets = secret_store
        self._timeout = timeout
        self._http = http

    # --- configuration (no network) --------------------------------------

    def configured(self) -> tuple[bool, str]:
        mask = self._secrets.mask(BLUESKY_HANDLE)
        password = self._secrets.get(BLUESKY_APP_PASSWORD)
        if mask and password:
            return True, f"{BLUESKY_HANDLE}: {mask}"
        missing = [
            name
            for name, value in ((BLUESKY_HANDLE, mask), (BLUESKY_APP_PASSWORD, password))
            if not value
        ]
        return False, " / ".join(f"{name} absente" for name in missing)

    def _session(self) -> dict[str, Any]:
        handle = self._secrets.get(BLUESKY_HANDLE)
        password = self._secrets.get(BLUESKY_APP_PASSWORD)
        if not handle or not password:
            raise RuntimeError(f"{BLUESKY_HANDLE} / {BLUESKY_APP_PASSWORD} are not configured")
        status, data = request_json(
            self.session_url,
            payload={"identifier": handle, "password": password},
            timeout=self._timeout,
            http=self._http,
        )
        if status != 200 or not isinstance(data, dict) or not data.get("accessJwt"):
            raise RuntimeError(f"bluesky login failed: HTTP {status}")
        return data

    # --- real API calls ---------------------------------------------------

    def publish(self, content: str) -> str:
        text = (content or "").strip()
        if not text:
            raise RuntimeError("draft content is empty")
        if len(text) > MAX_POST_CHARS:
            raise RuntimeError(f"bluesky post exceeds {MAX_POST_CHARS} characters")
        session = self._session()
        status, data = request_json(
            self.record_url,
            payload={
                "repo": str(session.get("did") or ""),
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": text,
                    "createdAt": datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
                "validate": True,
            },
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            timeout=self._timeout,
            http=self._http,
        )
        uri = data.get("uri") if isinstance(data, dict) else None
        if status not in (200, 201) or not uri:
            raise RuntimeError(f"bluesky publish failed: HTTP {status}")
        return f"bluesky:{uri}"

    def test(self) -> str:
        session = self._session()
        who = session.get("handle") or session.get("did") or "?"
        return f"authenticated as {who}"
