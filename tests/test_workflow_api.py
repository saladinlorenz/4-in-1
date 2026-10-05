from __future__ import annotations

import time

import httpx

from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth
from workflows import AppScheduler, WorkflowEngine

from .conftest import final_answer_payload
from .test_api import auth_session, make_runner

STEPS = [
    {"name": "one", "kind": "agent", "prompt": "do one {context}"},
    {"name": "two", "kind": "agent", "prompt": "do two"},
]


def make_server(settings, storage, notifier, responses=None):
    runner = make_runner(settings, storage, notifier, responses)
    engine = WorkflowEngine(
        storage,
        runner,
        retry_limit=1,
        retry_backoff_base=0.0,
        schedule=lambda delay, callback: callback(),
    )
    runner.workflow_engine = engine
    runner.add_task_listener(engine.on_task_finished)
    runner.add_confirmation_listener(engine.on_confirmation_resolved)
    scheduler = AppScheduler(storage, engine, runner)
    auth = AdminAuth(storage, SecretStore(settings.storage_dir / ".env.runtime"))
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(
            runner,
            storage,
            workflow_engine=engine,
            scheduler=scheduler,
        ),
        auth=auth,
    )
    server.start()
    return server, runner, engine, scheduler


def wait_run(storage, run_id: int, *statuses: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = storage.get_run(run_id)
        if run and run["status"] in statuses:
            return run
        time.sleep(0.02)
    return storage.get_run(run_id)


def test_invalid_workflow_definitions_are_rejected(settings, storage, notifier):
    server, runner, engine, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        invalid_payloads = [
            {"name": "wf", "steps": [{"name": "a", "kind": "agent", "prompt": "p"}], "extra": 1},
            {"name": "wf", "steps": [{"name": "a", "kind": "shell", "prompt": "p"}]},
            {"name": "wf", "steps": [{"name": "a", "kind": "agent", "prompt": "p", "sudo": True}]},
            {"name": "wf", "steps": []},
            {"name": "bad name!", "steps": [{"name": "a", "kind": "agent", "prompt": "p"}]},
            {"name": "wf", "steps": [{"name": "a", "kind": "agent", "prompt": ""}]},
            {"name": "wf", "steps": [{"kind": "agent", "prompt": "p"}]},
            {"name": "wf", "steps": [{"name": "a", "kind": "agent", "prompt": "p"}], "cron": {"hour": 8}},
            {"name": "wf", "steps": "not-a-list"},
        ]
        for payload in invalid_payloads:
            res = httpx.post(base + "/api/workflows", json=payload, headers=headers, timeout=5)
            assert res.status_code == 400, payload
            assert "error" in res.json()

        dry = httpx.post(
            base + "/api/workflows/dry_run",
            json={"name": "wf", "steps": [{"name": "a", "kind": "agent", "prompt": "p", "x": 1}]},
            headers=headers,
            timeout=5,
        )
        assert dry.status_code == 400

        listing = httpx.get(base + "/api/workflows", headers=headers, timeout=5)
        assert listing.status_code == 200
        assert listing.json()["items"] == []
        assert storage.list_workflows() == []

        try:
            engine.register("ghost", [{"kind": "shell", "prompt": "rm -rf /"}])
        except ValueError:
            pass
        else:
            raise AssertionError("engine.register must reject invalid steps")

        denied = httpx.post(
            base + "/api/workflows",
            json={"name": "wf", "steps": STEPS},
            timeout=5,
        )
        assert denied.status_code == 401
    finally:
        server.stop()
        runner.shutdown()


def test_create_dry_run_and_run_lifecycle(settings, storage, notifier):
    responses = [final_answer_payload("result one"), final_answer_payload("result two")]
    server, runner, _engine, _ = make_server(settings, storage, notifier, responses)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        dry = httpx.post(
            base + "/api/workflows/dry_run",
            json={"name": "demo", "steps": STEPS},
            headers=headers,
            timeout=5,
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["step_count"] == 2
        assert dry.json()["persisted"] is False
        assert httpx.get(
            base + "/api/workflows", headers=headers, timeout=5
        ).json()["items"] == []

        created = httpx.post(
            base + "/api/workflows",
            json={"name": "demo", "steps": STEPS},
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text
        assert created.json()["name"] == "demo"
        assert created.json()["step_count"] == 2

        listing = httpx.get(base + "/api/workflows", headers=headers, timeout=5).json()
        assert [w["name"] for w in listing["items"]] == ["demo"]
        assert set(listing["items"][0]["steps"][0]) == {"name", "kind", "prompt"}

        started = httpx.post(
            base + "/api/workflows/run",
            json={"name": "demo", "context": "ctx"},
            headers=headers,
            timeout=5,
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        run = wait_run(storage, run_id, "SUCCESS", "FAILED")
        assert run["status"] == "SUCCESS"
        assert run["result"] == "result two"

        runs = httpx.get(base + "/api/workflows?limit=5", headers=headers, timeout=5).json()["runs"]
        assert any(r["id"] == run_id and r["status"] == "SUCCESS" for r in runs)

        ghost = httpx.post(
            base + "/api/workflows/run",
            json={"name": "does_not_exist"},
            headers=headers,
            timeout=5,
        )
        assert ghost.status_code == 400
        assert "unknown workflow" in ghost.json()["error"]

        actions = [row["action"] for row in storage.recent_audit(20)]
        assert "workflow.create" in actions
        assert "workflow.run" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_cancel_run_through_api(settings, storage, notifier):
    server, runner, _engine, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        created = httpx.post(
            base + "/api/workflows",
            json={
                "name": "gated",
                "steps": [{"name": "gate", "kind": "confirmation", "prompt": "Publish?"}],
            },
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text

        started = httpx.post(
            base + "/api/workflows/run",
            json={"name": "gated"},
            headers=headers,
            timeout=5,
        )
        run_id = started.json()["run_id"]
        assert storage.get_run(run_id)["status"] == "WAITING_CONFIRMATION"

        cancelled = httpx.post(
            base + "/api/workflows/cancel",
            json={"run_id": run_id},
            headers=headers,
            timeout=5,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["ok"] is True
        assert storage.get_run(run_id)["status"] == "CANCELLED"

        again = httpx.post(
            base + "/api/workflows/cancel",
            json={"run_id": run_id},
            headers=headers,
            timeout=5,
        )
        assert again.json()["ok"] is False
        assert "already CANCELLED" in again.json()["reason"]

        unknown = httpx.post(
            base + "/api/workflows/cancel",
            json={"run_id": 999999},
            headers=headers,
            timeout=5,
        )
        assert unknown.json() == {"ok": False, "reason": "unknown run"}

        invalid = httpx.post(
            base + "/api/workflows/cancel",
            json={"run_id": "abc"},
            headers=headers,
            timeout=5,
        )
        assert invalid.status_code == 400

        actions = [row["action"] for row in storage.recent_audit(20)]
        assert "workflow.cancel" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_schedule_workflow_crud(settings, storage, notifier):
    server, runner, _engine, _ = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        httpx.post(
            base + "/api/workflows",
            json={"name": "demo", "steps": STEPS},
            headers=headers,
            timeout=5,
        )

        scheduled = httpx.post(
            base + "/api/workflows/schedule",
            json={"name": "demo_0800", "workflow_name": "demo", "hour": 8, "minute": 30},
            headers=headers,
            timeout=5,
        )
        assert scheduled.status_code == 200, scheduled.text

        jobs = httpx.get(base + "/api/workflows", headers=headers, timeout=5).json()["jobs"]
        job = next(j for j in jobs if j["name"] == "demo_0800")
        assert job == {
            "name": "demo_0800",
            "workflow_name": "demo",
            "hour": 8,
            "minute": 30,
            "enabled": True,
            "last_enqueued_at": None,
        }

        bad_payloads = [
            {"name": "j", "workflow_name": "demo", "hour": 24, "minute": 0},
            {"name": "j", "workflow_name": "demo", "hour": 8, "minute": 60},
            {"name": "j", "workflow_name": "ghost", "hour": 8, "minute": 0},
            {"name": "j", "workflow_name": "demo", "hour": 8, "minute": 0, "cron": {}},
            {"name": "bad name", "workflow_name": "demo", "hour": 8, "minute": 0},
        ]
        for payload in bad_payloads:
            res = httpx.post(
                base + "/api/workflows/schedule",
                json=payload,
                headers=headers,
                timeout=5,
            )
            assert res.status_code == 400, payload

        removed = httpx.post(
            base + "/api/workflows/schedule/delete",
            json={"name": "demo_0800"},
            headers=headers,
            timeout=5,
        )
        assert removed.status_code == 200
        assert removed.json() == {"ok": True}
        assert storage.get_scheduled_job("demo_0800") is None

        missing = httpx.post(
            base + "/api/workflows/schedule/delete",
            json={"name": "demo_0800"},
            headers=headers,
            timeout=5,
        )
        assert missing.json() == {"ok": False}
    finally:
        server.stop()
        runner.shutdown()


def test_schedule_set_syncs_live_scheduler(settings, storage, notifier):
    server, runner, engine, scheduler = make_server(settings, storage, notifier)
    try:
        engine.register("demo", STEPS)
        scheduler.start()
        try:
            scheduler.schedule_set("live_job", "demo", 9, 15)
            assert scheduler._scheduler.get_job("live_job") is not None
            stored = storage.get_scheduled_job("live_job")
            assert stored["enabled"] == 1

            try:
                scheduler.schedule_set("nope", "ghost", 9, 15)
            except ValueError as exc:
                assert "unknown workflow" in str(exc)
            else:
                raise AssertionError("unknown workflow must be refused")

            assert scheduler.schedule_delete("live_job") is True
            assert scheduler._scheduler.get_job("live_job") is None
            assert storage.get_scheduled_job("live_job") is None
        finally:
            scheduler.stop()
    finally:
        server.stop()
        runner.shutdown()
