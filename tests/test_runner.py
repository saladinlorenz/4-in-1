from __future__ import annotations

import json
import threading

from smolagents import ChatMessage, MessageRole, ToolCallingAgent, tool

from agent import AgentRunner
from freellmapi_adapter import ErrorKind, LLMError

from .conftest import FakeRouter, final_answer_payload


@tool
def noop_tool(text: str) -> str:
    """Return a fixed acknowledgement.

    Args:
        text: Text that is ignored.
    """
    return "ack"


def make_runner(settings, storage, notifier, router, **kwargs) -> AgentRunner:
    kwargs.setdefault("search_fn", lambda query, count: [])
    kwargs.setdefault("fetch_fn", lambda url: "fetched")
    return AgentRunner(settings, storage, router, notifier, **kwargs)


def test_successful_task_stores_result_and_notifies(settings, storage, notifier):
    router = FakeRouter(responses=[final_answer_payload("la réponse est 42")])
    runner = make_runner(settings, storage, notifier, router)

    task_id = runner.submit("quelle est la réponse ?", chat_id=7)
    assert runner.wait_idle(timeout=30)

    task = storage.get_task(task_id)
    assert task["status"] == "SUCCESS"
    assert task["result"] == "la réponse est 42"
    assert len(notifier.items) == 1
    assert f"Task #{task_id} completed" in notifier.items[0]

    messages = storage.recent_messages(10)
    assert [row["role"] for row in messages] == ["user", "assistant"]
    runner.shutdown()


def test_failed_task_records_incident(settings, storage, notifier):
    error = LLMError(ErrorKind.AUTH, "bad key", endpoint="one")
    router = FakeRouter(error=error)
    runner = make_runner(settings, storage, notifier, router)

    task_id = runner.submit("une tâche")
    assert runner.wait_idle(timeout=30)

    task = storage.get_task(task_id)
    assert task["status"] == "FAILED"
    assert "bad key" in (task["error"] or "")
    assert storage.recent_incidents(5)[0]["source"] == "agent"
    assert f"Task #{task_id} failed" in notifier.items[0]
    runner.shutdown()


def test_cancel_pending_task_before_worker_picks_it(settings, storage, notifier):
    first_started = threading.Event()
    release_first = threading.Event()

    class BlockingModel:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, messages, **kwargs):
            self.calls += 1
            first_started.set()
            release_first.wait(timeout=15)
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content=None,
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "final_answer", "arguments": json.dumps({"answer": "première tâche"})},
                    }
                ],
            )

    router = FakeRouter()
    runner = make_runner(
        settings,
        storage,
        notifier,
        router,
        agent_factory=lambda: ToolCallingAgent(tools=[], model=BlockingModel(), max_steps=3),
    )

    first_id = runner.submit("première")
    assert first_started.wait(timeout=15)
    second_id = runner.submit("seconde")

    ok, reason = runner.cancel(second_id)
    assert ok is True
    assert reason == "cancelled_before_start"
    assert storage.get_task(second_id)["status"] == "CANCELLED"

    release_first.set()
    assert runner.wait_idle(timeout=30)

    assert storage.get_task(first_id)["status"] == "SUCCESS"
    assert storage.get_task(second_id)["status"] == "CANCELLED"
    runner.shutdown()


def test_cancel_running_task_interrupts_agent(settings, storage, notifier):
    started = threading.Event()
    release = threading.Event()

    class SlowModel:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                started.set()
                release.wait(timeout=15)
                return ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=None,
                    tool_calls=[
                        {"id": "c1", "type": "function", "function": {"name": "noop_tool", "arguments": json.dumps({"text": "x"})}}
                    ],
                )
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content=None,
                tool_calls=[
                    {"id": "c2", "type": "function", "function": {"name": "final_answer", "arguments": json.dumps({"answer": "tard"})}}
                ],
            )

    router = FakeRouter()
    runner = make_runner(
        settings,
        storage,
        notifier,
        router,
        agent_factory=lambda: ToolCallingAgent(tools=[noop_tool], model=SlowModel(), max_steps=5),
    )

    task_id = runner.submit("tâche longue")
    assert started.wait(timeout=15)

    ok, reason = runner.cancel(task_id)
    assert ok is True
    assert reason == "interrupt_requested"
    assert runner.status()["current_task"] == task_id

    release.set()
    assert runner.wait_idle(timeout=30)

    task = storage.get_task(task_id)
    assert task["status"] == "CANCELLED"
    assert storage.recent_incidents(5) == []
    runner.shutdown()


