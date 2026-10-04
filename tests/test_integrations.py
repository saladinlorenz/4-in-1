from __future__ import annotations

import httpx

from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth, IntegrationTester

from .test_api import auth_session, make_runner
from .test_social_v3 import FakeHttp

TELEGRAM_TOKEN = "123456:ABCDEF-secret-token"
GITHUB_TOKEN = "ghp_abcdef123456"
SMTP_PASSWORD = "super-pass-xyz"


def make_secrets(settings, name: str) -> SecretStore:
    store = SecretStore(settings.storage_dir / name)
    return store


def test_describe_masks_secret_values(settings):
    store = make_secrets(settings, ".env.integrations")
    tester = IntegrationTester(store, None)

    rows = {row["name"]: row for row in tester.describe()}
    assert set(rows) == {"telegram", "ddgs", "smtp", "github"}
    assert rows["telegram"]["configured"] is False
    assert rows["github"]["configured"] is False
    assert rows["smtp"]["configured"] is False
    assert rows["ddgs"]["configured"] is True
    assert rows["ddgs"]["status"] == "aucune clé requise"

    store.set("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    store.set("GITHUB_TOKEN", GITHUB_TOKEN)
    store.set("SMTP_HOST", "smtp.example.com")
    store.set("SMTP_PASSWORD", SMTP_PASSWORD)
    rows = {row["name"]: row for row in tester.describe()}
    assert rows["telegram"]["configured"] is True
    assert rows["github"]["configured"] is True
    assert rows["smtp"]["configured"] is True
    assert rows["telegram"]["status"].startswith("Configured (ends ...")
    assert rows["smtp"]["status"].count("Configured") == 2
    for row in rows.values():
        assert TELEGRAM_TOKEN not in row["status"]
        assert GITHUB_TOKEN not in row["status"]
        assert SMTP_PASSWORD not in row["status"]


def test_telegram_and_github_connection_tests(settings):
    store = make_secrets(settings, ".env.integrations-tg")
    store.set("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)

    http = FakeHttp((200, {"ok": True, "result": {"username": "agentos_bot"}}))
    tester = IntegrationTester(store, None, http=http)
    assert tester.test("telegram") == "authenticated as @agentos_bot"
    assert http.requests[0].full_url.endswith("/bot" + TELEGRAM_TOKEN + "/getMe")

    failing = IntegrationTester(store, None, http=FakeHttp((404, {})))
    try:
        failing.test("telegram")
    except RuntimeError as exc:
        assert "HTTP 404" in str(exc)
        assert TELEGRAM_TOKEN not in str(exc)
    else:
        raise AssertionError("404 must raise")

    empty = IntegrationTester(make_secrets(settings, ".env.integrations-empty"), None, http=FakeHttp())
    try:
        empty.test("telegram")
    except RuntimeError as exc:
        assert "TELEGRAM_BOT_TOKEN is not configured" in str(exc)
    else:
        raise AssertionError("missing token must raise")

    github_store = make_secrets(settings, ".env.integrations-gh")
    github_store.set("GITHUB_TOKEN", GITHUB_TOKEN)
    gh_http = FakeHttp((200, {"login": "saladin"}))
    gh_tester = IntegrationTester(github_store, None, http=gh_http)
    assert gh_tester.test("github") == "authenticated as saladin"
    assert gh_http.requests[0].full_url == "https://api.github.com/user"
    assert any("Bearer" in str(value) for _, value in gh_http.requests[0].header_items())

    gh_failing = IntegrationTester(github_store, None, http=FakeHttp((401, {})))
    try:
        gh_failing.test("github")
    except RuntimeError as exc:
        assert "HTTP 401" in str(exc)
        assert GITHUB_TOKEN not in str(exc)
    else:
        raise AssertionError("401 must raise")

    no_github = IntegrationTester(make_secrets(settings, ".env.integrations-no-gh"), None)
    try:
        no_github.test("github")
    except RuntimeError as exc:
        assert "GITHUB_TOKEN is not configured" in str(exc)
    else:
        raise AssertionError("missing token must raise")


def test_smtp_handshake_never_sends_mail(settings):
    store = make_secrets(settings, ".env.integrations-smtp")
    store.set("SMTP_HOST", "smtp.example.com")
    store.set("SMTP_PORT", "587")
    store.set("SMTP_USER", "bot@example.com")
    store.set("SMTP_PASSWORD", SMTP_PASSWORD)

    created: list[tuple] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout=None):
            self.host, self.port, self.timeout = host, port, timeout
            self.ehlo_called = False
            self.login_args = None
            self.quit_called = False
            created.append(self)

        def ehlo(self):
            self.ehlo_called = True

        def login(self, user, password):
            self.login_args = (user, password)

        def quit(self):
            self.quit_called = True

    tester = IntegrationTester(store, None, smtp_factory=FakeSmtp, timeout=7)
    detail = tester.test("smtp")
    assert detail == "connected to smtp.example.com:587 (authenticated)"
    client = created[0]
    assert client.ehlo_called is True
    assert client.login_args == ("bot@example.com", SMTP_PASSWORD)
    assert client.quit_called is True
    assert client.timeout == 7

    anonymous_store = make_secrets(settings, ".env.integrations-smtp2")
    anonymous_store.set("SMTP_HOST", "smtp.example.com")
    created.clear()
    anon = IntegrationTester(anonymous_store, None, smtp_factory=FakeSmtp)
    assert anon.test("smtp") == "connected to smtp.example.com:587 (anonymous)"
    assert created[0].login_args is None

    no_host = IntegrationTester(make_secrets(settings, ".env.integrations-no-smtp"), None)
    try:
        no_host.test("smtp")
    except RuntimeError as exc:
        assert "SMTP_HOST is not configured" in str(exc)
    else:
        raise AssertionError("missing host must raise")

    bad_port_store = make_secrets(settings, ".env.integrations-bad-port")
    bad_port_store.set("SMTP_HOST", "smtp.example.com")
    bad_port_store.set("SMTP_PORT", "not-a-port")
    try:
        IntegrationTester(bad_port_store, None, smtp_factory=FakeSmtp).test("smtp")
    except RuntimeError as exc:
        assert "SMTP_PORT" in str(exc)
    else:
        raise AssertionError("bad port must raise")


def test_ddgs_connection_test(settings):
    store = make_secrets(settings, ".env.integrations-ddgs")
    tester = IntegrationTester(
        store, None, search_fn=lambda query, count: [{"title": "hit"}]
    )
    assert tester.test("ddgs") == "search ok (1 result)"

    empty = IntegrationTester(store, None, search_fn=lambda query, count: [])
    try:
        empty.test("ddgs")
    except RuntimeError as exc:
        assert "no results" in str(exc)
    else:
        raise AssertionError("empty search must raise")

    try:
        tester.test("nonsense")
    except ValueError as exc:
        assert "unknown integration" in str(exc)
    else:
        raise AssertionError("unknown kind must raise")


def make_server(settings, storage, notifier, tester):
    runner = make_runner(settings, storage, notifier)
    secret_store = SecretStore(settings.storage_dir / ".env.runtime")
    admin_auth = AdminAuth(storage, secret_store)
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(runner, storage, auth=admin_auth, integrations=tester),
        auth=admin_auth,
    )
    server.start()
    return server, runner


