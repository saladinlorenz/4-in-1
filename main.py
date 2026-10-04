from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from agent import AgentRunner
from config import Settings, load_settings, setup_logging
from dashboard import HealthServer
from freellmapi_adapter import LLMRouter
from memory import Storage
from telegram_bot import LogNotifier, TelegramNotifier, build_application, register_handlers

logger = logging.getLogger("agentos")


def build_snapshot(runner: AgentRunner, storage: Storage, router: LLMRouter, settings: Settings):
    def snapshot() -> dict[str, Any]:
        database_ok = storage.ping()
        status = runner.status()
        return {
            "status": "ok" if database_ok else "degraded",
            "version": settings.app_version,
            "uptime_seconds": status.get("uptime_seconds"),
            "db": database_ok,
            "current_task": status.get("current_task"),
            "task_counts": status.get("task_counts"),
            "llm_endpoints": status.get("llm_endpoints"),
            "telegram_configured": status.get("telegram_configured"),
        }

    return snapshot


async def run(settings: Settings) -> None:
    settings.ensure_dirs()
    storage = Storage(settings.db_path)
    storage.init_schema()
    router = LLMRouter(
        settings.llm_endpoints,
        attempts=settings.llm_max_attempts,
        backoff_base=settings.llm_backoff_base,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )
    runner = AgentRunner(settings, storage, router, LogNotifier())
    health = HealthServer(settings.health_host, settings.health_port, build_snapshot(runner, storage, router, settings))
    application = None
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop(*_args: object) -> None:
        logger.info("shutdown requested")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, request_stop)

    if not settings.llm_endpoints:
        logger.warning("no LLM endpoint configured: set LLM_ENDPOINTS in .env")
    if not settings.telegram_allowed_user_ids:
        logger.warning("no authorized Telegram user configured: set TELEGRAM_ALLOWED_USER_IDS")

    application = build_application(settings)
    if application is not None:
        register_handlers(application, runner, storage, settings)
        runner.notifier = TelegramNotifier(
            application.bot,
            settings.telegram_admin_chat_id,
            loop,
            timeout=settings.telegram_send_timeout,
        )
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram polling started")
    else:
        logger.warning("Telegram disabled: TELEGRAM_BOT_TOKEN is not set")

    health.start()
    logger.info(
        "AgentOS %s ready: health on http://%s:%s (python runner active)",
        settings.app_version,
        settings.health_host,
        health.port,
    )

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        if application is not None:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception:
                logger.warning("Telegram shutdown failed", exc_info=True)
        runner.interrupt_current()
        runner.shutdown(wait=True)
        health.stop()
        router.close()
        storage.close()
        logger.info("AgentOS stopped")


def main() -> int:
    settings = load_settings()
    setup_logging(settings.log_level, settings.logs_dir)
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN missing: /ask and notifications are unavailable")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
