from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_STEPS = 20
MAX_STEP_CHARS = 4000
MAX_CONTEXT_CHARS = 2000
MAX_KEY_CHARS = 128

STEP_KINDS = ("agent", "confirmation")


def _name(value: str, label: str) -> str:
    if not NAME_RE.fullmatch(value or ""):
        raise ValueError(f"{label} must be 1-64 chars (letters, digits, _ - .)")
    return value


def validation_message(exc: Exception) -> str:
    """First human-readable error from a Pydantic validation failure."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:
            items = []
        if items:
            first = items[0]
            return str(first.get("msg") or first)[:300]
    return str(exc).splitlines()[-1][:300]


class StepInput(BaseModel):
    """One workflow step: closed JSON object, no unknown keys accepted."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["agent", "confirmation"]
    prompt: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _name(value, "step name")

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("step prompt must not be empty")
        if len(text) > MAX_STEP_CHARS:
            raise ValueError(f"step prompt exceeds {MAX_STEP_CHARS} chars")
        return text


class WorkflowInput(BaseModel):
    """Workflow definition: closed JSON object ``{name, steps}`` only."""

    model_config = ConfigDict(extra="forbid")

    name: str
    steps: list[StepInput] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _name(value, "workflow name")


class RunInput(BaseModel):
    """Launch request: closed JSON object ``{name, context?, idempotency_key?}``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    context: str = Field(default="", max_length=MAX_CONTEXT_CHARS)
    idempotency_key: str | None = Field(default=None, max_length=MAX_KEY_CHARS)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _name(value, "workflow name")

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ScheduleInput(BaseModel):
    """Cron job: closed JSON object ``{name, workflow_name, hour, minute, enabled?}``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    workflow_name: str
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _check_job_name(cls, value: str) -> str:
        return _name(value, "job name")

    @field_validator("workflow_name")
    @classmethod
    def _check_workflow_name(cls, value: str) -> str:
        return _name(value, "workflow name")


def _validated(model: type[BaseModel], payload: Any, label: str) -> Any:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        return model(**payload)
    except Exception as exc:
        raise ValueError(validation_message(exc)) from None


def parse_workflow(payload: Any) -> tuple[str, list[dict[str, str]]]:
    """Validate a workflow definition; returns ``(name, normalized_steps)``."""
    data = _validated(WorkflowInput, payload, "workflow")
    steps = [
        {"name": step.name, "kind": step.kind, "prompt": step.prompt}
        for step in data.steps
    ]
    return str(data.name), steps


def parse_run(payload: Any) -> dict[str, Any]:
    """Validate a run request; returns a normalized dict."""
    data = _validated(RunInput, payload, "run request")
    return {
        "name": data.name,
        "context": data.context,
        "idempotency_key": data.idempotency_key,
    }


def parse_schedule(payload: Any) -> dict[str, Any]:
    """Validate a cron schedule request; returns a normalized dict."""
    data = _validated(ScheduleInput, payload, "schedule")
    return {
        "name": data.name,
        "workflow_name": data.workflow_name,
        "hour": data.hour,
        "minute": data.minute,
        "enabled": bool(data.enabled),
    }
