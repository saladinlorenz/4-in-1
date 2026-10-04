from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx

from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth
from social import BlueskyAdapter, DevtoAdapter, SocialService, TelegramAdapter

from .test_api import auth_session, make_runner
from .test_social import make_tool_deps, tool_map


class FakeHttp:
    """Scripted HTTP transport: returns queued (status, payload) responses."""

    def __init__(self, *responses: tuple[int, dict]) -> None:
        self.responses = list(responses)
        self.requests: list = []

    def __call__(self, request, *, timeout: float | None = None) -> tuple[int, bytes]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload).encode("utf-8")


def header_value(request, name: str) -> str | None:
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


def test_devto_adapter_publishes_and_tests(settings):
    store = SecretStore(settings.storage_dir / ".env.devto")
    store.set("DEVTO_API_KEY", "dvp_1234567890")

    http = FakeHttp((201, {"url": "https://dev.to/u/saladin-post-1"}))
    adapter = DevtoAdapter(store, http=http)
    receipt = adapter.publish("My great title\n\nParagraph one.\nParagraph two.")
    assert receipt == "devto:https://dev.to/u/saladin-post-1"
    assert "dvp_1234567890" not in receipt

    request = http.requests[0]
    assert request.full_url == "https://dev.to/api/articles"
    assert header_value(request, "api-key") == "dvp_1234567890"
    body = json.loads(request.data)
    assert body["article"]["title"] == "My great title"
    assert body["article"]["published"] is True
    assert "Paragraph one." in body["article"]["body_markdown"]

    ok, status = adapter.configured()
    assert ok is True
    assert "Configured (ends ...7890)" in status

    test_http = FakeHttp((200, {"username": "saladin"}))
    assert DevtoAdapter(store, http=test_http).test() == "authenticated as @saladin"

    failing = DevtoAdapter(store, http=FakeHttp((401, {})))
    try:
        failing.publish("x")
    except RuntimeError as exc:
        assert "HTTP 401" in str(exc)
    else:
        raise AssertionError("401 must raise")


def test_devto_adapter_refuses_without_key(settings):
    store = SecretStore(settings.storage_dir / ".env.devto-empty")
    adapter = DevtoAdapter(store, http=FakeHttp())
    for call in (lambda: adapter.publish("x"), adapter.test):
        try:
            call()
        except RuntimeError as exc:
            assert "DEVTO_API_KEY is not configured" in str(exc)
        else:
            raise AssertionError("missing key must raise")
    ok, status = adapter.configured()
    assert ok is False
    assert status == "DEVTO_API_KEY absente"


def test_bluesky_adapter_publishes_and_tests(settings):
    store = SecretStore(settings.storage_dir / ".env.bluesky")
    store.set("BLUESKY_HANDLE", "saladin.bsky.social")
    store.set("BLUESKY_APP_PASSWORD", "abcd-efgh-ijkl")

    http = FakeHttp(
        (200, {"accessJwt": "jwt-abc", "did": "did:plc:123", "handle": "saladin.bsky.social"}),
        (201, {"uri": "at://did:plc:123/app.bsky.feed.post/xyz"}),
    )
    adapter = BlueskyAdapter(store, http=http)
    receipt = adapter.publish("hello bluesky")
    assert receipt == "bluesky:at://did:plc:123/app.bsky.feed.post/xyz"
    assert "abcd-efgh" not in receipt

    session_request, record_request = http.requests
    assert session_request.full_url.endswith("/com.atproto.server.createSession")
    session_body = json.loads(session_request.data)
    assert session_body == {
        "identifier": "saladin.bsky.social",
        "password": "abcd-efgh-ijkl",
    }
    assert header_value(record_request, "Authorization") == "Bearer jwt-abc"
    record_body = json.loads(record_request.data)
    assert record_body["record"]["text"] == "hello bluesky"
    assert record_body["collection"] == "app.bsky.feed.post"

    test_http = FakeHttp(
        (200, {"accessJwt": "jwt-2", "did": "did:plc:123", "handle": "saladin.bsky.social"})
    )
    assert (
        BlueskyAdapter(store, http=test_http).test()
        == "authenticated as saladin.bsky.social"
    )

    try:
        BlueskyAdapter(store, http=FakeHttp()).publish("x" * 301)
    except RuntimeError as exc:
        assert "300" in str(exc)
    else:
        raise AssertionError("overlong post must raise")

    login_failure = BlueskyAdapter(
        store, http=FakeHttp((401, {"error": "AuthenticationRequired"}))
    )
    try:
        login_failure.test()
    except RuntimeError as exc:
        assert "login failed: HTTP 401" in str(exc)
    else:
        raise AssertionError("failed login must raise")

    ok, status = adapter.configured()
    assert ok is True
    assert "Configured (ends ...cial)" in status


def test_bluesky_adapter_refuses_without_credentials(settings):
    store = SecretStore(settings.storage_dir / ".env.bluesky-empty")
    adapter = BlueskyAdapter(store, http=FakeHttp())
    try:
        adapter.publish("hello")
    except RuntimeError as exc:
        assert "BLUESKY_HANDLE" in str(exc)
    else:
        raise AssertionError("missing credentials must raise")
    ok, status = adapter.configured()
    assert ok is False
    assert "BLUESKY_HANDLE" in status


