from __future__ import annotations

import httpx

from agent import ALLOWED_TOOL_NAMES, AgentRunner
from config.secrets import SecretStore
from services import AdminAuth, AgentConfigService, SettingsService

from .conftest import FakeRouter
from .test_api import auth_session, make_runner

CUSTOM_PROMPT = "Réponds toujours en français."


def make_config(settings, storage) -> AgentConfigService:
    return AgentConfigService(SettingsService(storage), settings)


def test_defaults_without_overrides(settings, storage):
    config = make_config(settings, storage)
    effective = config.effective()
    assert effective["max_steps"] == settings.agent_max_steps
    assert effective["max_output_chars"] == settings.agent_max_output_chars
    assert effective["system_prompt"] == ""
    assert effective["temperature"] == 0.3
    assert effective["dry_run"] is False
    assert effective["tools"] is None
    assert config.allowed_tools() == frozenset(ALLOWED_TOOL_NAMES)


def test_update_persists_and_merges(settings, storage):
    config = make_config(settings, storage)
    effective = config.update(
        {
            "system_prompt": CUSTOM_PROMPT,
            "max_steps": 7,
            "max_output_chars": 900,
            "temperature": 0.9,
            "dry_run": True,
            "tools": ["web_search", "read_file"],
        }
    )
    assert effective["system_prompt"] == CUSTOM_PROMPT
    assert effective["max_steps"] == 7
    assert effective["dry_run"] is True
    assert effective["tools"] == ["web_search", "read_file"]

    # a fresh service instance over the same SQLite sees the same config
    fresh = make_config(settings, storage)
    assert fresh.effective() == effective

    actions = [row["action"] for row in storage.recent_audit(20)]
    assert "agent.settings.update" in actions
    assert any(a.startswith("settings.set:agent.") for a in actions)


def test_validation_rejects_bad_payloads(settings, storage):
    config = make_config(settings, storage)
    bad_payloads = [
        {"max_steps": 0},
        {"max_steps": 51},
        {"temperature": 3},
        {"max_output_chars": 10},
        {"system_prompt": "x" * 5000},
        {"tools": ["execute_shell"]},
        {"tools": []},
        {"tools": "web_search"},
        {"dry_run": "yes"},
    ]
    for payload in bad_payloads:
        try:
            config.update(payload)
        except ValueError:
            continue
        raise AssertionError(f"payload should be rejected: {payload}")
    assert storage.list_settings("agent") == []


def test_dry_run_blocks_side_effect_tools(settings, storage):
    config = make_config(settings, storage)
    config.update({"dry_run": True})
    allowed = config.allowed_tools()
    for blocked in ("social_publish", "write_file", "send_notification", "workflow_run", "workflow_create"):
        assert blocked not in allowed
    assert "final_answer" in allowed
    assert "web_search" in allowed

    config.update({"dry_run": False, "tools": ["web_search", "social_publish"]})
    allowed = config.allowed_tools()
    assert "social_publish" in allowed
    assert "execute_shell" not in allowed  # never in ALLOWED in the first place


def test_hostile_stored_values_fall_back_to_defaults(settings, storage):
    settings_service = SettingsService(storage)
    settings_service.set("agent.max_steps", "abc", category="agent")
    settings_service.set("agent.temperature", "hot", category="agent")
    settings_service.set("agent.tools", "web_search", category="agent")
    settings_service.set("agent.dry_run", "yes", category="agent")
    settings_service.set("agent.system_prompt", 42, category="agent")

    config = AgentConfigService(settings_service, settings)
    effective = config.effective()
    assert effective["max_steps"] == settings.agent_max_steps
    assert effective["temperature"] == 0.3
    assert effective["tools"] is None
    assert effective["dry_run"] is False
    assert effective["system_prompt"] == ""


def test_runner_build_agent_applies_settings(settings, storage, notifier):
    config = make_config(settings, storage)
    config.update(
        {
            "system_prompt": CUSTOM_PROMPT,
            "max_steps": 5,
            "max_output_chars": 777,
            "temperature": 0.9,
            "dry_run": True,
            "tools": ["web_search", "web_fetch"],
        }
    )
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter([]),
        notifier,
        agent_config=config,
    )
    options = runner._agent_options()
    assert options["max_steps"] == 5
    assert options["max_output_chars"] == 777
    assert options["temperature"] == 0.9
    assert options["allowed_tools"] == frozenset(
        {"web_search", "web_fetch", "final_answer"}
    )

    agent = runner._build_agent()
    assert agent.max_steps == 5
    assert CUSTOM_PROMPT in agent.prompt_templates["system_prompt"]
    names = set(agent.tools)
    assert "web_search" in names
    assert "write_file" not in names  # dry-run
    assert "remember" not in names  # not selected
    assert "final_answer" in names  # structural, always present

    # runner without agent_config keeps pure .env behaviour
    plain = AgentRunner(settings, storage, FakeRouter([]), notifier)
    plain_options = plain._agent_options()
    assert plain_options["max_steps"] == settings.agent_max_steps
    assert plain_options["allowed_tools"] == frozenset(ALLOWED_TOOL_NAMES)
    assert plain_options["system_prompt"] == ""


def make_server(settings, storage, notifier):
    from dashboard import HealthServer, LocalApi

    runner = make_runner(settings, storage, notifier)
    settings_service = SettingsService(storage)
    agent_config = AgentConfigService(settings_service, settings)
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(
            runner,
            storage,
            settings_service=settings_service,
            agent_config=agent_config,
        ),
        auth=AdminAuth(storage, SecretStore(settings.storage_dir / ".env.runtime")),
    )
    server.start()
    return server, runner


def test_agent_api_http(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"

        info = httpx.get(base + "/api/agent", timeout=5)
        assert info.status_code == 200
        data = info.json()
        assert data["settings"]["max_steps"] == settings.agent_max_steps
        assert "web_search" in data["available_tools"]
        assert "final_answer" not in data["available_tools"]
        assert "execute_shell" not in data["available_tools"]
        assert "social_publish" in data["dry_run_blocked"]

        denied = httpx.post(
            base + "/api/agent", json={"max_steps": 5}, timeout=5
        )
        assert denied.status_code == 401

        headers = auth_session(base)
        saved = httpx.post(
            base + "/api/agent",
            json={"max_steps": 5, "dry_run": True, "tools": ["web_search"]},
            headers=headers,
            timeout=5,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["settings"]["max_steps"] == 5

        invalid = httpx.post(
            base + "/api/agent",
            json={"max_steps": 999},
            headers=headers,
            timeout=5,
        )
        assert invalid.status_code == 400

        again = httpx.get(base + "/api/agent", timeout=5)
        assert again.json()["settings"]["max_steps"] == 5
        assert again.json()["settings"]["dry_run"] is True

        actions = [row["action"] for row in storage.recent_audit(30)]
        assert "agent.settings.update" in actions
    finally:
        server.stop()
        runner.shutdown()
