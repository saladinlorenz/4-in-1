from __future__ import annotations

import json
from datetime import datetime, timezone

from smolagents import tool

from config.logging import redact

from .base import ToolDeps


def make_notify_tool(deps: ToolDeps) -> object:
    @tool
    def send_notification(message: str) -> str:
        """Send a notification message to the operator over Telegram.

        Args:
            message: The notification text to send to the operator.
        """
        cleaned = (message or "").strip()
        if not cleaned:
            return "Notification not sent: empty message."
        try:
            deps.notifier.send(cleaned)
        except Exception as exc:
            return f"Notification failed: {redact(str(exc))}"
        return "Notification sent."

    return send_notification


def make_status_tool(deps: ToolDeps) -> object:
    @tool
    def get_status() -> str:
        """Return the current status of the agent system: tasks, uptime and LLM endpoints."""
        try:
            payload = deps.status_fn()
            payload.setdefault("time", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"Status failed: {redact(str(exc))}"

    return get_status
