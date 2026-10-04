from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

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
        if not self.endpoints:
            raise LLMError(ErrorKind.CONFIG, "no LLM endpoint configured (set LLM_ENDPOINTS)")
        failures: list[LLMError] = []
        for endpoint in self._ordered():
            started = time.perf_counter()
            try:
                result = call_endpoint(
                    endpoint,
                    payload,
                    client=self._client,
                    attempts=self.attempts,
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

    def health(self) -> list[dict[str, Any]]:
        now = self._clock()
        report = []
        with self._lock:
            for index, endpoint in enumerate(self.endpoints):
                until = self._cooldowns.get(index, 0.0)
                report.append(
                    {
                        "endpoint": endpoint.label,
                        "base_url": endpoint.base_url,
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

    def _ordered(self) -> list[LLMEndpoint]:
        now = self._clock()
        ready: list[LLMEndpoint] = []
        cooled: list[LLMEndpoint] = []
        with self._lock:
            for index, endpoint in enumerate(self.endpoints):
                if self._cooldowns.get(index, 0.0) <= now:
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