def test_telegram_adapter_connection_test():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, name="test-loop-v3", daemon=True)
    thread.start()
    assert ready.wait(timeout=5)

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(username="agentos_bot")

    try:
        adapter = TelegramAdapter(FakeBot(), 42, loop, timeout=5)
        ok, _ = adapter.configured()
        assert ok is True
        assert adapter.test() == "authenticated as @agentos_bot"

        missing = TelegramAdapter(None, 0, loop, timeout=5)
        ok, detail = missing.configured()
        assert ok is False
        assert "absent" in detail
        try:
            missing.test()
        except RuntimeError as exc:
            assert "not configured" in str(exc)
        else:
            raise AssertionError("test without bot must raise")
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_service_describe_and_test(storage):
    class StubAdapter:
        platform = "devto"

        def configured(self):
            return True, "Configured (ends ...7890)"

        def test(self):
            return "authenticated as @saladin"

    class NoTestAdapter:
        platform = "telegram"

        def publish(self, content: str) -> str:
            raise AssertionError("must not publish")

    service = SocialService(storage, None, {"devto": StubAdapter(), "telegram": NoTestAdapter()})

    rows = {row["platform"]: row for row in service.describe()}
    assert rows["devto"]["configured"] is True
    assert rows["telegram"]["configured"] is True

    assert service.test("devto") == "authenticated as @saladin"
    try:
        service.test("ghost")
    except ValueError as exc:
        assert "no adapter configured" in str(exc)
    else:
        raise AssertionError("unknown platform must raise")
    try:
        service.test("telegram")
    except ValueError as exc:
        assert "no connection test" in str(exc)
    else:
        raise AssertionError("platform without test must raise")


def test_publish_without_confirmation_never_reaches_adapter(settings, storage, notifier):
    class SpyAdapter:
        platform = "telegram"

        def __init__(self) -> None:
            self.contents: list[str] = []

        def publish(self, content: str) -> str:
            self.contents.append(content)
            return "telegram:99"

    runner = make_runner(settings, storage, notifier)
    adapter = SpyAdapter()
    service = SocialService(storage, runner, {"telegram": adapter})
    runner.add_confirmation_listener(service.on_confirmation)

    draft_id = storage.create_draft("telegram", "not yet")
    tools = tool_map(make_tool_deps(settings, storage, notifier))
    refused = tools["social_publish"](draft_id=draft_id)
    assert "Publishing unavailable" in refused
    assert storage.get_draft(draft_id)["status"] == "DRAFT"
    assert adapter.contents == []

    confirmation_id = storage.create_confirmation("publish_draft", f"draft#{draft_id}")
    runner.resolve_confirmation(confirmation_id, "REJECTED")
    assert storage.get_draft(draft_id)["status"] == "DRAFT"
    assert adapter.contents == []
    runner.shutdown()


def make_server(settings, storage, notifier, service):
    runner = make_runner(settings, storage, notifier)
    auth = AdminAuth(storage, SecretStore(settings.storage_dir / ".env.runtime"))
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(runner, storage, social=service),
        auth=auth,
    )
    server.start()
    return server, runner


def test_social_api_endpoints(settings, storage, notifier):
    class StubAdapter:
        platform = "devto"

        def configured(self):
            return True, "Configured (ends ...7890)"

        def test(self):
            return "authenticated as @saladin"

    class BrokenAdapter:
        platform = "broken"

        def configured(self):
            return False, "absente"

        def test(self):
            raise RuntimeError("boom")

    service = SocialService(
        storage, None, {"devto": StubAdapter(), "broken": BrokenAdapter()}
    )
    storage.create_draft("devto", "upcoming post")

    server, runner = make_server(settings, storage, notifier, service)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        listing = httpx.get(base + "/api/social", timeout=5)
        assert listing.status_code == 200
        payload = listing.json()
        platforms = {a["platform"] for a in payload["adapters"]}
        assert platforms == {"devto", "broken"}
        assert len(payload["drafts"]) == 1
        assert payload["drafts"][0]["platform"] == "devto"

        ok = httpx.post(
            base + "/api/social/test",
            json={"platform": "devto"},
            headers=headers,
            timeout=5,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True, "detail": "authenticated as @saladin"}

        failing = httpx.post(
            base + "/api/social/test",
            json={"platform": "broken"},
            headers=headers,
            timeout=5,
        )
        assert failing.status_code == 200
        assert failing.json() == {"ok": False, "error": "boom"}

        for bad in (
            {"platform": "ghost"},
            {"platform": ""},
            {"platform": "devto", "extra": 1},
            {},
        ):
            res = httpx.post(
                base + "/api/social/test", json=bad, headers=headers, timeout=5
            )
            assert res.status_code == 400, bad

        denied = httpx.post(base + "/api/social/test", json={"platform": "devto"}, timeout=5)
        assert denied.status_code == 401

        assert "social.test" in [row["action"] for row in storage.recent_audit(20)]

        for path in ("/api/social/publish", "/api/drafts/publish"):
            res = httpx.post(base + path, json={"draft_id": 1}, headers=headers, timeout=5)
            assert res.status_code == 404, path
    finally:
        server.stop()
        runner.shutdown()
