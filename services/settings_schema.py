from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MISSING = object()

_TYPES = ("str", "int", "float", "bool", "choice", "list[int]", "secret")

# Every key here lives in the ``settings`` table (SQLite) and is applied to the
# pydantic Settings object at boot (``apply_db_settings``) => "restart" badge.
SETTINGS_SPECS: list[dict[str, Any]] = [
    # --- general ---
    {"key": "app_name", "category": "general", "label": "Nom de l'instance", "type": "str", "max": 64},
    {
        "key": "log_level",
        "category": "general",
        "label": "Niveau de log",
        "type": "choice",
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    },
    # --- telegram ---
    {"key": "telegram_bot_token", "category": "telegram", "label": "Jeton du bot", "type": "secret"},
    {
        "key": "telegram_admin_chat_id",
        "category": "telegram",
        "label": "Chat ID administrateur",
        "type": "int",
        "min": 0,
        "help": "0 = notifications Telegram désactivées",
    },
    {
        "key": "telegram_allowed_user_ids",
        "category": "telegram",
        "label": "Utilisateurs autorisés",
        "type": "list[int]",
        "help": "Liste d'IDs Telegram (séparés par des virgules) ; vide = bot inerte",
    },
    {
        "key": "telegram_send_timeout",
        "category": "telegram",
        "label": "Délai d'envoi Telegram (s)",
        "type": "float",
        "min": 1,
        "max": 300,
    },
    # --- scheduler ---
    {"key": "scheduler_enabled", "category": "scheduler", "label": "Scheduler cron actif", "type": "bool"},
    {"key": "scheduler_timezone", "category": "scheduler", "label": "Fuseau horaire", "type": "str", "max": 64},
    # --- limits ---
    {
        "key": "confirmation_ttl_hours",
        "category": "limits",
        "label": "TTL des confirmations (h)",
        "type": "float",
        "min": 1,
        "max": 8760,
    },
    {
        "key": "stuck_task_hours",
        "category": "limits",
        "label": "Déclencheur tâches bloquées (h)",
        "type": "float",
        "min": 0.1,
        "max": 168,
    },
    {"key": "web_search_max_results", "category": "limits", "label": "Résultats de recherche max", "type": "int", "min": 1, "max": 10},
    {"key": "web_search_timeout", "category": "limits", "label": "Timeout recherche (s)", "type": "float", "min": 1, "max": 120},
    {"key": "web_fetch_timeout", "category": "limits", "label": "Timeout fetch (s)", "type": "float", "min": 1, "max": 120},
    {"key": "web_fetch_max_bytes", "category": "limits", "label": "Fetch taille max (octets)", "type": "int", "min": 1_000, "max": 10_000_000},
    {"key": "web_fetch_max_chars", "category": "limits", "label": "Fetch texte max (car.)", "type": "int", "min": 1_000, "max": 100_000},
    {"key": "files_max_bytes", "category": "limits", "label": "Fichier sandbox max (octets)", "type": "int", "min": 1_000, "max": 10_000_000},
    {"key": "files_max_entries", "category": "limits", "label": "Entrées listing max", "type": "int", "min": 10, "max": 10_000},
    # --- models ---
    {"key": "llm_max_attempts", "category": "models", "label": "Tentatives par endpoint", "type": "int", "min": 1, "max": 10},
    {"key": "llm_backoff_base", "category": "models", "label": "Backoff LLM (base)", "type": "float", "min": 1, "max": 10},
    {"key": "llm_cooldown_seconds", "category": "models", "label": "Cooldown endpoint (s)", "type": "float", "min": 1, "max": 3600},
    {
        "key": "llm_tool_choice",
        "category": "models",
        "label": "tool_choice",
        "type": "choice",
        "choices": ["required", "auto", "none"],
    },
    # --- workflows ---
    {"key": "workflow_retry_limit", "category": "workflows", "label": "Retries par étape", "type": "int", "min": 0, "max": 10},
    {"key": "workflow_retry_backoff_base", "category": "workflows", "label": "Backoff retries (base)", "type": "float", "min": 1, "max": 10},
    # --- health ---
    {"key": "health_host", "category": "health", "label": "Interface d'écoute", "type": "str", "max": 64},
    {"key": "health_port", "category": "health", "label": "Port", "type": "int", "min": 1, "max": 65535},
]

