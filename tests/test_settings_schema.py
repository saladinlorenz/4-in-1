from __future__ import annotations

import httpx

from config import Settings
from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import (
    AdminAuth,
    SettingsService,
    apply_db_settings,
    describe_schema,
    validate_setting,
)

from .test_api import auth_session, make_runner


def make_server(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    secret_store = SecretStore(settings.storage_dir / ".env.runtime")
    auth = AdminAuth(storage, secret_store)
    service = SettingsService(storage, actor="dashboard")
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(
            runner,
            storage,
            settings_service=service,
            secret_store=secret_store,
            auth=auth,
            app_settings=settings,
        ),
        auth=auth,
    )
    server.start()
    return server, runner


def test_schema_endpoint_lists_spec_with_defaults(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(base + "/api/settings/schema", timeout=5).status_code == 401

        headers = auth_session(base)
        res = httpx.get(base + "/api/settings/schema", headers=headers, timeout=5)
        assert res.status_code == 200, res.text
        rows = {row["key"]: row for row in res.json()["items"]}

        enabled = rows["scheduler_enabled"]
        assert enabled["category"] == "scheduler"
        assert enabled["type"] == "bool"
        assert enabled["default"] is True
        assert enabled["value"] is True
        assert enabled["override"] is False
        assert enabled["restart"] is True

        assert rows["log_level"]["choices"] == [
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ]
        assert rows["health_port"]["max"] == 65535
        token = rows["telegram_bot_token"]
        assert token["secret"] is True
        assert token["status"] == "absent"
        assert token["value"] is None
        assert all(row["restart"] for row in rows.values())
    finally:
        server.stop()
        runner.shutdown()


def test_schema_values_are_validated_and_coerced(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        def post(key, value, category):
            return httpx.post(
                base + "/api/settings",
                json={"key": key, "value": value, "category": category},
                headers=headers,
                timeout=5,
            )

        assert post("scheduler_enabled", "false", "scheduler").status_code == 200
        assert post("scheduler_enabled", "nope", "scheduler").status_code == 400
        assert post("log_level", "TRACE", "general").status_code == 400
        assert post("log_level", "DEBUG", "general").status_code == 200
        assert post("confirmation_ttl_hours", 0, "limits").status_code == 400
        assert post("confirmation_ttl_hours", 48, "limits").status_code == 200
        assert post("health_port", 70000, "health").status_code == 400
        assert post("telegram_allowed_user_ids", "1, 42", "telegram").status_code == 200
        secret = post("telegram_bot_token", "x", "telegram")
        assert secret.status_code == 400
        assert "secrets/set" in secret.json()["error"]

        res = httpx.get(base + "/api/settings/schema", headers=headers, timeout=5)
        rows = {row["key"]: row for row in res.json()["items"]}
        assert rows["scheduler_enabled"]["value"] is False
        assert rows["scheduler_enabled"]["override"] is True
        assert rows["log_level"]["value"] == "DEBUG"
        assert rows["confirmation_ttl_hours"]["value"] == 48.0
        assert rows["telegram_allowed_user_ids"]["value"] == [1, 42]

        actions = [row["action"] for row in storage.recent_audit(30)]
        assert "settings.set:scheduler_enabled" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_apply_db_settings_merges_at_boot(settings, storage):
    storage.set_setting(
        "scheduler_enabled", False, category="scheduler", updated_by="t"
    )
    storage.set_setting(
        "scheduler_timezone", "Europe/Paris", category="scheduler", updated_by="t"
    )
    storage.set_setting(
        "telegram_allowed_user_ids", [7, 8], category="telegram", updated_by="t"
    )
    storage.set_setting("web_search_timeout", 3.5, category="limits", updated_by="t")
    storage.set_setting("log_level", "TRACE", category="general", updated_by="t")

    fresh = Settings(_env_file=None)
    applied, warnings = apply_db_settings(storage, fresh)

    assert fresh.scheduler_enabled is False
    assert fresh.scheduler_timezone == "Europe/Paris"
    assert fresh.telegram_allowed_user_ids == [7, 8]
    assert fresh.web_search_timeout == 3.5
    assert fresh.log_level == "INFO"  # invalid override skipped, default kept
    assert "scheduler_enabled" in applied
    assert "log_level" not in applied
    assert any("log_level" in warning for warning in warnings)


def test_validate_setting_helper():
    assert validate_setting("unknown_key", 1) is None
    assert validate_setting("scheduler_enabled", "yes") == (True, "scheduler")
    assert validate_setting("telegram_allowed_user_ids", "42") == ([42], "telegram")
    for bad in ("nope", 2.5, None):
        try:
            validate_setting("scheduler_enabled", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be refused")
    try:
        validate_setting("llm_max_attempts", 0)
    except ValueError as exc:
        assert ">= 1" in str(exc)
    else:
        raise AssertionError("range must be enforced")


def test_describe_schema_reports_overrides(settings, storage):
    service = SettingsService(storage, actor="dashboard")
    service.set("scheduler_timezone", "UTC2", category="scheduler")
    rows = {row["key"]: row for row in describe_schema(service, settings)}
    assert rows["scheduler_timezone"]["value"] == "UTC2"
    assert rows["scheduler_timezone"]["override"] is True
    assert rows["scheduler_enabled"]["override"] is False
    assert rows["scheduler_enabled"]["value"] == settings.scheduler_enabled
