from __future__ import annotations

from typing import Any

from .http_json import HttpFn, request_json

DEVTO_API_KEY = "DEVTO_API_KEY"


class DevtoAdapter:
    """Publishes a draft as a real DEV.to article (REST API, api-key header).

    Secrets are read from the SecretStore on every call, so a rotation is
    effective without a restart. Nothing is ever simulated: a missing key or
    a non-2xx response raises and the draft is marked FAILED by the service.
    """

    platform = "devto"
    api_url = "https://dev.to/api/articles"
    me_url = "https://dev.to/api/users/me"

    def __init__(self, secret_store: Any, *, timeout: float = 20.0, http: HttpFn | None = None):
        self._secrets = secret_store
        self._timeout = timeout
        self._http = http

    # --- configuration (no network) --------------------------------------

    def configured(self) -> tuple[bool, str]:
        mask = self._secrets.mask(DEVTO_API_KEY)
        if mask:
            return True, mask
        return False, f"{DEVTO_API_KEY} absente"

    def _key(self) -> str:
        key = self._secrets.get(DEVTO_API_KEY)
        if not key:
            raise RuntimeError(f"{DEVTO_API_KEY} is not configured")
        return key

    # --- real API calls ---------------------------------------------------

    @staticmethod
    def _split(content: str) -> tuple[str, str]:
        lines = (content or "").splitlines()
        first = next((line.strip() for line in lines if line.strip()), "")
        title = (first[:128] or "AgentOS draft").strip()
        if lines and lines[0].strip() == first:
            body = "\n".join(lines[1:]).strip()
        else:
            body = (content or "").strip()
        return title, body or title

    def publish(self, content: str) -> str:
        title, body = self._split(content)
        status, data = request_json(
            self.api_url,
            payload={
                "article": {
                    "title": title,
                    "body_markdown": body,
                    "published": True,
                }
            },
            headers={"api-key": self._key()},
            timeout=self._timeout,
            http=self._http,
        )
        url = data.get("url") if isinstance(data, dict) else None
        if status not in (200, 201) or not url:
            raise RuntimeError(f"dev.to publish failed: HTTP {status}")
        return f"devto:{url}"

    def test(self) -> str:
        status, data = request_json(
            self.me_url,
            headers={"api-key": self._key()},
            timeout=self._timeout,
            http=self._http,
        )
        username = data.get("username") if isinstance(data, dict) else None
        if status == 200 and username:
            return f"authenticated as @{username}"
        raise RuntimeError(f"dev.to connection test failed: HTTP {status}")
