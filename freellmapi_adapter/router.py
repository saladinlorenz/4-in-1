from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from config.logging import redact
from config.settings import LLMEndpoint

from .fallback import ErrorKind, LLMError, LLMExhausted
from .provider import call_endpoint

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(
        self,
        endpoints: list[LLMEndpoint],
        *,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 2,
        backoff_base: float = 1.5,
        cooldown_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoints = list(endpoints)
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.cooldown_seconds = cooldown_seconds
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(transport=transport, follow_redirects=True)
        self._lock = threading.Lock()
        self._cooldowns: dict[int, float] = {}
        self._last_error: dict[int, str] = {}
        self._last_latency_ms: dict[int, float] = {}
        self._success_count: dict[int, int] = {}

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidates = [endpoint for endpoint in self.endpoints if endpoint.enabled]
        if not candidates:
            raise LLMError(
                ErrorKind.CONFIG,
                "no enabled LLM endpoint configured (set LLM_ENDPOINTS or enable one)",
            )
        failures: list[LLMError] = []
        for endpoint in self._ordered(candidates):
            started = time.perf_counter()
            try:
                result = call_endpoint(
                    endpoint,
                    payload,
                    client=self._client,
                    attempts=endpoint.attempts or self.attempts,
                    backoff_base=self.backoff_base,
                    sleep=self._sleep,
                )
            except LLMError as error:
                failures.append(error)
                self._mark_failure(endpoint, error)
                logger.warning("LLM endpoint failed: %s", error)
                continue
            self._mark_success(endpoint, (time.perf_counter() - started) * 1000)
            return result
        raise LLMExhausted(failures)

    def reload(self, endpoints: list[LLMEndpoint]) -> None:
        """Hot-swap the endpoint list (dashboard edits) without restarting."""
        with self._lock:
            self.endpoints = list(endpoints)
            # per-index runtime state is invalid after a swap
            self._cooldowns.clear()
            self._last_error.clear()
            self._last_latency_ms.clear()
            self._success_count.clear()
        logger.info("LLM endpoints reloaded: %d configured", len(self.endpoints))

    def test_endpoint(
        self, endpoint: LLMEndpoint, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Single connectivity probe: one attempt, no backoff, tiny prompt."""
        body = payload or {
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
        started = time.perf_counter()
        try:
            call_endpoint(
                endpoint,
                body,
                client=self._client,
                attempts=1,
                backoff_base=1.0,
                sleep=lambda _seconds: None,
            )
        except LLMError as error:
            return {"ok": False, "error": redact(str(error))}
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def health(self) -> list[dict[str, Any]]:
        now = self._clock()
        report = []
        with self._lock:
            for index, endpoint in enumerate(self.endpoints):
                until = self._cooldowns.get(index, 0.0)
                report.append(
                    {
                        "name": endpoint.display_name,
                        "endpoint": endpoint.label,
                        "base_url": endpoint.base_url,
                        "enabled": endpoint.enabled,
                        "priority": endpoint.priority,
                        "state": "cooldown" if until > now else "ready",
                        "cooldown_seconds": round(max(0.0, until - now), 1),
                        "last_error": self._last_error.get(index),
                        "last_latency_ms": self._last_latency_ms.get(index),
                        "success_count": self._success_count.get(index, 0),
                    }
                )
        return report

    def close(self) -> None:
        self._client.close()

    def _ordered(
        self, candidates: list[LLMEndpoint] | None = None
    ) -> list[LLMEndpoint]:
        now = self._clock()
        pool = self.endpoints if candidates is None else list(candidates)
        ready: list[LLMEndpoint] = []
        cooled: list[LLMEndpoint] = []
        with self._lock:
            for endpoint in pool:
                try:
                    index = self.endpoints.index(endpoint)
                except ValueError:
                    index = -1
                until = self._cooldowns.get(index, 0.0)
                if until <= now:
                    ready.append(endpoint)
                else:
                    cooled.append(endpoint)
        return ready + cooled

    def _id(self, endpoint: LLMEndpoint) -> int:
        return self.endpoints.index(endpoint)

    def _mark_success(self, endpoint: LLMEndpoint, latency_ms: float) -> None:
        index = self._id(endpoint)
        with self._lock:
            self._cooldowns.pop(index, None)
            self._last_error.pop(index, None)
            self._last_latency_ms[index] = round(latency_ms, 1)
            self._success_count[index] = self._success_count.get(index, 0) + 1

    def _mark_failure(self, endpoint: LLMEndpoint, error: LLMError) -> None:
        index = self._id(endpoint)
        with self._lock:
            self._last_error[index] = str(error)
            if error.retryable or error.kind in (ErrorKind.AUTH, ErrorKind.NOT_FOUND):
                self._cooldowns[index] = self._clock() + self.cooldown_seconds
