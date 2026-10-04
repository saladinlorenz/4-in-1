from __future__ import annotations

from datetime import datetime, timezone

from agent import AgentRunner
from workflows import AppScheduler, WorkflowEngine

from .conftest import FakeRouter


class FakeClock:
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def make_runner(settings, storage, notifier) -> AgentRunner:
    return AgentRunner(
        settings,
        storage,
        FakeRouter(),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )


def make_scheduler(storage, runner, engine, clock=None, **kwargs) -> AppScheduler:
    kwargs.setdefault("confirmation_ttl_hours", 24.0)
    kwargs.setdefault("stuck_task_hours", 2.0)
    return AppScheduler(
        storage,
        engine,
        runner,
        clock=clock or FakeClock(datetime(2026, 10, 4, 7, 59, tzinfo=timezone.utc)),
        **kwargs,
    )


def test_run_job_enqueues_with_day_key_and_dedups(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    engine = WorkflowEngine(storage, runner)
    engine.register("gated", [{"name": "gate", "kind": "confirmation", "prompt": "Proceed?"}])
    scheduler = make_scheduler(storage, runner, engine)
    scheduler.register_jobs([{"name": "job1", "workflow_name": "gated", "cron": {"hour": 8}}])

    first = scheduler.run_job("job1")
    second = scheduler.run_job("job1")

    assert first is not None
    assert first == second
    assert len(storage.list_runs(10)) == 1
    job = storage.get_scheduled_job("job1")
    assert job["last_enqueued_at"] == "2026-10-04T07:59:00+00:00"
    runner.shutdown()


def test_run_job_unknown_and_disabled(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    engine = WorkflowEngine(storage, runner)
    scheduler = make_scheduler(storage, runner, engine)

    assert scheduler.run_job("missing") is None

    engine.register("gated", [{"name": "gate", "kind": "confirmation", "prompt": "Proceed?"}])
    scheduler.register_jobs([{"name": "off", "workflow_name": "gated", "cron": {"hour": 8}}])
    storage.save_scheduled_job("off", "gated", '{"hour": 8}', enabled=False)

    assert scheduler.run_job("off") is None
    assert storage.list_runs(10) == []
    runner.shutdown()


def test_expire_stale_confirmations_cancels_waiting_task(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    engine = WorkflowEngine(storage, runner)
    scheduler = make_scheduler(
        storage,
        runner,
        engine,
        confirmation_ttl_hours=0.0,
        clock=lambda: datetime.now(timezone.utc),
    )

    task_id = storage.create_task("sensitive")
    storage.mark_running(task_id)
    confirm_id = runner.request_confirmation(task_id, "publish_draft", "draft#1")
    assert confirm_id is not None

    expired = scheduler.expire_stale_confirmations()

    assert expired == 1
    assert storage.get_confirmation(confirm_id)["status"] == "EXPIRED"
    assert storage.get_task(task_id)["status"] == "CANCELLED"
    assert storage.get_task(task_id)["error"] == "confirmation expired"
    runner.shutdown()


def test_watch_stuck_tasks_reports_incident_and_notification(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    engine = WorkflowEngine(storage, runner)
    scheduler = make_scheduler(
        storage, runner, engine, stuck_task_hours=0.0, clock=lambda: datetime.now(timezone.utc)
    )

    task_id = storage.create_task("long running")
    storage.mark_running(task_id)
    fresh_id = storage.create_task("still pending")

    stuck = scheduler.watch_stuck_tasks()

    assert stuck == [task_id]
    incidents = storage.recent_incidents(5)
    assert incidents[0]["source"] == "supervisor"
    assert f"task {task_id}" in incidents[0]["detail"]
    assert any("Supervisor:" in text for text in notifier.items)
    assert storage.get_task(fresh_id)["status"] == "PENDING"
    runner.shutdown()


def test_start_and_stop_registers_all_jobs(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier)
    engine = WorkflowEngine(storage, runner)
    engine.register("gated", [{"name": "gate", "kind": "confirmation", "prompt": "Proceed?"}])
    scheduler = make_scheduler(storage, runner, engine)
    scheduler.register_jobs([{"name": "job1", "workflow_name": "gated", "cron": {"hour": 8, "minute": 0}}])

    active = scheduler.start()
    try:
        assert active == 3
        assert scheduler._scheduler is not None
        assert scheduler._scheduler.running is True
        assert scheduler._scheduler.get_job("job1") is not None
    finally:
        scheduler.stop()
    assert scheduler._scheduler is None
    runner.shutdown()
