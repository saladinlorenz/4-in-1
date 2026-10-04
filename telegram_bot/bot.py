from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from agent.core import AgentRunner
from agent.permissions import is_authorized_user
from config.settings import Settings
from memory import Storage

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "AgentOS commands:\n"
    "/status - system status\n"
    "/ask <message> - run a task (same as sending a plain message)\n"
    "/tasks - list recent tasks\n"
    "/cancel <task_id> - cancel a pending or running task\n"
    "/approve <confirmation_id> - approve a pending confirmation\n"
    "/reject <confirmation_id> - reject a pending confirmation\n"
    "/help - this message"
)


def format_status(status: dict) -> str:
    lines = [
        f"app: {status.get('app')} {status.get('version')}",
        f"uptime: {status.get('uptime_seconds')}s",
        f"current task: {status.get('current_task')}",
        f"tasks: {status.get('task_counts')}",
        f"telegram: {'configured' if status.get('telegram_configured') else 'disabled'}",
    ]
    for endpoint in status.get("llm_endpoints", []):
        lines.append(
            "llm: {endpoint} [{state}] last_error={last_error}".format(
                endpoint=endpoint.get("endpoint"),
                state=endpoint.get("state"),
                last_error=endpoint.get("last_error"),
            )
        )
    if not status.get("llm_endpoints"):
        lines.append("llm: no endpoints configured")
    return "\n".join(lines)


def format_tasks(rows: list[dict]) -> str:
    if not rows:
        return "No tasks yet."
    lines = []
    for row in rows:
        prompt = (row.get("prompt") or "").replace("\n", " ")[:60]
        lines.append(
            f"#{row.get('id')} [{row.get('status')}] {row.get('created_at')} {prompt}"
        )
    return "\n".join(lines)


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    for start in range(0, len(text), 4000):
        await message.reply_text(text[start : start + 4000])


def build_application(settings: Settings) -> Application | None:
    if not settings.telegram_bot_token:
        return None
    return Application.builder().token(settings.telegram_bot_token).build()


def register_handlers(
    app: Application,
    runner: AgentRunner,
    storage: Storage,
    settings: Settings,
) -> None:
    allowed = set(settings.allowed_user_ids)
    if not allowed:
        logger.warning("telegram_allowed_user_ids is empty: the bot will ignore every update")

    def authorized(update: Update) -> bool:
        user = update.effective_user
        user_id = user.id if user else None
        if not is_authorized_user(user_id, allowed):
            logger.warning("rejected Telegram update from user_id=%s", user_id)
            return False
        return True

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply(update, HELP_TEXT)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply(update, format_status(runner.status()))

    async def _submit(update: Update, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt:
            await _reply(update, "Usage: /ask <message>")
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        task_id = runner.submit(prompt, chat_id=chat_id)
        await _reply(update, f"Task #{task_id} queued.")

    async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _submit(update, " ".join(context.args or []))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        message = update.effective_message
        await _submit(update, message.text if message else "")

    async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        rows = await asyncio.to_thread(storage.list_tasks, 10)
        await _reply(update, format_tasks(rows))

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        args = context.args or []
        if not args:
            await _reply(update, "Usage: /cancel <task_id>")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            await _reply(update, "task_id must be a number")
            return
        ok, reason = await asyncio.to_thread(runner.cancel, task_id)
        state = "accepted" if ok else "refused"
        await _reply(update, f"Cancel task #{task_id}: {state} ({reason})")

    async def _decide_confirmation(
        update: Update, context: ContextTypes.DEFAULT_TYPE, decision: str
    ) -> None:
        if not authorized(update):
            return
        command = "approve" if decision == "APPROVED" else "reject"
        args = context.args or []
        if not args:
            await _reply(update, f"Usage: /{command} <confirmation_id>")
            return
        try:
            confirmation_id = int(args[0])
        except ValueError:
            await _reply(update, "confirmation_id must be a number")
            return
        ok, reason = await asyncio.to_thread(
            runner.resolve_confirmation, confirmation_id, decision
        )
        state = "accepted" if ok else "refused"
        await _reply(update, f"Confirmation #{confirmation_id}: {state} ({reason})")

    async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _decide_confirmation(update, context, "APPROVED")

    async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _decide_confirmation(update, context, "REJECTED")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
