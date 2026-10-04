from __future__ import annotations

import enum
import random


class ErrorKind(str, enum.Enum):
    CONFIG = "config"
    TIMEOUT = "timeout"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    CLIENT = "client"
    SERVER = "server"
    INVALID_RESPONSE = "invalid_response"


class LLMError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        endpoint: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.endpoint = endpoint
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return is_retryable(self.kind)

    def __str__(self) -> str:
        where = f" [{self.endpoint}]" if self.endpoint else ""
        status = f" HTTP {self.status_code}" if self.status_code else ""
        return f"{self.kind.value}{status}{where}: {self.message}"


class LLMExhausted(LLMError):
    def __init__(self, failures: list[LLMError]) -> None:
        detail = "; ".join(str(failure) for failure in failures) or "no endpoint attempted"
        super().__init__(
            ErrorKind.CONFIG if not failures else failures[-1].kind,
            f"all LLM endpoints failed: {detail}",
        )
        self.failures = failures


def classify_status(status_code: int) -> ErrorKind:
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    if status_code in (401, 403):
        return ErrorKind.AUTH
    if status_code == 404:
        return ErrorKind.NOT_FOUND
    if 400 <= status_code < 500:
        return ErrorKind.CLIENT
    if status_code >= 500:
        return ErrorKind.SERVER
    return ErrorKind.INVALID_RESPONSE


def is_retryable(kind: ErrorKind) -> bool:
    return kind in {
        ErrorKind.TIMEOUT,
        ErrorKind.NETWORK,
        ErrorKind.RATE_LIMIT,
        ErrorKind.SERVER,
        ErrorKind.INVALID_RESPONSE,
    }


def backoff_delay(attempt: int, base: float = 1.5, cap: float = 8.0) -> float:
    delay = min(cap, base ** max(1, attempt))
    return delay + random.uniform(0, min(0.5, delay * 0.2))