def test_status_shape(settings, storage, notifier):
    router = FakeRouter()
    runner = make_runner(settings, storage, notifier, router)
    status = runner.status()

    assert status["app"] == "agentos"
    assert status["current_task"] is None
    assert status["task_counts"] == {}
    assert status["llm_endpoints"][0]["state"] == "ready"
    assert status["telegram_configured"] is True
    runner.shutdown()


def test_notification_failure_does_not_fail_task(settings, storage, notifier):
    class ExplodingNotifier:
        def send(self, text: str) -> None:
            raise RuntimeError("telegram down")

    router = FakeRouter(responses=[final_answer_payload("ok")])
    runner = AgentRunner(
        settings,
        storage,
        router,
        ExplodingNotifier(),
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )
    task_id = runner.submit("salut")
    assert runner.wait_idle(timeout=30)

    assert storage.get_task(task_id)["status"] == "SUCCESS"
    runner.shutdown()


def test_chunk_text_limits():
    from agent import chunk_text

    assert chunk_text("") == []
    assert chunk_text("abc") == ["abc"]
    chunks = chunk_text("x" * 9000, limit=4000)
    assert [len(chunk) for chunk in chunks] == [4000, 4000, 1000]


def test_resume_requeues_interrupted_and_pending_tasks(settings, storage, notifier):
    from memory import TaskStatus

    interrupted = storage.create_task("interrupted")
    storage.mark_running(interrupted)
    pending = storage.create_task("pending")
    done = storage.create_task("done")
    storage.mark_running(done)
    storage.finish_task(done, TaskStatus.SUCCESS, result="ok")

    router = FakeRouter(
        responses=[final_answer_payload("reprise un"), final_answer_payload("reprise deux")]
    )
    runner = make_runner(settings, storage, notifier, router)
    stats = runner.resume()
    assert runner.wait_idle(timeout=30)

    assert stats == {"requeued_running": 1, "cancelled_running": 0, "resumed_pending": 2}
    assert storage.get_task(interrupted)["status"] == "SUCCESS"
    assert storage.get_task(pending)["status"] == "SUCCESS"
    assert storage.get_task(done)["status"] == "SUCCESS"
    assert storage.get_task(done)["result"] == "ok"
    assert len(notifier.items) == 2
    runner.shutdown()


def test_resume_is_idempotent(settings, storage, notifier):
    task_id = storage.create_task("only once")
    router = FakeRouter(responses=[final_answer_payload("unique")])
    runner = make_runner(settings, storage, notifier, router)

    runner.resume()
    runner.resume()
    assert runner.wait_idle(timeout=30)

    assert storage.get_task(task_id)["status"] == "SUCCESS"
    assert storage.get_task(task_id)["result"] == "unique"
    completions = [text for text in notifier.items if "completed" in text]
    assert len(completions) == 1
    runner.shutdown()


def test_resume_keeps_cancel_requested_task_cancelled(settings, storage, notifier):
    task_id = storage.create_task("was cancelled while running")
    storage.mark_running(task_id)
    storage.cancel_task(task_id)

    router = FakeRouter(responses=[])
    runner = make_runner(settings, storage, notifier, router)
    stats = runner.resume()
    assert runner.wait_idle(timeout=30)

    assert stats == {"requeued_running": 0, "cancelled_running": 1, "resumed_pending": 0}
    assert storage.get_task(task_id)["status"] == "CANCELLED"
    assert notifier.items == []
    runner.shutdown()


