from __future__ import annotations

import httpx

from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth

from .conftest import FakeRouter

PASSWORD = "supersecret42"


def make_auth_server(settings, storage, notifier, **auth_kwargs):
    from agent import AgentRunner

    runner = AgentRunner(
        settings,
        storage,
        FakeRouter([]),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    secret_store = SecretStore(settings.storage_dir / ".env.runtime")
    auth = AdminAuth(
        storage, secret_store, max_failures=3, lockout_seconds=60, **auth_kwargs
    )
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(runner, storage),
        auth=auth,
    )
    server.start()
    return server, runner, auth, secret_store


def test_setup_login_and_write_gating(settings, storage, notifier):
    server, runner, _auth, _ = make_auth_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"

        early = httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5)
        assert early.status_code == 403
        assert early.json() == {"error": "not_configured"}

        weak = httpx.post(base + "/api/setup", json={"password": "short"}, timeout=5)
        assert weak.status_code == 400

        setup = httpx.post(base + "/api/setup", json={"password": PASSWORD}, timeout=5)
        assert setup.status_code == 200
        again = httpx.post(base + "/api/setup", json={"password": PASSWORD}, timeout=5)
        assert again.status_code == 403
        assert again.json() == {"error": "already_configured"}

        wrong = httpx.post(base + "/api/login", json={"password": "nope"}, timeout=5)
        assert wrong.status_code == 401

        login = httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5)
        assert login.status_code == 200
        token = login.cookies.get("agentos_session")
        csrf = login.json()["csrf_token"]
        assert token and csrf
        assert PASSWORD not in login.text

        read = httpx.get(base + "/api/tasks", headers={"Cookie": f"agentos_session={token}"}, timeout=5)
        assert read.status_code == 200

        denied = httpx.post(base + "/api/tasks/1/cancel", timeout=5)
        assert denied.status_code == 401

        no_csrf = httpx.post(
            base + "/api/tasks/1/cancel",
            headers={"Cookie": f"agentos_session={token}"},
            timeout=5,
        )
        assert no_csrf.status_code == 403

        bad_csrf = httpx.post(
            base + "/api/tasks/1/cancel",
            headers={"Cookie": f"agentos_session={token}", "X-CSRF-Token": "wrong"},
            timeout=5,
        )
        assert bad_csrf.status_code == 403

        allowed = httpx.post(
            base + "/api/tasks/1/cancel",
            headers={
                "Cookie": f"agentos_session={token}",
                "X-CSRF-Token": csrf,
            },
            timeout=5,
        )
        assert allowed.status_code == 200
        assert allowed.json()["reason"] == "unknown_task"

        logout = httpx.post(
            base + "/api/logout",
            headers={
                "Cookie": f"agentos_session={token}",
                "X-CSRF-Token": csrf,
            },
            timeout=5,
        )
        assert logout.status_code == 200

        after = httpx.post(
            base + "/api/tasks/1/cancel",
            headers={
                "Cookie": f"agentos_session={token}",
                "X-CSRF-Token": csrf,
            },
            timeout=5,
        )
        assert after.status_code == 401
    finally:
        server.stop()
        runner.shutdown()


def test_login_rate_limit_and_audit(settings, storage, notifier):
    server, runner, _auth, _ = make_auth_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        httpx.post(base + "/api/setup", json={"password": PASSWORD}, timeout=5)

        for _ in range(3):
            assert (
                httpx.post(base + "/api/login", json={"password": "bad"}, timeout=5).status_code
                == 401
            )
        locked = httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5)
        assert locked.status_code == 429
        assert locked.json() == {"error": "rate_limited"}

        actions = [row["action"] for row in storage.recent_audit(20)]
        assert actions.count("auth.login_failed") == 3
        assert "auth.setup" in actions
        assert "auth.login" not in actions
    finally:
        server.stop()
        runner.shutdown()


def test_secrets_never_returned_and_hashed(settings, storage, notifier):
    server, runner, auth, secret_store = make_auth_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        bodies = []
        bodies.append(
            httpx.post(base + "/api/setup", json={"password": PASSWORD}, timeout=5).text
        )
        bodies.append(
            httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5).text
        )
        bodies.append(httpx.get(base + "/api/status", timeout=5).text)
        for body in bodies:
            assert PASSWORD not in body

        stored = secret_store.path.read_text(encoding="utf-8")
        assert PASSWORD not in stored
        assert "pbkdf2_sha256$" in stored
        assert auth.password_configured() is True
    finally:
        server.stop()
        runner.shutdown()
