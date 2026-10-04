from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from agent import AgentRunner
from agent.tools import build_tools
from agent.tools.base import ToolDeps
from social import SocialService, TelegramAdapter

from .conftest import FakeRouter


def make_runner(settings, storage, notifier) -> AgentRunner:
    return AgentRunner(
        settings,
        storage,
        FakeRouter(),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )


def make_tool_deps(settings, storage, notifier, request_confirmation=None) -> ToolDeps:
    return ToolDeps(
        storage=storage,
        notifier=notifier,
        status_fn=lambda: {"app": "agentos", "task_counts": {}},
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
        files_dir=settings.files_dir,
        files_max_bytes=settings.files_max_bytes,
        files_max_entries=settings.files_max_entries,
        web_fetch_max_chars=settings.web_fetch_max_chars,
        request_confirmation=request_confirmation,
    )


def tool_map(deps: ToolDeps):
    return {item.name: item for item in build_tools(deps)}


def test_create_draft_dedups_and_lists(settings, storage, notifier):
    tools = tool_map(make_tool_deps(settings, storage, notifier))

    created = tools["social_create_draft"](platform="Telegram", content="Hello world")
    assert created == "Draft #1 created for telegram (status DRAFT)."

    duplicate = tools["social_create_draft"](platform="telegram", content="Hello world")
    assert "already exists" in duplicate

    assert tools["social_create_draft"](platform="", content="x").startswith("Draft not created")
    assert tools["social_create_draft"](platform="telegram", content="").startswith(
        "Draft not created"
    )

    listing = tools["social_list_drafts"]()
    assert "#1 [DRAFT] telegram" in listing
    assert tools["social_list_drafts"](status="FAILED") == "No drafts found."


def test_publish_requires_confirmation_and_holds_task(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    task_id = storage.create_task("post the news")
    storage.mark_running(task_id)
    deps = make_tool_deps(
        settings,
        storage,
        notifier,
        request_confirmation=lambda kind, payload: runner.request_confirmation(
            task_id, kind, payload
        ),
    )
    tools = tool_map(deps)
    draft_id = storage.create_draft("telegram", "real content")

    first = tools["social_publish"](draft_id=draft_id)
    assert f"Confirmation #1 required to publish draft #{draft_id}" in first
    assert "Nothing was published" in first

    confirmation = storage.find_confirmation("publish_draft", f"draft#{draft_id}")
    assert confirmation["status"] == "PENDING"
    assert storage.get_task(task_id)["status"] == "WAITING_CONFIRMATION"

    second = tools["social_publish"](draft_id=draft_id)
    assert "Waiting for confirmation #1" in second
    assert storage.get_draft(draft_id)["status"] == "DRAFT"
    runner.shutdown()


def test_publish_only_after_approval(settings, storage, notifier):
    class FakeAdapter:
        platform = "telegram"

        def __init__(self) -> None:
            self.contents: list[str] = []

        def publish(self, content: str) -> str:
            self.contents.append(content)
            return "telegram:77"

    runner = make_runner(settings, storage, notifier)
    adapter = FakeAdapter()
    service = SocialService(storage, runner, {"telegram": adapter})
    runner.add_confirmation_listener(service.on_confirmation)

    draft_id = storage.create_draft("telegram", "real content")
    confirmation_id = storage.create_confirmation("publish_draft", f"draft#{draft_id}")

    assert storage.get_draft(draft_id)["status"] == "DRAFT"

    ok, reason = runner.resolve_confirmation(confirmation_id, "APPROVED")
    assert (ok, reason) == (True, "approved")

    draft = storage.get_draft(draft_id)
    assert draft["status"] == "PUBLISHED"
    assert draft["published_at"] is not None
    assert draft["error"] is None
    assert adapter.contents == ["real content"]
    assert any("published on telegram — telegram:77" in text for text in notifier.items)
    runner.shutdown()


def test_publish_rejected_keeps_draft(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    service = SocialService(storage, runner, {})
    runner.add_confirmation_listener(service.on_confirmation)

    draft_id = storage.create_draft("telegram", "content")
    confirmation_id = storage.create_confirmation("publish_draft", f"draft#{draft_id}")

    runner.resolve_confirmation(confirmation_id, "REJECTED")

    assert storage.get_draft(draft_id)["status"] == "DRAFT"
    assert any("was not published (rejected)" in text for text in notifier.items)
    runner.shutdown()


def test_publish_without_adapter_marks_failed(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    service = SocialService(storage, runner, {})
    runner.add_confirmation_listener(service.on_confirmation)

    draft_id = storage.create_draft("devto", "article body")
    confirmation_id = storage.create_confirmation("publish_draft", f"draft#{draft_id}")

    runner.resolve_confirmation(confirmation_id, "APPROVED")

    draft = storage.get_draft(draft_id)
    assert draft["status"] == "FAILED"
    assert draft["error"] == "no adapter configured for devto"
    assert any("NOT published" in text for text in notifier.items)
    runner.shutdown()


def test_publish_adapter_failure_marks_failed(settings, storage, notifier):
    class BrokenAdapter:
        platform = "telegram"

        def publish(self, content: str) -> str:
            raise RuntimeError("boom")

    runner = make_runner(settings, storage, notifier)
    service = SocialService(storage, runner, {"telegram": BrokenAdapter()})
    runner.add_confirmation_listener(service.on_confirmation)

    draft_id = storage.create_draft("telegram", "content")
    confirmation_id = storage.create_confirmation("publish_draft", f"draft#{draft_id}")

    runner.resolve_confirmation(confirmation_id, "APPROVED")

    draft = storage.get_draft(draft_id)
    assert draft["status"] == "FAILED"
    assert "boom" in (draft["error"] or "")
    assert any("publish FAILED" in text for text in notifier.items)
    runner.shutdown()


def test_confirmations_of_other_kinds_are_ignored(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    service = SocialService(storage, runner, {})
    runner.add_confirmation_listener(service.on_confirmation)

    confirmation_id = storage.create_confirmation("workflow_run", "workflow_run#1.step0")
    runner.resolve_confirmation(confirmation_id, "APPROVED")
    assert not notifier.items
    runner.shutdown()


def test_telegram_adapter_publishes_for_real():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, name="test-loop", daemon=True)
    thread.start()
    assert ready.wait(timeout=5)

    class FakeBot:
        async def send_message(self, chat_id, text):
            assert chat_id == 42
            return SimpleNamespace(message_id=123)

    try:
        adapter = TelegramAdapter(FakeBot(), 42, loop, timeout=5)
        assert adapter.publish("hello") == "telegram:123"

        missing_chat = TelegramAdapter(FakeBot(), 0, loop, timeout=5)
        try:
            missing_chat.publish("hello")
            raise AssertionError("publish without chat id must raise")
        except RuntimeError as exc:
            assert "telegram_admin_chat_id" in str(exc)

        empty = TelegramAdapter(FakeBot(), 42, loop, timeout=5)
        try:
            empty.publish("   ")
            raise AssertionError("publish of empty content must raise")
        except RuntimeError as exc:
            assert "empty" in str(exc)
    finally:
        loop.call_soon_threadsafe(loop.stop)
