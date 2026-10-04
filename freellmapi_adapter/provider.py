from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from config.logging import redact
from config.settings import LLMEndpoint

from .fallback import ErrorKind, LLMError, backoff_delay, classify_status

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 400


def _safe_body(text: str) -> str:
    return redact(text.replace("\n", " "))[:MAX_BODY_CHARS]


def call_endpoint(
    endpoint: LLMEndpoint,
    payload: dict[str, Any],
    *,
    client: httpx.Client,
    attempts: int = 2,
    backoff_base: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
    tool_choice_retry: bool = True,
) -> dict[str, Any]:
    url = endpoint.base_url.rstrip("/") + "/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    body: dict[str, Any] = {**payload, "model": endpoint.model}
    if endpoint.max_tokens:
        body.setdefault("max_tokens", endpoint.max_tokens)
    if endpoint.temperature is not None:
        body.setdefault("temperature", endpoint.temperature)
    label = endpoint.label

    attempt = 0
    tool_choice_dropped = not tool_choice_retry or "tool_choice" not in body
    while True:
        attempt += 1
        try:
            response = client.post(url, json=body, headers=headers, timeout=endpoint.timeout)
        except httpx.TimeoutException:
            error = LLMError(ErrorKind.TIMEOUT, "request timed out", endpoint=label)
        except httpx.HTTPError as exc:
            error = LLMError(ErrorKind.NETWORK, redact(str(exc)), endpoint=label)
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    error = LLMError(
                        ErrorKind.INVALID_RESPONSE,
                        "response body is not JSON",
                        endpoint=label,
                        status_code=200,
                    )
                else:
                    if isinstance(data, dict) and data.get("choices"):
                        return data
                    error = LLMError(
                        ErrorKind.INVALID_RESPONSE,
                        "response JSON has no choices",
                        endpoint=label,
                        status_code=200,
                    )
            else:
                text = _safe_body(response.text)
                if response.status_code == 400 and not tool_choice_dropped:
                    tool_choice_dropped = True
                    body.pop("tool_choice", None)
                    logger.warning("retrying %s without tool_choice after HTTP 400", label)
                    continue
                error = LLMError(
                    classify_status(response.status_code),
                    text or "request failed",
                    endpoint=label,
                    status_code=response.status_code,
                )

        if error.retryable and attempt < attempts:
            logger.warning("LLM attempt %s failed for %s: %s", attempt, label, error)
            sleep(backoff_delay(attempt, base=backoff_base))
            continue
        logger.warning("LLM endpoint %s gave up: %s", label, error)
        raise error