def test_integration_test_endpoints(settings, storage, notifier):
    store = make_secrets(settings, ".env.integrations-api")
    store.set("GITHUB_TOKEN", GITHUB_TOKEN)
    tester = IntegrationTester(store, None, http=FakeHttp((200, {"login": "saladin"})))

    server, runner = make_server(settings, storage, notifier, tester)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        listing = httpx.get(base + "/api/integrations", timeout=5)
        assert listing.status_code == 200
        assert {row["name"] for row in listing.json()["items"]} == {
            "telegram",
            "ddgs",
            "smtp",
            "github",
        }

        ok = httpx.post(
            base + "/api/integrations/test",
            json={"kind": "github"},
            headers=headers,
            timeout=5,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True, "detail": "authenticated as saladin"}
        assert GITHUB_TOKEN not in ok.text

        for bad in ({"kind": "ghost"}, {"kind": ""}, {"kind": "github", "extra": 1}, {}):
            res = httpx.post(
                base + "/api/integrations/test",
                json=bad,
                headers=headers,
                timeout=5,
            )
            assert res.status_code == 400, bad

        unauth = httpx.post(
            base + "/api/integrations/test", json={"kind": "github"}, timeout=5
        )
        assert unauth.status_code == 401

        assert "integration.test" in [
            row["action"] for row in storage.recent_audit(20)
        ]
    finally:
        server.stop()
        runner.shutdown()
