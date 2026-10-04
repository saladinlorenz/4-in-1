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
