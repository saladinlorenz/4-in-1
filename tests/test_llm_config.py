from __future__ import annotations

import httpx
import pytest

from config.secrets import SecretStore
from config.settings import LLMEndpoint
from freellmapi_adapter import LLMRouter
from freellmapi_adapter.fallback import ErrorKind, LLMError
from services import LLMConfigService, SettingsService

API_KEY = "sk-live-ABCD1234"


def ok_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})


def build(settings, storage, handler=ok_transport):
    router = LLMRouter([], transport=httpx.MockTransport(handler), attempts=1)
    secrets = SecretStore(settings.storage_dir / ".env.runtime")
    service = LLMConfigService(SettingsService(storage), secrets, router)
    return service, router, secrets


def test_seed_moves_config_and_keys_to_sqlite(settings, storage):
    service, router, secrets = build(settings, storage)
    env = [
        LLMEndpoint(base_url="https://a.example/v1", api_key=API_KEY, model="m1"),
        LLMEndpoint(base_url="https://b.example/v1", model="m2"),
    ]
    assert service.seed(env) == 2
    assert service.seed(env) == 0

    rows = storage.list_settings("models")
    assert len(rows) == 2
    for row in rows:
        assert "api_key" not in row["value"]
        assert API_KEY not in str(row["value"])
    assert secrets.get("LLM_ENDPOINT_KEY_1") == API_KEY

    assert service.reload() == 2
    assert [endpoint.model for endpoint in router.endpoints] == ["m1", "m2"]
    assert router.endpoints[0].api_key == API_KEY

    actions = [row["action"] for row in storage.recent_audit(10)]
    assert "llm.seed" in actions


def test_create_update_delete_hot_reload(settings, storage):
    service, router, _ = build(settings, storage)

    created = service.create({"base_url": "https://a.example/v1", "model": "m1"})
    assert created["id"] == 1
    assert [endpoint.model for endpoint in router.endpoints] == ["m1"]

    created2 = service.create({"base_url": "https://b.example/v1", "model": "m2", "priority": 1})
    assert created2["id"] == 2
    assert len(router.endpoints) == 2
    # ordered by priority: endpoint 2 (priority 1) is tried first
    ordered = [endpoint.model for endpoint in router._ordered(router.endpoints)]
    assert ordered[0] == "m2"

    service.update(1, {"model": "m1-v2", "enabled": False})
    by_url = {endpoint.base_url: endpoint for endpoint in router.endpoints}
    assert by_url["https://a.example/v1"].model == "m1-v2"
    assert by_url["https://a.example/v1"].enabled is False

    assert service.delete(2) is True
    assert len(router.endpoints) == 1
    assert service.list()[0]["id"] == 1


def test_api_key_write_only_and_masked(settings, storage):
    service, _, secrets = build(settings, storage)
    public = service.create({"base_url": "https://a.example/v1", "api_key": API_KEY})

    assert "api_key" not in public
    assert public["api_key_configured"] is True
    assert public["api_key_masked"].endswith("...1234)")

    rows = storage.list_settings("models")
    assert API_KEY not in str(rows)
    raw = secrets.path.read_text(encoding="utf-8")
    assert API_KEY in raw
    assert raw.count("=") >= 1

    service.update(1, {"api_key": ""})
    assert service.list()[0]["api_key_configured"] is False
    assert secrets.has("LLM_ENDPOINT_KEY_1") is False


def test_test_endpoint_success_and_failure(settings, storage):
    service, _, _ = build(settings, storage, handler=ok_transport)
    service.create({"base_url": "https://a.example/v1"})
    result = service.test(1)
    assert result["ok"] is True
    assert result["latency_ms"] >= 0

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    service2, _, _ = build(settings, storage, handler=failing)
    service2.create({"base_url": "https://a.example/v1"})
    result2 = service2.test(1)
    assert result2["ok"] is False
    assert "boom" in result2["error"]

    actions = [row["action"] for row in storage.recent_audit(10)]
    assert "llm.endpoint.test" in actions


def test_chat_skips_disabled_and_needs_one_enabled(settings, storage):
    service, router, _ = build(settings, storage)
    service.create({"base_url": "https://a.example/v1", "model": "m1", "enabled": False})
    with pytest.raises(LLMError) as excinfo:
        router.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert excinfo.value.kind is ErrorKind.CONFIG

    service.create({"base_url": "https://b.example/v1", "model": "m2"})
    result = router.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert result["choices"]


def test_validation_rejects_bad_payloads(settings, storage):
    service, _, _ = build(settings, storage)
    with pytest.raises(ValueError):
        service.create({"model": "m1"})
    with pytest.raises(ValueError):
        service.create({"base_url": "ftp://a.example"})
    with pytest.raises(ValueError):
        service.create({"base_url": "https://a.example/v1", "timeout": 0})
    with pytest.raises(ValueError):
        service.create({"base_url": "https://a.example/v1", "priority": -1})
    with pytest.raises(ValueError):
        service.update(1, {})
    assert service.list() == []