def test_request_and_approve_confirmation_resumes_task(settings, storage, notifier):
    task_id = storage.create_task("publish draft 1")
    storage.mark_running(task_id)

    router = FakeRouter(responses=[final_answer_payload("published")])
    runner = make_runner(settings, storage, notifier, router)

    confirm_id = runner.request_confirmation(task_id, "publish_draft", "draft#1")
    assert confirm_id is not None
    assert storage.get_task(task_id)["status"] == "WAITING_CONFIRMATION"
    assert any(f"Confirmation #{confirm_id} required" in text for text in notifier.items)

    ok, reason = runner.resolve_confirmation(confirm_id, "APPROVED")
    assert (ok, reason) == (True, "approved")
    assert runner.wait_idle(timeout=30)

    assert storage.get_confirmation(confirm_id)["status"] == "APPROVED"
    task = storage.get_task(task_id)
    assert task["status"] == "SUCCESS"
    assert task["result"] == "published"
    assert any(f"resuming task #{task_id}" in text for text in notifier.items)

    ok, reason = runner.resolve_confirmation(confirm_id, "REJECTED")
    assert ok is False
    assert reason == "already_approved"
    runner.shutdown()


def test_reject_confirmation_cancels_task(settings, storage, notifier):
    task_id = storage.create_task("publish draft 2")
    storage.mark_running(task_id)

    runner = make_runner(settings, storage, notifier, FakeRouter(responses=[]))
    confirm_id = runner.request_confirmation(task_id, "publish_draft", "draft#2")
    assert confirm_id is not None

    ok, reason = runner.resolve_confirmation(confirm_id, "REJECTED")
    assert (ok, reason) == (True, "rejected")

    task = storage.get_task(task_id)
    assert task["status"] == "CANCELLED"
    assert task["error"] == "confirmation rejected"
    assert storage.get_confirmation(confirm_id)["status"] == "REJECTED"
    runner.shutdown()


def test_confirmation_errors_and_expiration(settings, storage, notifier):
    runner = make_runner(settings, storage, notifier, FakeRouter(responses=[]))

    assert runner.resolve_confirmation(999, "APPROVED") == (False, "unknown_confirmation")
    assert runner.resolve_confirmation(999, "MAYBE") == (False, "unknown_confirmation")
    assert runner.expire_confirmation(999) == (False, "unknown_confirmation")

    pending_id = storage.create_task("still pending")
    assert runner.request_confirmation(pending_id, "publish", "payload") is None

    task_id = storage.create_task("running task")
    storage.mark_running(task_id)
    confirm_id = runner.request_confirmation(task_id, "publish", "payload")
    assert confirm_id is not None

    assert runner.expire_confirmation(confirm_id) == (True, "expired")
    task = storage.get_task(task_id)
    assert task["status"] == "CANCELLED"
    assert task["error"] == "confirmation expired"
    assert storage.get_confirmation(confirm_id)["status"] == "EXPIRED"
    assert runner.expire_confirmation(confirm_id) == (False, "already_expired")
    runner.shutdown()


def test_generate_text_uses_router(settings, storage, notifier):
    router = FakeRouter(
        responses=[
            {"choices": [{"message": {"content": "pong"}}]},
            {"choices": []},
        ]
    )
    runner = make_runner(settings, storage, notifier, router)

    assert runner._generate_text("ping") == "pong"
    assert router.payloads[0]["messages"][0]["content"] == "ping"
    assert router.payloads[0]["temperature"] == 0.3

    try:
        runner._generate_text("again")
        raise AssertionError("empty choices must raise")
    except RuntimeError as exc:
        assert "no choices" in str(exc)
    runner.shutdown()


def test_workflow_helpers(settings, storage, notifier):
    from workflows import WorkflowEngine

    runner = make_runner(settings, storage, notifier, FakeRouter(responses=[]))
    assert runner._workflow_runs("", 5) == []
    try:
        runner._workflow_create("wf", [])
        raise AssertionError("missing engine must raise")
    except RuntimeError as exc:
        assert "not attached" in str(exc)

    engine = WorkflowEngine(storage, runner, schedule=lambda delay, callback: None)
    runner.workflow_engine = engine
    workflow_id = runner._workflow_create(
        "wf", [{"name": "step1", "kind": "agent", "prompt": "do it"}]
    )
    assert workflow_id >= 1
    assert storage.get_workflow("wf") is not None

    workflow = storage.get_workflow("wf")
    storage.create_run(workflow["id"], "wf")
    storage.create_run(workflow["id"], "other")
    rows = runner._workflow_runs("wf", 5)
    assert [row["workflow_name"] for row in rows] == ["wf"]
    runner.shutdown()
