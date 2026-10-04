from __future__ import annotations

import time

from agent import AgentRunner
from freellmapi_adapter import ErrorKind, LLMError
from workflows import WorkflowEngine

from .conftest import FakeRouter, final_answer_payload


def make_runner(settings, storage, notifier, router) -> AgentRunner:
    return AgentRunner(
        settings,
        storage,
        router,
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )


def make_engine(storage, runner, **kwargs) -> WorkflowEngine:
    kwargs.setdefault("retry_limit", 1)
    kwargs.setdefault("retry_backoff_base", 0.0)
    kwargs.setdefault("schedule", lambda delay, callback: callback())
    return WorkflowEngine(storage, runner, **kwargs)


def wire(runner: AgentRunner, engine: WorkflowEngine) -> None:
    runner.add_task_listener(engine.on_task_finished)
    runner.add_confirmation_listener(engine.on_confirmation_resolved)


def wait_for(storage, run_id: int, *statuses: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = storage.get_run(run_id)
        if run and run["status"] in statuses:
            return run
        time.sleep(0.05)
    return storage.get_run(run_id)


def wait_idle(runner: AgentRunner, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.wait_idle(timeout=2)
        time.sleep(0.05)
        if runner.status()["current_task"] is None:
            return True
    return False


def two_step_workflow() -> list[dict]:
    return [
        {"name": "one", "kind": "agent", "prompt": "do one"},
        {"name": "two", "kind": "agent", "prompt": "do two"},
    ]


def test_workflow_runs_to_success(settings, storage, notifier):
    router = FakeRouter(
        responses=[final_answer_payload("result one"), final_answer_payload("result two")]
    )
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    engine.register("demo", two_step_workflow())
    wire(runner, engine)

    run_id = engine.enqueue("demo", context="ctx")
    assert run_id is not None
    assert wait_idle(runner)

    run = wait_for(storage, run_id, "SUCCESS", "FAILED")
    assert run["status"] == "SUCCESS"
    assert run["result"] == "result two"

    steps = storage.list_steps(run_id)
    assert [step["status"] for step in steps] == ["SUCCESS", "SUCCESS"]
    second_task = storage.get_task(steps[1]["task_id"])
    assert "do two" in second_task["prompt"]
    assert "result one" in second_task["prompt"]
    assert any(f"run #{run_id}: SUCCESS" in text for text in notifier.items)
    runner.shutdown()


def test_register_is_idempotent_and_enqueue_rejects_unknown(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier, FakeRouter())
    engine = make_engine(storage, runner)

    first_id = engine.register("demo", two_step_workflow())
    second_id = engine.register("demo", two_step_workflow())
    assert first_id == second_id
    assert engine.enqueue("missing") is None
    runner.shutdown()


def test_enqueue_deduplicates_by_idempotency_key(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier, FakeRouter())
    engine = make_engine(storage, runner)
    engine.register(
        "gated",
        [{"name": "gate", "kind": "confirmation", "prompt": "Proceed with the run?"}],
    )
    wire(runner, engine)

    first = engine.enqueue("gated", idempotency_key="daily:2026-10-04")
    second = engine.enqueue("gated", idempotency_key="daily:2026-10-04")

    assert first == second
    assert len(storage.list_runs(10)) == 1
    assert storage.get_run(first)["status"] == "WAITING_CONFIRMATION"
    runner.shutdown()


def test_confirmation_step_pauses_then_approves_and_finishes(settings, storage, notifier):
    router = FakeRouter(
        responses=[final_answer_payload("prepared"), final_answer_payload("final answer")]
    )
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    engine.register(
        "gated",
        [
            {"name": "prepare", "kind": "agent", "prompt": "prepare the digest"},
            {"name": "gate", "kind": "confirmation", "prompt": "Store the digest?"},
            {"name": "store", "kind": "agent", "prompt": "store it"},
        ],
    )
    wire(runner, engine)

    run_id = engine.enqueue("gated")
    waiting = wait_for(storage, run_id, "WAITING_CONFIRMATION", "FAILED")
    assert waiting["status"] == "WAITING_CONFIRMATION"

    confirmation = storage.find_confirmation("workflow_run", f"workflow_run#{run_id}.step1")
    assert confirmation is not None
    assert confirmation["status"] == "PENDING"
    assert any(f"/approve {confirmation['id']}" in text for text in notifier.items)

    ok, reason = runner.resolve_confirmation(int(confirmation["id"]), "APPROVED")
    assert (ok, reason) == (True, "approved")
    assert wait_idle(runner)

    run = wait_for(storage, run_id, "SUCCESS", "FAILED")
    assert run["status"] == "SUCCESS"
    assert run["result"] == "final answer"
    runner.shutdown()


def test_confirmation_step_reject_cancels_run(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier, FakeRouter(responses=[]))
    engine = make_engine(storage, runner)
    engine.register(
        "gated",
        [{"name": "gate", "kind": "confirmation", "prompt": "Proceed?"}],
    )
    wire(runner, engine)

    run_id = engine.enqueue("gated")
    assert wait_for(storage, run_id, "WAITING_CONFIRMATION")["status"] == (
        "WAITING_CONFIRMATION"
    )
    confirmation = storage.find_confirmation("workflow_run", f"workflow_run#{run_id}.step0")

    ok, reason = runner.resolve_confirmation(int(confirmation["id"]), "REJECTED")
    assert (ok, reason) == (True, "rejected")

    run = wait_for(storage, run_id, "CANCELLED")
    assert run["status"] == "CANCELLED"
    assert run["error"] == "confirmation rejected"
    runner.shutdown()


def test_failed_step_retries_then_succeeds(settings, storage, notifier):
    class FailOnceRouter(FakeRouter):
        def __init__(self, responses) -> None:
            super().__init__(responses)
            self.calls = 0

        def chat(self, payload: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                self.payloads.append(payload)
                raise LLMError(ErrorKind.TIMEOUT, "first call fails", endpoint="one")
            return super().chat(payload)

    router = FailOnceRouter(responses=[final_answer_payload("second attempt works")])
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    engine.register("flaky", [{"name": "only", "kind": "agent", "prompt": "do it"}])
    wire(runner, engine)

    run_id = engine.enqueue("flaky")
    run = wait_for(storage, run_id, "SUCCESS", "FAILED")
    assert wait_idle(runner)

    assert run["status"] == "SUCCESS"
    assert run["result"] == "second attempt works"
    step = storage.list_steps(run_id)[0]
    assert step["retry_count"] == 1
    assert step["status"] == "SUCCESS"
    assert step["task_id"] is not None
    assert len(storage.list_tasks(10, status="FAILED")) == 1
    runner.shutdown()


def test_step_fails_after_retries_exhausted(settings, storage, notifier):
    router = FakeRouter(error=LLMError(ErrorKind.TIMEOUT, "always down", endpoint="one"))
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    engine.register("doomed", [{"name": "only", "kind": "agent", "prompt": "do it"}])
    wire(runner, engine)

    run_id = engine.enqueue("doomed")
    run = wait_for(storage, run_id, "FAILED", timeout=30)
    assert run["status"] == "FAILED"
    assert "always down" in (run["error"] or "")

    step = storage.list_steps(run_id)[0]
    assert step["status"] == "FAILED"
    assert step["retry_count"] == 1
    assert len(storage.list_tasks(10, status="FAILED")) == 2
    runner.shutdown()


def test_resume_advances_step_that_finished_before_crash(settings, storage, notifier):
    router = FakeRouter(responses=[final_answer_payload("final")])
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    workflow_id = engine.register("demo", two_step_workflow())
    wire(runner, engine)

    run_id = storage.create_run(workflow_id, "demo")
    step_id = storage.create_step(run_id, 0, "one")
    storage.update_step(step_id, status="SUCCESS", result="previous result")
    storage.update_run(run_id, status="RUNNING", current_step=0)

    counts = engine.resume()
    assert wait_idle(runner)
    run = wait_for(storage, run_id, "SUCCESS", "FAILED")

    assert counts["advanced"] == 1
    assert run["status"] == "SUCCESS"
    assert run["result"] == "final"
    steps = storage.list_steps(run_id)
    assert len(steps) == 2
    second_task = storage.get_task(steps[1]["task_id"])
    assert "previous result" in second_task["prompt"]
    runner.shutdown()


def test_resume_restarts_step_with_missing_task(settings, storage, notifier):
    router = FakeRouter(
        responses=[final_answer_payload("recovered"), final_answer_payload("and finished")]
    )
    runner = make_runner(settings, storage, notifier, router)
    engine = make_engine(storage, runner)
    workflow_id = engine.register("demo", two_step_workflow())
    wire(runner, engine)

    run_id = storage.create_run(workflow_id, "demo")
    storage.create_step(run_id, 0, "one")
    storage.update_run(run_id, status="RUNNING", current_step=0)

    counts = engine.resume()
    assert wait_idle(runner)
    run = wait_for(storage, run_id, "SUCCESS", "FAILED")

    assert counts["restarted"] == 1
    assert run["status"] == "SUCCESS"
    runner.shutdown()


def test_resume_keeps_waiting_run_with_pending_confirmation(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier, FakeRouter())
    engine = make_engine(storage, runner)
    workflow_id = engine.register(
        "gated", [{"name": "gate", "kind": "confirmation", "prompt": "Proceed?"}]
    )
    wire(runner, engine)

    run_id = storage.create_run(workflow_id, "gated")
    storage.create_step(run_id, 0, "gate", "confirmation")
    storage.update_run(run_id, status="WAITING_CONFIRMATION", current_step=0)
    storage.create_confirmation("workflow_run", f"workflow_run#{run_id}.step0")

    counts = engine.resume()

    assert counts["waiting"] == 1
    assert storage.get_run(run_id)["status"] == "WAITING_CONFIRMATION"
    runner.shutdown()
