from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent.backends import describe_backends, get_backend
from agent.permissions import ALLOWED_TOOL_NAMES

from .settings_service import SettingsService, validation_message

CATEGORY = "agent"
ACTOR = "dashboard"
FINAL_ANSWER = "final_answer"
DEFAULT_BACKEND = "smolagents"

KEY_SYSTEM_PROMPT = "agent.system_prompt"
KEY_MAX_STEPS = "agent.max_steps"
KEY_MAX_OUTPUT_CHARS = "agent.max_output_chars"
KEY_TEMPERATURE = "agent.temperature"
KEY_DRY_RUN = "agent.dry_run"
KEY_TOOLS = "agent.tools"
KEY_BACKEND = "agent.backend"

DEFAULT_TEMPERATURE = 0.3
MAX_PROMPT_CHARS = 4000

# Tools with external side effects: removed automatically in dry-run mode.
DRY_RUN_BLOCKED = frozenset(
    {
        "social_publish",
        "workflow_create",
        "workflow_run",
        "send_notification",
        "write_file",
    }
)

# What the dashboard may toggle (final_answer is structural, never optional).
CONFIGURABLE_TOOLS = sorted(set(ALLOWED_TOOL_NAMES) - {FINAL_ANSWER})


class AgentSettingsInput(BaseModel):
    """Dashboard payload for /api/agent. Unknown fields are ignored."""

    backend: str = DEFAULT_BACKEND
    system_prompt: str = ""
    max_steps: int = Field(12, ge=1, le=50)
    max_output_chars: int = Field(4000, ge=200, le=100_000)
    temperature: float = Field(DEFAULT_TEMPERATURE, ge=0, le=2)
    dry_run: bool = False
    tools: list[str] | None = None

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        clean = (value or "").strip()
        backend = get_backend(clean)
        if backend is None:
            raise ValueError(f"unknown agent backend: {clean or '?'}")
        ok, reason = backend.availability()
        if not ok:
            raise ValueError(f"agent backend {clean} unavailable: {reason}")
        return clean

    @field_validator("system_prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        clean = (value or "").replace("\r", "")
        if len(clean) > MAX_PROMPT_CHARS:
            raise ValueError(f"system_prompt must be at most {MAX_PROMPT_CHARS} chars")
        return clean

    @field_validator("tools")
    @classmethod
    def _check_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unique = list(dict.fromkeys(value))
        unknown = [name for name in unique if name not in CONFIGURABLE_TOOLS]
        if unknown:
            raise ValueError(
                "unknown tools: "
                + ", ".join(unknown)
                + f" (allowed: {', '.join(CONFIGURABLE_TOOLS)})"
            )
        if not unique:
            raise ValueError("tools must list at least one tool")
        return unique


class AgentConfigService:
    """Agent settings (SQLite) merged over `.env` defaults.

    Read by ``AgentRunner`` on every run, so dashboard edits apply to the
    next task without a restart. Values written through the generic settings
    editor are re-validated here before use (hostile or broken entries fall
    back to safe defaults).
    """

    def __init__(self, settings_service: SettingsService, defaults: Any) -> None:
        self.settings = settings_service
        self.defaults = defaults

    # --- reading ---------------------------------------------------------

    def _raw(self, key: str) -> Any:
        try:
            return self.settings.get(key, None)
        except ValueError:
            return None

    @staticmethod
    def _int(value: Any, fallback: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return fallback
        return number if low <= number <= high else fallback

    @staticmethod
    def _float(value: Any, fallback: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if low <= number <= high else fallback

    def effective(self) -> dict[str, Any]:
        prompt = self._raw(KEY_SYSTEM_PROMPT)
        tools = self._raw(KEY_TOOLS)
        if not isinstance(tools, list) or not tools:
            tools = None
        else:
            tools = [str(name) for name in tools if str(name) in CONFIGURABLE_TOOLS] or None
        backend = self._raw(KEY_BACKEND)
        if not isinstance(backend, str) or get_backend(backend) is None:
            backend = DEFAULT_BACKEND
        else:
            registered = get_backend(backend)
            ok, _ = registered.availability() if registered else (False, "")
            if not ok:
                backend = DEFAULT_BACKEND
        return {
            "backend": backend,
            "system_prompt": prompt if isinstance(prompt, str) else "",
            "max_steps": self._int(
                self._raw(KEY_MAX_STEPS),
                self.defaults.agent_max_steps,
                1,
                50,
            ),
            "max_output_chars": self._int(
                self._raw(KEY_MAX_OUTPUT_CHARS),
                self.defaults.agent_max_output_chars,
                200,
                100_000,
            ),
            "temperature": self._float(
                self._raw(KEY_TEMPERATURE), DEFAULT_TEMPERATURE, 0.0, 2.0
            ),
            "dry_run": self._raw(KEY_DRY_RUN) is True,
            "tools": tools,
        }

    def allowed_tools(self) -> frozenset[str]:
        """Effective whitelist: config intersect ALLOWED minus dry-run effects."""
        config = self.effective()
        allowed = set(config["tools"]) if config["tools"] else set(ALLOWED_TOOL_NAMES)
        if config["dry_run"]:
            allowed -= DRY_RUN_BLOCKED
        allowed &= set(ALLOWED_TOOL_NAMES)
        allowed.add(FINAL_ANSWER)
        return frozenset(allowed)

    # --- writing ---------------------------------------------------------

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if "dry_run" in payload and not isinstance(payload.get("dry_run"), bool):
            raise ValueError("dry_run must be a boolean")
        if "tools" in payload and payload.get("tools") is not None:
            tools = payload.get("tools")
            if not isinstance(tools, list) or not all(
                isinstance(name, str) for name in tools
            ):
                raise ValueError("tools must be a list of tool names")
        try:
            data = AgentSettingsInput(**payload)
        except Exception as exc:
            raise ValueError(validation_message(exc)) from None
        self.settings.set(KEY_SYSTEM_PROMPT, data.system_prompt, category=CATEGORY, actor=ACTOR)
        self.settings.set(KEY_MAX_STEPS, data.max_steps, category=CATEGORY, actor=ACTOR)
        self.settings.set(
            KEY_MAX_OUTPUT_CHARS, data.max_output_chars, category=CATEGORY, actor=ACTOR
        )
        self.settings.set(KEY_TEMPERATURE, data.temperature, category=CATEGORY, actor=ACTOR)
        self.settings.set(KEY_DRY_RUN, data.dry_run, category=CATEGORY, actor=ACTOR)
        self.settings.set(KEY_TOOLS, data.tools, category=CATEGORY, actor=ACTOR)
        self.settings.set(KEY_BACKEND, data.backend, category=CATEGORY, actor=ACTOR)
        self.settings.storage.add_audit(ACTOR, "agent.settings.update", "")
        return self.effective()

    def describe(self) -> dict[str, Any]:
        config = self.effective()
        return {
            "settings": config,
            "available_tools": CONFIGURABLE_TOOLS,
            "dry_run_blocked": sorted(DRY_RUN_BLOCKED),
            "backends": describe_backends(selected=config["backend"]),
            "defaults": {
                "max_steps": self.defaults.agent_max_steps,
                "max_output_chars": self.defaults.agent_max_output_chars,
                "temperature": DEFAULT_TEMPERATURE,
                "backend": DEFAULT_BACKEND,
            },
        }
