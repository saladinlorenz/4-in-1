from __future__ import annotations

import httpx

from agent.permissions import ALLOWED_TOOL_NAMES
from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth, AgentConfigService, SettingsService

from .test_api import PASSWORD, auth_session, make_runner


def make_server(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    secret_store = SecretStore(settings.storage_dir / ".env.runtime")
    settings_service = SettingsService(storage, actor="dashboard")
    agent_config = AgentConfigService(settings_service, settings)
    admin_auth = AdminAuth(storage, secret_store)
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(
            runner,
            storage,
            settings_service=settings_service,
            secret_store=secret_store,
            agent_config=agent_config,
            auth=admin_auth,
        ),
        auth=admin_auth,
    )
    server.start()
    return server, runner, secret_store


def login(base: str) -> dict[str, str]:
    res = httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5)
    assert res.status_code == 200, res.text
    token = res.cookies.get("agentos_session")
    assert token
    return {
        "Cookie": f"agentos_session={token}",
        "X-CSRF-Token": res.json()["csrf_token"],
    }


def test_sessions_listing_and_revocation(settings, storage, notifier):
    server, runner, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"

        denied = httpx.get(base + "/api/security", timeout=5)
        assert denied.status_code == 401

        first = auth_session(base)
        second = login(base)

        listing = httpx.get(base + "/api/security", headers=first, timeout=5)
        assert listing.status_code == 200, listing.text
        sessions = listing.json()["sessions"]
        assert len(sessions) == 2
        for session in sessions:
            assert set(session) == {"id", "created_at", "expires_at"}
        first_id, second_id = sessions[0]["id"], sessions[1]["id"]

        revoke = httpx.post(
            base + "/api/security/sessions/revoke",
            json={"id": first_id},
            headers=second,
            timeout=5,
        )
        assert revoke.status_code == 200, revoke.text
        assert revoke.json() == {"ok": True}

        after = httpx.get(base + "/api/security", headers=second, timeout=5)
        assert [s["id"] for s in after.json()["sessions"]] == [second_id]

        dead = httpx.get(base + "/api/security", headers=first, timeout=5)
        assert dead.status_code == 401

        again = httpx.post(
            base + "/api/security/sessions/revoke",
            json={"id": first_id},
            headers=second,
            timeout=5,
        )
        assert again.json() == {"ok": False}

        for bad in ({}, {"id": "abc"}, {"id": 0}):
            res = httpx.post(
                base + "/api/security/sessions/revoke",
                json=bad,
                headers=second,
                timeout=5,
            )
            assert res.status_code == 400, bad

        unauth = httpx.post(
            base + "/api/security/sessions/revoke", json={"id": second_id}, timeout=5
        )
        assert unauth.status_code == 401

        actions = [row["action"] for row in storage.recent_audit(30)]
        assert "auth.session_revoke" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_permissions_are_served_and_immutable(settings, storage, notifier):
    server, runner, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        sec = httpx.get(base + "/api/security", headers=headers, timeout=5).json()
        assert sec["tools"] == sorted(ALLOWED_TOOL_NAMES)
        assert "shell" not in sec["tools"]
        assert set(sec["dry_run_blocked"]) <= set(sec["tools"])

        refused = httpx.post(
            base + "/api/agent",
            json={"tools": ["web_search", "not_a_tool"]},
            headers=headers,
            timeout=5,
        )
        assert refused.status_code == 400
        assert "unknown tools" in refused.json()["error"]

        saved = httpx.post(
            base + "/api/agent",
            json={"tools": ["web_search"]},
            headers=headers,
            timeout=5,
        )
        assert saved.status_code == 200, saved.text

        tampered = httpx.post(
            base + "/api/settings",
            json={"key": "agent.tools", "value": ["evil_tool", "web_search"], "category": "agent"},
            headers=headers,
            timeout=5,
        )
        assert tampered.status_code == 200, tampered.text

        info = httpx.get(base + "/api/agent", headers=headers, timeout=5).json()
        assert info["settings"]["tools"] == ["web_search"]
        assert "evil_tool" not in info["available_tools"]
        assert set(info["settings"]["tools"]) <= set(ALLOWED_TOOL_NAMES)
    finally:
        server.stop()
        runner.shutdown()


def test_secret_rotation(settings, storage, notifier):
    server, runner, secret_store = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        rotated = httpx.post(
            base + "/api/secrets/rotate",
            json={"key": "GITHUB_TOKEN", "value": "ghp_super_secret_value"},
            headers=headers,
            timeout=5,
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json() == {"ok": True}
        assert "ghp_super_secret_value" not in rotated.text
        assert secret_store.has("GITHUB_TOKEN")

        listing = httpx.get(base + "/api/secrets", headers=headers, timeout=5).json()
        item = next(i for i in listing["items"] if i["key"] == "GITHUB_TOKEN")
        assert item["status"].startswith("Configured (ends ...")

        protected = httpx.post(
            base + "/api/secrets/rotate",
            json={"key": "ADMIN_PASSWORD_HASH", "value": "x"},
            headers=headers,
            timeout=5,
        )
        assert protected.status_code == 400
        assert "cannot be rotated" in protected.json()["error"]

        unauth = httpx.post(
            base + "/api/secrets/rotate",
            json={"key": "GITHUB_TOKEN", "value": "x"},
            timeout=5,
        )
        assert unauth.status_code == 401

        actions = [row["action"] for row in storage.recent_audit(30)]
        assert "secret.rotate" in actions
    finally:
        server.stop()
        runner.shutdown()
