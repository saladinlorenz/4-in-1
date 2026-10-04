from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from memory import Storage

logger = logging.getLogger(__name__)


class AppScheduler:
    """Time-based triggers only: APScheduler wakes up, SQLite does the work.

    Three families of jobs:
    - cron jobs that enqueue a workflow with a per-day idempotency key;
    - periodic confirmation expiry (through ``runner`` so listeners fire);
    - periodic stuck-task watch (incidents, no state mutation).

    The scheduler never contains business logic: every trigger ends in
    ``engine.enqueue`` or a read-only supervision pass.
    """

    def __init__(
        self,
        storage: Storage,
        engine: object,
        runner: object,
        *,
        timezone_name: str = "UTC",
        confirmation_ttl_hours: float = 24.0,
        stuck_task_hours: float = 2.0,
        clock: object | None = None,
    ) -> None:
        self.storage = storage
        self.engine = engine
        self.runner = runner
        self.timezone_name = timezone_name
        self.confirmation_ttl_hours = confirmation_ttl_hours
        self.stuck_task_hours = stuck_task_hours
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._scheduler: object | None = None

    # --- registration ----------------------------------------------------

    def register_jobs(self, jobs: list[dict]) -> int:
        persisted = 0
        for job in jobs:
            cron = job.get("cron") or {}
            self.storage.save_scheduled_job(
                str(job["name"]),
                str(job["workflow_name"]),
                json.dumps(cron),
            )
            persisted += 1
        return persisted

    # --- dynamic schedule management (dashboard) -------------------------

    def schedule_set(
        self,
        name: str,
        workflow_name: str,
        hour: int,
        minute: int,
        *,
        enabled: bool = True,
    ) -> int:
        """Create or update a cron job, syncing the live scheduler if started."""
        if self.storage.get_workflow(workflow_name) is None:
            raise ValueError(f"unknown workflow '{workflow_name}'")
        cron = {"hour": int(hour), "minute": int(minute)}
        job_id = self.storage.save_scheduled_job(
            name, workflow_name, json.dumps(cron), enabled=enabled
        )
        self._sync_job(name, cron, enabled)
        logger.info(
            "scheduled job %s -> %s at %02d:%02d (enabled=%s)",
            name,
            workflow_name,
            int(hour),
            int(minute),
            enabled,
        )
        return job_id

    def schedule_delete(self, name: str) -> bool:
        """Remove a cron job from SQLite and from the live scheduler."""
        removed = self.storage.delete_scheduled_job(name)
        if removed:
            self._remove_job(name)
            logger.info("scheduled job %s deleted", name)
        return removed

    def _sync_job(self, name: str, cron: dict, enabled: bool) -> None:
        scheduler = self._scheduler
        if scheduler is None:
            return
        if not enabled:
            self._remove_job(name)
            return
        from apscheduler.triggers.cron import CronTrigger

        scheduler.add_job(
            self.run_job,
            CronTrigger(hour=cron.get("hour"), minute=cron.get("minute"), timezone=self._tz()),
            args=[name],
            id=name,
            replace_existing=True,
        )

    def _remove_job(self, name: str) -> None:
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            scheduler.remove_job(name)
        except Exception:
            logger.debug("scheduled job %s was not registered", name)

    def start(self) -> int:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        tz = self._tz()
        scheduler = BackgroundScheduler(timezone=tz)
        active = 0
        for job in self.storage.list_scheduled_jobs():
            if not job["enabled"]:
                continue
            try:
                cron = json.loads(job["cron"])
            except ValueError:
                logger.warning("scheduled job %s has invalid cron JSON, skipped", job["name"])
                continue
            scheduler.add_job(
                self.run_job,
                CronTrigger(hour=cron.get("hour"), minute=cron.get("minute"), timezone=tz),
                args=[job["name"]],
                id=str(job["name"]),
                replace_existing=True,
            )
            active += 1
        if self.confirmation_ttl_hours > 0:
            scheduler.add_job(
                self.expire_stale_confirmations,
                IntervalTrigger(minutes=30),
                id="expire-confirmations",
                replace_existing=True,
            )
            active += 1
        if self.stuck_task_hours > 0:
            scheduler.add_job(
                self.watch_stuck_tasks,
                IntervalTrigger(minutes=15),
                id="watch-stuck-tasks",
                replace_existing=True,
            )
            active += 1
        scheduler.start()
        self._scheduler = scheduler
        logger.info("scheduler started: %s job(s), timezone %s", active, tz)
        return active

    def stop(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.warning("scheduler shutdown failed", exc_info=True)

    # --- triggers --------------------------------------------------------

    def run_job(self, name: str) -> int | None:
        """Cron body: enqueue the workflow with a per-day idempotency key."""
        job = self.storage.get_scheduled_job(name)
        if job is None:
            logger.warning("scheduled job %s is unknown", name)
            return None
        if not job["enabled"]:
            logger.info("scheduled job %s is disabled", name)
            return None
        day = self._clock().date().isoformat()
        run_id = self.engine.enqueue(job["workflow_name"], idempotency_key=f"{name}:{day}")
        if run_id is not None:
            self.storage.mark_job_enqueued(name, self._clock().isoformat())
        return run_id

    def expire_stale_confirmations(self) -> int:
        cutoff = (self._clock() - timedelta(hours=self.confirmation_ttl_hours)).isoformat()
        return int(self.runner.expire_stale_confirmations(cutoff))

    def watch_stuck_tasks(self) -> list[int]:
        cutoff = (self._clock() - timedelta(hours=self.stuck_task_hours)).isoformat()
        stale = self.storage.stale_tasks(cutoff)
        for task in stale:
            detail = f"task {task['id']} still RUNNING since {task['started_at']}"
            self.storage.add_incident("supervisor", "stuck task detected", detail)
            self.runner.notify(f"Supervisor: {detail}")
            logger.warning(detail)
        return [int(task["id"]) for task in stale]

    # --- internals -------------------------------------------------------

    def _tz(self) -> object:
        try:
            return ZoneInfo(self.timezone_name)
        except Exception:
            logger.warning("unknown timezone %s, falling back to UTC", self.timezone_name)
            return timezone.utc
