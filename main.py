from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from agent import AgentRunner
from config import Settings, load_settings, setup_logging
from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from freellmapi_adapter import LLMRouter
from memory import Storage
from services import (
    AdminAuth,
    AgentConfigService,
    IntegrationTester,
    LLMConfigService,
    SettingsService,
    apply_db_settings,
)
from social import BlueskyAdapter, DevtoAdapter, SocialService, TelegramAdapter
from telegram_bot import LogNotifier, TelegramNotifier, build_application, register_handlers
from workflows import DEFAULT_SCHEDULED_JOBS, DEFAULT_WORKFLOWS, AppScheduler, WorkflowEngine

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
    secret_store = SecretStore(settings.storage_dir / ".env.runtime")
    settings_service = SettingsService(storage, actor="dashboard")
    agent_config = AgentConfigService(settings_service, settings)
    runner = AgentRunner(
        settings, storage, router, LogNotifier(), agent_config=agent_config
    )
    engine = WorkflowEngine(
        storage,
        runner,
        retry_limit=settings.workflow_retry_limit,
        retry_backoff_base=settings.workflow_retry_backoff_base,
    )
    for workflow_name, workflow_steps in DEFAULT_WORKFLOWS.items():
        engine.register(workflow_name, workflow_steps)
    runner.workflow_engine = engine
    runner.add_task_listener(engine.on_task_finished)
    runner.add_confirmation_listener(engine.on_confirmation_resolved)
    social = SocialService(storage, runner)
    social.add_adapter(DevtoAdapter(secret_store))
    social.add_adapter(BlueskyAdapter(secret_store))
    runner.add_confirmation_listener(social.on_confirmation)
    llm_config = LLMConfigService(settings_service, secret_store, router)
    seeded = llm_config.seed(settings.llm_endpoints)
    llm_config.reload()
    if seeded:
        logger.info("LLM endpoints moved from .env to SQLite: %d", seeded)
    scheduler = AppScheduler(
        storage,
        engine,
        runner,
        timezone_name=settings.scheduler_timezone,
        confirmation_ttl_hours=settings.confirmation_ttl_hours,
        stuck_task_hours=settings.stuck_task_hours,
    )
    admin_auth = AdminAuth(storage, secret_store)
    integrations = IntegrationTester(secret_store, settings)
    health = HealthServer(
        settings.health_host,
        settings.health_port,
        build_snapshot(runner, storage, router, settings),
        api=LocalApi(
            runner,
            storage,
            settings_service=settings_service,
            secret_store=secret_store,
            llm_config=llm_config,
            agent_config=agent_config,
            workflow_engine=engine,
            scheduler=scheduler,
            social=social,
            auth=admin_auth,
            integrations=integrations,
            app_settings=settings,
        ),
        auth=admin_auth,
    )
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
        social.add_adapter(
            TelegramAdapter(
                application.bot,
                settings.telegram_admin_chat_id,
                loop,
                timeout=settings.telegram_send_timeout,
            )
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
    resumed = runner.resume()
    logger.info(
        "recovery: requeued=%s cancelled=%s pending=%s",
        resumed["requeued_running"],
        resumed["cancelled_running"],
        resumed["resumed_pending"],
    )
    workflow_resume = engine.resume()
    logger.info(
        "workflow recovery: waiting=%s advanced=%s restarted=%s finished=%s requeued=%s",
        workflow_resume["waiting"],
        workflow_resume["advanced"],
        workflow_resume["restarted"],
        workflow_resume["finished"],
        workflow_resume["requeued"],
    )
    scheduler.register_jobs(DEFAULT_SCHEDULED_JOBS)
    if settings.scheduler_enabled:
        scheduler.start()
    else:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=false)")

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        scheduler.stop()
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
    settings.ensure_dirs()
    boot_storage = Storage(settings.db_path)
    boot_storage.init_schema()
    applied, warnings = apply_db_settings(boot_storage, settings)
    boot_storage.close()
    setup_logging(settings.log_level, settings.logs_dir)
    for warning in warnings:
        logger.warning("settings override skipped: %s", warning)
    if applied:
        logger.info("settings overrides applied at boot: %s", ", ".join(applied))
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN missing: /ask and notifications are unavailable")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
