from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from config.logging import redact

HttpFn = Callable[..., tuple[int, bytes]]


def default_http(request: urllib.request.Request, *, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.getcode() or 0), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(redact(f"network error: {exc.reason}")) from None


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    http: HttpFn | None = None,
) -> tuple[int, Any]:
    """POST (or GET) a JSON payload and return ``(status, decoded_body)``.

    Never raises on HTTP error statuses (the caller inspects them); raises
    ``RuntimeError`` only on transport failures. Secrets must never be part
    of ``url`` or the returned body.
    """
    hdrs = {"User-Agent": "agentos", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=data,
        headers=hdrs,
        method="POST" if payload is not None else "GET",
    )
    status, raw = (http or default_http)(request, timeout=timeout)
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        decoded: Any = json.loads(text) if text else {}
    except ValueError:
        decoded = {}
    return status, decoded
