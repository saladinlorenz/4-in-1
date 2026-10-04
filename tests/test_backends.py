from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from agent import AgentRunner, SmolagentsBackend, describe_backends, get_backend
from agent.backends import AgentBackend, register_backend, unregister_backend
from services import AgentConfigService, SettingsService

from .conftest import FakeRouter
from .test_agent_config import make_server as make_agent_server
from .test_api import auth_session


class StubAgent:
    interrupt_switch = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "stub answer"


class StubBackend(AgentBackend):
    name = "stub"
    instances: ClassVar[list[StubAgent]] = []

    def build(self, runner) -> StubAgent:
        agent = StubAgent()
        StubBackend.instances.append(agent)
        return agent


class BrokenBackend(AgentBackend):
    name = "broken"

    def availability(self) -> tuple[bool, str]:
        return False, "not installed"

    def build(self, runner) -> object:
        raise AssertionError("an unavailable backend must never build")


def make_config(settings, storage) -> AgentConfigService:
    return AgentConfigService(SettingsService(storage), settings)


def test_registry_exposes_smolagents():
    backend = get_backend("smolagents")
    assert isinstance(backend, SmolagentsBackend)
    assert backend.availability() == (True, "")
    assert get_backend("does-not-exist") is None
    rows = describe_backends(selected="smolagents")
    by_name = {row["name"]: row for row in rows}
    assert by_name["smolagents"]["available"] is True
    assert by_name["smolagents"]["selected"] is True


def test_smolclaw_registered_but_never_activable(settings, storage):
    backend = get_backend("smolclaw")
    assert backend is not None
    ok, reason = backend.availability()
    assert ok is False
    assert "Bun" in reason and "MIT" in reason

    rows = {row["name"]: row for row in describe_backends(selected="smolagents")}
    assert rows["smolclaw"]["available"] is False
    assert rows["smolclaw"]["selected"] is False
    assert rows["smolclaw"]["reason"]

    config = make_config(settings, storage)
    try:
        config.update({"backend": "smolclaw"})
    except ValueError:
        pass
    else:
        raise AssertionError("smolclaw must not be selectable")
    assert config.effective()["backend"] == "smolagents"

    # even a tampered database value can never activate it
    settings_service = SettingsService(storage)
    settings_service.set("agent.backend", "smolclaw", category="agent")
    assert config.effective()["backend"] == "smolagents"
    with pytest.raises(RuntimeError):
        backend.build(runner=None)  # type: ignore[arg-type]


def test_update_selects_backend_and_persists(settings, storage):
    config = make_config(settings, storage)
    assert config.update({"backend": "smolagents"})["backend"] == "smolagents"

    try:
        register_backend(BrokenBackend())
        for payload in ({"backend": "ghost"}, {"backend": "broken"}, {"backend": 42}):
            try:
                config.update(payload)
            except ValueError:
                continue
            raise AssertionError(f"payload should be rejected: {payload}")
        assert config.effective()["backend"] == "smolagents"
    finally:
        unregister_backend("broken")

    actions = [row["action"] for row in storage.recent_audit(30)]
    assert "settings.set:agent.backend" in actions


def test_hostile_backend_value_falls_back(settings, storage):
    settings_service = SettingsService(storage)
    settings_service.set("agent.backend", "ghost", category="agent")
    config = AgentConfigService(settings_service, settings)
    assert config.effective()["backend"] == "smolagents"
    describe = config.describe()
    names = [row["name"] for row in describe["backends"]]
    assert names == sorted(["smolagents", "smolclaw"])


def test_runner_uses_selected_backend(settings, storage, notifier):
    config = make_config(settings, storage)
    register_backend(StubBackend())
    try:
        config.update({"backend": "stub"})
        runner = AgentRunner(
            settings, storage, FakeRouter([]), notifier, agent_config=config
        )
        try:
            task_id = runner.submit("hello backend")
            assert runner.wait_idle(timeout=10)
            task = storage.get_task(task_id)
            assert task["status"] == "SUCCESS"
            assert task["result"] == "stub answer"
            assert StubBackend.instances[-1].prompts == ["hello backend"]
        finally:
            runner.shutdown()
    finally:
        unregister_backend("stub")


def test_runner_refuses_ghost_backend(settings, storage, notifier):
    class GhostConfig:
        def effective(self):
            return {"backend": "ghost"}

    runner = AgentRunner(
        settings, storage, FakeRouter([]), notifier, agent_config=GhostConfig()
    )
    try:
        try:
            runner._build_backend_agent()
        except RuntimeError as exc:
            assert "ghost" in str(exc)
        else:
            raise AssertionError("ghost backend must be refused")
    finally:
        runner.shutdown()


def test_agent_api_exposes_and_validates_backends(settings, storage, notifier):
    server, runner = make_agent_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        info = httpx.get(base + "/api/agent", timeout=5)
        assert info.status_code == 200
        backends = {row["name"]: row for row in info.json()["backends"]}
        assert backends["smolagents"]["available"] is True
        assert backends["smolagents"]["selected"] is True
        assert backends["smolclaw"]["available"] is False
        assert backends["smolclaw"]["reason"]

        headers = auth_session(base)
        saved = httpx.post(
            base + "/api/agent",
            json={"backend": "smolagents", "max_steps": 6},
            headers=headers,
            timeout=5,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["settings"]["backend"] == "smolagents"

        invalid = httpx.post(
            base + "/api/agent",
            json={"backend": "smolclaw"},
            headers=headers,
            timeout=5,
        )
        assert invalid.status_code == 400
        assert "not integrated" in invalid.json()["error"]

        try:
            register_backend(BrokenBackend())
            refused = httpx.post(
                base + "/api/agent",
                json={"backend": "broken"},
                headers=headers,
                timeout=5,
            )
            assert refused.status_code == 400
        finally:
            unregister_backend("broken")
    finally:
        server.stop()
        runner.shutdown()
