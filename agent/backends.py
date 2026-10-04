from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import AgentRunner


class AgentBackend(ABC):
    """Common interface for the agent implementation behind AgentRunner.

    Only ONE backend is active per run (selected in settings). Backends never
    receive secrets, never talk to the dashboard and never modify settings:
    ``AgentRunner`` stays the task director and SQLite stays the source of
    truth.
    """

    name: str = "abstract"

    def availability(self) -> tuple[bool, str]:
        """(usable, reason-if-not). An unavailable backend cannot be selected."""
        return True, ""

    @abstractmethod
    def build(self, runner: AgentRunner) -> Any:
        """Return a runnable agent: object with run(prompt) + interrupt_switch."""


class SmolagentsBackend(AgentBackend):
    """Wrapper around the existing ToolCallingAgent implementation."""

    name = "smolagents"

    def build(self, runner: AgentRunner) -> Any:
        return runner._build_agent()


class SmolClawBackend(AgentBackend):
    """Placeholder AFTER real inspection of the vendored ``smolclaw-main/``.

    Inspection facts (2026-10-04, files: package.json, LICENSE, README.md, src/):
    - license: MIT (Madison Carter) - usable;
    - runtime: Bun/TypeScript (``"start": "bun run src/index.ts"``), no
      Python package or callable entrypoint;
    - deps: @anthropic-ai/sdk, openai, grammy (own Telegram bot), croner,
      sqlite-vec, zod, pino;
    - it is a standalone 24/7 Telegram agent with its own polling loop, own
      SQLite memory and unrestricted shell/process tools.

    Integration blockers on our target (Python 3.11 on Debian PRoot, no JS
    runtime, exactly one Telegram bot, one active agent, no shell without
    human confirmation): it would need Bun, a second bot token and would run
    a concurrent agent engine. Registered as UNAVAILABLE on purpose: it can
    be listed in the dashboard, but it can never be selected or built until
    a real bridge exists. No simulated integration.
    """

    name = "smolclaw"

    def availability(self) -> tuple[bool, str]:
        return (
            False,
            "Bun/TypeScript app (MIT, inspected): no Python entrypoint, "
            "needs bun + a second Telegram bot; not integrated",
        )

    def build(self, runner: AgentRunner) -> Any:
        raise RuntimeError(
            "smolclaw backend is not integrated (see inspection notes)"
        )


BACKENDS: dict[str, AgentBackend] = {}


def register_backend(backend: AgentBackend) -> None:
    BACKENDS[backend.name] = backend


def unregister_backend(name: str) -> None:
    BACKENDS.pop(name, None)


def get_backend(name: str) -> AgentBackend | None:
    return BACKENDS.get(name)


def describe_backends(selected: str = "") -> list[dict[str, Any]]:
    rows = []
    for name in sorted(BACKENDS):
        backend = BACKENDS[name]
        ok, reason = backend.availability()
        rows.append(
            {
                "name": name,
                "available": bool(ok),
                "reason": reason,
                "selected": name == selected,
            }
        )
    return rows


register_backend(SmolagentsBackend())
register_backend(SmolClawBackend())
