from __future__ import annotations

import asyncio

from telegram import Message, Update
from telegram.ext import CommandHandler, ContextTypes

from agent import AgentRunner
from agent.permissions import is_authorized_user
from telegram_bot import build_application, format_status, format_tasks, register_handlers

from .conftest import FakeRouter, final_answer_payload


def find_handler(app, command: str) -> CommandHandler:
    for handler in app.handlers[0]:
        if isinstance(handler, CommandHandler) and command in handler.commands:
            return handler
    raise AssertionError(f"command /{command} not registered")


def make_update(bot, user_id: int, text: str, chat_id: int = 111) -> Update:
    return Update.de_json(
        {
            "update_id": user_id,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "tester"},
                "text": text,
            },
        },
        bot,
    )


def test_format_status_contains_endpoints_and_counts():
    text = format_status(
        {
            "app": "agentos",
            "version": "0.1.0",
            "uptime_seconds": 12.5,
            "current_task": None,
            "task_counts": {"SUCCESS": 2},
            "telegram_configured": True,
            "llm_endpoints": [{"endpoint": "host/auto", "state": "ready", "last_error": None}],
        }
    )
    assert "app: agentos 0.1.0" in text
    assert "llm: host/auto [ready]" in text
    assert "{'SUCCESS': 2}" in text


def test_format_status_without_endpoints():
    text = format_status({"app": "agentos", "version": "0.1.0", "llm_endpoints": []})
    assert "no endpoints configured" in text


def test_format_tasks_empty_and_populated():
    assert format_tasks([]) == "No tasks yet."
    text = format_tasks(
        [{"id": 3, "status": "RUNNING", "created_at": "2026-10-04", "prompt": "fais un résumé"}]
    )
    assert "#3 [RUNNING]" in text
    assert "fais un résumé" in text


def test_authorization_rules():
    assert is_authorized_user(42, set()) is False
    assert is_authorized_user(None, {42}) is False
    assert is_authorized_user(7, {42}) is False
    assert is_authorized_user(42, {42}) is True


def test_build_application_requires_token(settings):
    settings.telegram_bot_token = ""
    assert build_application(settings) is None
    settings.telegram_bot_token = "123456789:TESTTOKEN_FOR_UNIT_TESTS"
    app = build_application(settings)
    assert app is not None


def test_register_handlers_registers_all_commands(settings, storage, notifier):
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter(responses=[final_answer_payload("ok")]),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    app = build_application(settings)
    register_handlers(app, runner, storage, settings)

    for command in ("start", "help", "status", "ask", "tasks", "cancel", "approve", "reject"):
        find_handler(app, command)
    runner.shutdown()


def test_status_handler_rejects_unauthorized_user(settings, storage, notifier, monkeypatch):
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter(),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    app = build_application(settings)
    register_handlers(app, runner, storage, settings)
    handler = find_handler(app, "status")

    replies: list[str] = []

    async def fake_reply(self, text=None, *args, **kwargs):
        replies.append(str(text))

    monkeypatch.setattr(Message, "reply_text", fake_reply)
    update = make_update(app.bot, user_id=999, text="/status")
    context = ContextTypes.DEFAULT_TYPE(app)

    asyncio.run(handler.callback(update, context))
    assert replies == []
    runner.shutdown()


def test_status_and_ask_handlers_for_authorized_user(settings, storage, notifier, monkeypatch):
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter(responses=[final_answer_payload("tout va bien")]),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    app = build_application(settings)
    register_handlers(app, runner, storage, settings)

    replies: list[str] = []

    async def fake_reply(self, text=None, *args, **kwargs):
        replies.append(str(text))

    monkeypatch.setattr(Message, "reply_text", fake_reply)
    context = ContextTypes.DEFAULT_TYPE(app)

    status_handler = find_handler(app, "status")
    asyncio.run(status_handler.callback(make_update(app.bot, 42, "/status"), context))
    assert any("app: agentos" in reply for reply in replies)

    replies.clear()
    ask_handler = find_handler(app, "ask")
    context.args = ["résume", "ce", "sujet"]
    asyncio.run(ask_handler.callback(make_update(app.bot, 42, "/ask résume ce sujet"), context))
    assert any("Task #1 queued." in reply for reply in replies)

    assert runner.wait_idle(timeout=30)
    assert storage.get_task(1)["status"] == "SUCCESS"
    assert storage.get_task(1)["result"] == "tout va bien"
    runner.shutdown()


def test_approve_and_reject_handlers(settings, storage, notifier, monkeypatch):
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter(responses=[final_answer_payload("done")]),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    task_id = storage.create_task("sensitive")
    storage.mark_running(task_id)
    confirm_id = storage.create_confirmation("publish_draft", "draft#7", task_id=task_id)
    storage.hold_task(task_id)

    app = build_application(settings)
    register_handlers(app, runner, storage, settings)

    replies: list[str] = []

    async def fake_reply(self, text=None, *args, **kwargs):
        replies.append(str(text))

    monkeypatch.setattr(Message, "reply_text", fake_reply)
    context = ContextTypes.DEFAULT_TYPE(app)

    approve_handler = find_handler(app, "approve")
    context.args = [str(confirm_id)]
    asyncio.run(
        approve_handler.callback(
            make_update(app.bot, 42, f"/approve {confirm_id}"), context
        )
    )
    assert any(f"Confirmation #{confirm_id}: accepted (approved)" in r for r in replies)
    assert storage.get_confirmation(confirm_id)["status"] == "APPROVED"
    assert runner.wait_idle(timeout=30)
    assert storage.get_task(task_id)["status"] == "SUCCESS"

    replies.clear()
    reject_handler = find_handler(app, "reject")
    context.args = [str(confirm_id)]
    asyncio.run(
        reject_handler.callback(make_update(app.bot, 42, f"/reject {confirm_id}"), context)
    )
    assert any(f"Confirmation #{confirm_id}: refused (already_approved)" in r for r in replies)

    replies.clear()
    context.args = ["not-a-number"]
    asyncio.run(approve_handler.callback(make_update(app.bot, 42, "/approve x"), context))
    assert any("confirmation_id must be a number" in r for r in replies)
    runner.shutdown()
