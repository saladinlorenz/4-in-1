from __future__ import annotations

import asyncio

from telegram import Message, Update
from telegram.ext import CommandHandler, ContextTypes

from agent import AgentRunner
from agent.permissions import is_authorized_user
from memory.sqlite_store import TaskStatus
from telegram_bot import (
    HELP_TEXT,
    build_application,
    format_memory,
    format_status,
    format_task,
    format_tasks,
    format_workflows,
    register_handlers,
)

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

    for command in (
        "start",
        "help",
        "status",
        "ask",
        "tasks",
        "task",
        "memory",
        "workflows",
        "cancel",
        "approve",
        "reject",
    ):
        find_handler(app, command)
    for command in ("task", "memory", "workflows"):
        assert f"/{command} <" in HELP_TEXT or f"/{command} -" in HELP_TEXT
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


def test_format_task_memory_and_workflows():
    task = {
        "id": 7,
        "status": "FAILED",
        "created_at": "2026-10-05 10:00",
        "started_at": "2026-10-05 10:00:01",
        "finished_at": "2026-10-05 10:00:02",
        "prompt": "fais un résumé\ndu web",
        "result": None,
        "error": "boom with sk-abcdefghijklmnop",
        "cancel_requested": 0,
    }
    text = format_task(task)
    assert "Task #7 [FAILED]" in text
    assert "started: 2026-10-05 10:00:01" in text
    assert "fais un résumé du web" in text
    assert "error: boom with ***" in text
    assert "sk-abcdefghijklmnop" not in text

    ok = format_task(
        {"id": 1, "status": "PENDING", "created_at": "d", "prompt": "p", "result": "r"}
    )
    assert "result: r" in ok
    assert "error:" not in ok
    assert "started:" not in ok

    assert format_memory([]) == "No memories matched."
    assert (
        format_memory([{"created_at": "2026-10-05", "text": "fact one"}])
        == "- [2026-10-05] fact one"
    )

    empty = format_workflows([], [], [])
    assert empty.count("- none") == 3

    text = format_workflows(
        [{"name": "daily", "steps": '[{"name": "a", "kind": "agent", "prompt": "p"}]'}],
        [
            {
                "id": 2,
                "status": "RUNNING",
                "workflow_name": "daily",
                "current_step": 1,
                "created_at": "2026-10-05",
                "error": "step failed",
            }
        ],
        [
            {
                "name": "daily_0800",
                "workflow_name": "daily",
                "cron": "0 8 * * *",
                "enabled": True,
            }
        ],
    )
    assert "- daily (1 steps)" in text
    assert "run #2 [RUNNING] daily step=1" in text
    assert "error=step failed" in text
    assert "- daily_0800 -> daily [0 8 * * *] enabled" in text

    broken = format_workflows([{"name": "x", "steps": "not-json"}], [], [])
    assert "- x (0 steps)" in broken


def test_task_memory_workflow_handlers(settings, storage, notifier, monkeypatch):
    runner = AgentRunner(
        settings,
        storage,
        FakeRouter(responses=[final_answer_payload("ok")]),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    task_id = storage.create_task("what is the answer")
    storage.mark_running(task_id)
    storage.finish_task(task_id, TaskStatus.SUCCESS, result="42")
    storage.remember("the admin password never lives in the repo", source="test")
    workflow_id = storage.save_workflow(
        "daily", '[{"name": "a", "kind": "agent", "prompt": "p"}]'
    )
    storage.create_run(workflow_id, "daily")
    storage.save_scheduled_job("daily_0800", "daily", "0 8 * * *")

    app = build_application(settings)
    register_handlers(app, runner, storage, settings)

    replies: list[str] = []

    async def fake_reply(self, text=None, *args, **kwargs):
        replies.append(str(text))

    monkeypatch.setattr(Message, "reply_text", fake_reply)
    context = ContextTypes.DEFAULT_TYPE(app)

    task_handler = find_handler(app, "task")
    context.args = [str(task_id)]
    asyncio.run(task_handler.callback(make_update(app.bot, 42, "/task"), context))
    assert any(
        f"Task #{task_id} [SUCCESS]" in r and "result: 42" in r for r in replies
    )

    replies.clear()
    context.args = []
    asyncio.run(task_handler.callback(make_update(app.bot, 42, "/task"), context))
    assert replies == ["Usage: /task <task_id>"]

    replies.clear()
    context.args = ["nope"]
    asyncio.run(task_handler.callback(make_update(app.bot, 42, "/task nope"), context))
    assert replies == ["task_id must be a number"]

    replies.clear()
    context.args = ["99999"]
    asyncio.run(task_handler.callback(make_update(app.bot, 42, "/task 99999"), context))
    assert replies == ["Task #99999 not found."]

    memory_handler = find_handler(app, "memory")
    replies.clear()
    context.args = []
    asyncio.run(memory_handler.callback(make_update(app.bot, 42, "/memory"), context))
    assert replies == ["Usage: /memory <query>"]

    replies.clear()
    context.args = ["admin", "password"]
    asyncio.run(memory_handler.callback(make_update(app.bot, 42, "/memory admin"), context))
    assert any("admin password never lives" in r for r in replies)

    replies.clear()
    context.args = ["nothing-matches-this"]
    asyncio.run(
        memory_handler.callback(make_update(app.bot, 42, "/memory nothing"), context)
    )
    assert replies == ["No memories matched."]

    workflows_handler = find_handler(app, "workflows")
    replies.clear()
    context.args = []
    asyncio.run(
        workflows_handler.callback(make_update(app.bot, 42, "/workflows"), context)
    )
    joined = "\n".join(replies)
    assert "Workflows:" in joined
    assert "- daily (1 steps)" in joined
    assert "Recent runs:" in joined
    assert "run #1" in joined and "daily" in joined
    assert "Scheduled jobs:" in joined
    assert "- daily_0800 -> daily [0 8 * * *] enabled" in joined

    runner.shutdown()
