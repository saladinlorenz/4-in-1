from __future__ import annotations

import json
from typing import Any

CATEGORIES = {
    "general",
    "agent",
    "models",
    "social",
    "security",
    "integrations",
    "workflows",
}

MAX_VALUE_CHARS = 4096
_KEY_MAX = 64


def validation_message(exc: Exception) -> str:
    """Human-readable first error from a Pydantic validation failure."""
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


class SettingsService:
    """Single door for non-sensitive settings (SQLite, audited).

    Secrets never belong here: they go through ``config.secrets.SecretStore``.
    Every mutation writes an audit entry with the acting identity.
    """

    def __init__(self, storage: Any, actor: str = "dashboard") -> None:
        self.storage = storage
        self.actor = actor

    @staticmethod
    def _check_key(key: str) -> str:
        clean = (key or "").strip()
        if not clean or len(clean) > _KEY_MAX:
            raise ValueError("setting key must be 1-64 chars")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
        if any(char not in allowed for char in clean):
            raise ValueError("setting key contains invalid characters")
        return clean

    def get(self, key: str, default: Any = None) -> Any:
        value = self.storage.get_setting(self._check_key(key))
        return default if value is None else value

    def set(
        self,
        key: str,
        value: Any,
        *,
        category: str = "general",
        actor: str | None = None,
    ) -> bool:
        clean = self._check_key(key)
        if category not in CATEGORIES:
            raise ValueError(f"unknown settings category: {category}")
        payload = json.dumps(value, ensure_ascii=False)
        if len(payload) > MAX_VALUE_CHARS:
            raise ValueError("setting value is too large")
        who = actor or self.actor
        self.storage.set_setting(clean, value, category=category, updated_by=who)
        self.storage.add_audit(who, f"settings.set:{clean}", f"category={category}")
        return True

    def delete(self, key: str, *, actor: str | None = None) -> bool:
        clean = self._check_key(key)
        removed = self.storage.delete_setting(clean)
        if removed:
            who = actor or self.actor
            self.storage.add_audit(who, f"settings.delete:{clean}")
        return removed

    def list(self, category: str | None = None) -> list[dict[str, Any]]:
        if category and category not in CATEGORIES:
            raise ValueError(f"unknown settings category: {category}")
        return self.storage.list_settings(category)