SPECS_BY_KEY: dict[str, dict[str, Any]] = {spec["key"]: spec for spec in SETTINGS_SPECS}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def coerce_value(spec: dict[str, Any], value: Any) -> Any:
    """Validate + normalize one schema value, raising ValueError on bad input."""
    kind = spec["type"]
    if kind == "secret":
        raise ValueError("secret settings are managed through /api/secrets/set")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{spec['key']} must be true or false")
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError(f"{spec['key']} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['key']} must be an integer") from None
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{spec['key']} must be an integer")
        _check_range(spec, number)
        return number
    if kind == "float":
        if isinstance(value, bool):
            raise ValueError(f"{spec['key']} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['key']} must be a number") from None
        _check_range(spec, number)
        return number
    if kind == "choice":
        text = str(value).strip()
        if text not in spec["choices"]:
            allowed = ", ".join(spec["choices"])
            raise ValueError(f"{spec['key']} must be one of: {allowed}")
        return text
    if kind == "list[int]":
        items = value
        if isinstance(value, str):
            items = [part for part in value.replace(";", ",").split(",") if part.strip()]
        if not isinstance(items, list):
            raise ValueError(f"{spec['key']} must be a list of Telegram user ids")
        ids: list[int] = []
        for item in items:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                raise ValueError(f"{spec['key']} must contain only integers") from None
        if len(ids) > 1000:
            raise ValueError(f"{spec['key']} accepts at most 1000 ids")
        return ids
    # str
    if isinstance(value, (int, float, bool)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"{spec['key']} must be a string")
    limit = int(spec.get("max") or 512)
    if len(text) > limit:
        raise ValueError(f"{spec['key']} must be at most {limit} chars")
    return text


def _check_range(spec: dict[str, Any], number: float) -> None:
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and number < minimum:
        raise ValueError(f"{spec['key']} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{spec['key']} must be <= {maximum}")


def validate_setting(key: str, value: Any) -> tuple[str, Any, str] | None:
    """Return (coerced_value, category) for a schema key, or None if unknown."""
    spec = SPECS_BY_KEY.get(key)
    if spec is None:
        return None
    return coerce_value(spec, value), spec["category"]


def apply_db_settings(storage: Any, settings: Any) -> tuple[list[str], list[str]]:
    """Copy stored schema overrides onto the Settings object (boot time).

    Returns ``(applied_keys, warnings)``; invalid stored values are skipped
    with a warning instead of crashing the boot.
    """
    applied: list[str] = []
    warnings: list[str] = []
    for spec in SETTINGS_SPECS:
        key = spec["key"]
        if spec["type"] == "secret":
            continue
        try:
            stored = storage.get_setting(key)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"{key}: unreadable ({exc})")
            continue
        if stored is None:
            continue
        try:
            value = coerce_value(spec, stored)
        except ValueError as exc:
            warnings.append(f"{key}: {exc}")
            continue
        setattr(settings, key, value)
        applied.append(key)
    return applied, warnings


def describe_schema(service: Any, settings: Any) -> list[dict[str, Any]]:
    """Schema entries with defaults (env included) and stored overrides."""
    rows: list[dict[str, Any]] = []
    for spec in SETTINGS_SPECS:
        key = spec["key"]
        stored = service.get(key, MISSING)
        override = stored is not MISSING
        default = getattr(settings, key, None) if spec["type"] != "secret" else None
        rows.append(
            {
                "key": key,
                "category": spec["category"],
                "label": spec["label"],
                "type": spec["type"],
                "help": spec.get("help", ""),
                "choices": list(spec.get("choices", ())),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "restart": True,
                "secret": spec["type"] == "secret",
                "default": default,
                "value": stored if override else default,
                "override": override,
            }
        )
    return rows
