from __future__ import annotations

import httpx

from config.secrets import SecretStore
from freellmapi_adapter import LLMRouter
from services import AdminAuth, LLMConfigService, SettingsService

from .test_api import auth_session, make_runner

API_KEY = "sk-live-ABCD1234"


def ok_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})


def make_server(settings, storage, notifier, handler=ok_transport):
    from dashboard import HealthServer, LocalApi

    runner = make_runner(settings, storage, notifier)
    router = LLMRouter([], transport=httpx.MockTransport(handler), attempts=1)
    secrets = SecretStore(settings.storage_dir / ".env.runtime")
    settings_service = SettingsService(storage)
    llm_config = LLMConfigService(settings_service, secrets, router)
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(
            runner,
            storage,
            settings_service=settings_service,
            secret_store=secrets,
            llm_config=llm_config,
        ),
        auth=AdminAuth(storage, secrets),
    )
    server.start()
    return server, runner, router


def test_settings_crud_requires_session(settings, storage, notifier):
    server, runner, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.post(
            base + "/api/settings",
            json={"key": "instance_name", "value": "Home"},
            timeout=5,
        ).status_code == 401

        headers = auth_session(base)
        done = httpx.post(
            base + "/api/settings",
            json={"key": "instance_name", "value": "Home", "category": "general"},
            headers=headers,
            timeout=5,
        )
        assert done.status_code == 200

        listing = httpx.get(base + "/api/settings?category=general", headers=headers, timeout=5)
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert any(item["key"] == "instance_name" for item in items)

        bad = httpx.post(
            base + "/api/settings",
            json={"key": "bad key!", "value": 1},
            headers=headers,
            timeout=5,
        )
        assert bad.status_code == 400
        wrong_cat = httpx.post(
            base + "/api/settings",
            json={"key": "x", "value": 1, "category": "nope"},
            headers=headers,
            timeout=5,
        )
        assert wrong_cat.status_code == 400

        removed = httpx.post(
            base + "/api/settings/delete",
            json={"key": "instance_name"},
            headers=headers,
            timeout=5,
        )
        assert removed.json() == {"ok": True}
    finally:
        server.stop()
        runner.shutdown()


def test_secrets_api_never_returns_values(settings, storage, notifier):
    server, runner, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        value = "ghp_test1234"

        unauth = httpx.post(
            base + "/api/secrets/set", json={"key": "GITHUB_TOKEN", "value": value}, timeout=5
        )
        assert unauth.status_code == 401

        done = httpx.post(
            base + "/api/secrets/set",
            json={"key": "GITHUB_TOKEN", "value": value},
            headers=headers,
            timeout=5,
        )
        assert done.status_code == 200

        listing = httpx.get(base + "/api/secrets", headers=headers, timeout=5)
        assert listing.status_code == 200
        assert value not in listing.text
        item = next(i for i in listing.json()["items"] if i["key"] == "GITHUB_TOKEN")
        assert item["status"].startswith("Configured (ends ...")
        assert "test1234" not in item["status"]  # only the last 4 chars are shown

        removed = httpx.post(
            base + "/api/secrets/delete",
            json={"key": "GITHUB_TOKEN"},
            headers=headers,
            timeout=5,
        )
        assert removed.json() == {"ok": True}
        listing2 = httpx.get(base + "/api/secrets", headers=headers, timeout=5)
        assert '"absent"' in listing2.text
    finally:
        server.stop()
        runner.shutdown()


def test_llm_endpoints_api_crud_hot_reload(settings, storage, notifier):
    server, runner, router = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        denied = httpx.post(
            base + "/api/llm/endpoints",
            json={"base_url": "https://a.example/v1"},
            timeout=5,
        )
        assert denied.status_code == 401

        created = httpx.post(
            base + "/api/llm/endpoints",
            json={"base_url": "https://a.example/v1", "model": "m1", "api_key": API_KEY},
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text
        endpoint = created.json()["endpoint"]
        assert endpoint["id"] == 1
        assert endpoint["api_key_configured"] is True
        assert API_KEY not in created.text
        assert [e.model for e in router.endpoints] == ["m1"]  # live reload

        listing = httpx.get(base + "/api/llm/endpoints", headers=headers, timeout=5)
        assert listing.status_code == 200
        assert API_KEY not in listing.text
        assert len(listing.json()["items"]) == 1
        assert len(listing.json()["health"]) == 1

        updated = httpx.post(
            base + "/api/llm/endpoints/update",
            json={"id": 1, "model": "m1-v2", "priority": 5},
            headers=headers,
            timeout=5,
        )
        assert updated.status_code == 200
        assert router.endpoints[0].model == "m1-v2"
        assert router.endpoints[0].priority == 5

        tested = httpx.post(
            base + "/api/llm/endpoints/test",
            json={"id": 1},
            headers=headers,
            timeout=5,
        )
        assert tested.json()["ok"] is True

        reloaded = httpx.post(
            base + "/api/llm/reload", json={}, headers=headers, timeout=5
        )
        assert reloaded.json() == {"ok": True, "count": 1}

        invalid = httpx.post(
            base + "/api/llm/endpoints",
            json={"base_url": "not-a-url"},
            headers=headers,
            timeout=5,
        )
        assert invalid.status_code == 400

        missing = httpx.post(
            base + "/api/llm/endpoints/delete",
            json={"id": 99},
            headers=headers,
            timeout=5,
        )
        assert missing.status_code == 400

        deleted = httpx.post(
            base + "/api/llm/endpoints/delete",
            json={"id": 1},
            headers=headers,
            timeout=5,
        )
        assert deleted.json() == {"ok": True}
        assert router.endpoints == []
        assert httpx.get(
            base + "/api/llm/endpoints", headers=headers, timeout=5
        ).json()["items"] == []
    finally:
        server.stop()
        runner.shutdown()
