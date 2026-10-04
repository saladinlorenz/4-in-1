from __future__ import annotations

from memory import Storage, TaskStatus


def test_task_lifecycle_success(storage: Storage):
    task_id = storage.create_task("hello", chat_id=7)
    task = storage.get_task(task_id)
    assert task["status"] == "PENDING"
    assert task["chat_id"] == 7

    assert storage.mark_running(task_id) is True
    assert storage.get_task(task_id)["status"] == "RUNNING"
    assert storage.mark_running(task_id) is False

    assert storage.finish_task(task_id, TaskStatus.SUCCESS, result="done") is True
    task = storage.get_task(task_id)
    assert task["status"] == "SUCCESS"
    assert task["result"] == "done"
    assert task["finished_at"] is not None


def test_terminal_task_cannot_be_reopened(storage: Storage):
    task_id = storage.create_task("x")
    storage.mark_running(task_id)
    storage.finish_task(task_id, TaskStatus.FAILED, error="boom")

    assert storage.finish_task(task_id, TaskStatus.SUCCESS, result="late") is False
    assert storage.get_task(task_id)["status"] == "FAILED"


def test_cancel_pending_task(storage: Storage):
    task_id = storage.create_task("x")
    ok, reason = storage.cancel_task(task_id)

    assert ok is True
    assert reason == "cancelled_before_start"
    assert storage.get_task(task_id)["status"] == "CANCELLED"
    assert storage.mark_running(task_id) is False


def test_cancel_running_task_sets_flag(storage: Storage):
    task_id = storage.create_task("x")
    storage.mark_running(task_id)

    ok, reason = storage.cancel_task(task_id)
    assert ok is True
    assert reason == "interrupt_requested"
    assert storage.is_cancel_requested(task_id) is True
    assert storage.get_task(task_id)["status"] == "RUNNING"


def test_cancel_unknown_and_terminal_task(storage: Storage):
    assert storage.cancel_task(999) == (False, "unknown_task")

    task_id = storage.create_task("x")
    storage.mark_running(task_id)
    storage.finish_task(task_id, TaskStatus.SUCCESS, result="ok")
    ok, reason = storage.cancel_task(task_id)
    assert ok is False
    assert reason == "already_success"


def test_task_counts_and_listing(storage: Storage):
    first = storage.create_task("a")
    second = storage.create_task("b")
    storage.mark_running(first)
    storage.finish_task(first, TaskStatus.SUCCESS, result="r")

    counts = storage.task_counts()
    assert counts == {"SUCCESS": 1, "PENDING": 1}

    rows = storage.list_tasks(10)
    assert [row["id"] for row in rows] == [second, first]
    assert storage.list_tasks(10, status="SUCCESS")[0]["id"] == first


def test_messages_roundtrip(storage: Storage):
    storage.add_message("user", "question", chat_id=5, task_id=1)
    storage.add_message("assistant", "answer", chat_id=5, task_id=1)
    rows = storage.recent_messages(10, chat_id=5)

    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "question"


def test_incidents(storage: Storage):
    storage.add_incident("agent", "task 1 failed", "trace")
    rows = storage.recent_incidents(5)
    assert rows[0]["source"] == "agent"
    assert rows[0]["detail"] == "trace"


def test_memory_search_exact_and_term_fallback(storage: Storage):
    storage.remember("Le siège de l'entreprise est à Madrid", source="agent")
    storage.remember("La clé API du compte est rotée chaque mois", source="agent")

    exact = storage.search_memory("Madrid")
    assert len(exact) == 1
    assert "Madrid" in exact[0]["text"]

    fallback = storage.search_memory("entreprise clé")
    assert len(fallback) >= 1

    assert storage.search_memory("inconnu-xyz") == []


def test_drafts_and_confirmations(storage: Storage):
    draft_id = storage.create_draft("devto", "Mon article", scheduled_for="2026-10-05T10:00:00+00:00")
    draft = storage.get_draft(draft_id)
    assert draft["platform"] == "devto"
    assert draft["status"] == "DRAFT"
    assert storage.list_drafts()[0]["id"] == draft_id

    confirm_id = storage.create_confirmation("publish", "draft#1")
    assert storage.decide_confirmation(confirm_id, "APPROVED") is True
    assert storage.decide_confirmation(confirm_id, "REJECTED") is False


def test_ping_and_reopen(storage: Storage, settings):
    assert storage.ping() is True
    storage.close()

    reopened = Storage(settings.db_path)
    assert reopened.ping() is True
    assert reopened.task_counts() == {}
    reopened.close()


def test_reset_interrupted_tasks_recovery(storage: Storage):
    running_id = storage.create_task("interrupted")
    storage.mark_running(running_id)
    cancel_id = storage.create_task("to cancel")
    storage.mark_running(cancel_id)
    storage.cancel_task(cancel_id)
    done_id = storage.create_task("done")
    storage.mark_running(done_id)
    storage.finish_task(done_id, TaskStatus.SUCCESS, result="ok")

    stats = storage.reset_interrupted_tasks()

    assert stats == {"cancelled": 1, "requeued": 1}
    assert storage.get_task(running_id)["status"] == "PENDING"
    assert storage.get_task(running_id)["started_at"] is None
    assert storage.get_task(cancel_id)["status"] == "CANCELLED"
    assert storage.get_task(done_id)["status"] == "SUCCESS"
    assert storage.get_task(done_id)["result"] == "ok"
    assert [row["id"] for row in storage.list_pending_tasks()] == [running_id]
    assert storage.mark_running(running_id) is True


def test_confirmation_hold_release_and_crud(storage: Storage):
    task_id = storage.create_task("publish draft 1")
    storage.mark_running(task_id)

    assert storage.hold_task(task_id) is True
    assert storage.get_task(task_id)["status"] == "WAITING_CONFIRMATION"
    assert storage.hold_task(task_id) is False

    confirm_id = storage.create_confirmation("publish_draft", "draft#1", task_id=task_id)
    confirmation = storage.get_confirmation(confirm_id)
    assert confirmation["task_id"] == task_id
    assert confirmation["status"] == "PENDING"
    assert storage.get_confirmation(999) is None
    assert storage.list_confirmations(status="PENDING")[0]["id"] == confirm_id

    assert storage.release_task(task_id, TaskStatus.PENDING) is True
    assert storage.release_task(task_id, TaskStatus.PENDING) is False
    task = storage.get_task(task_id)
    assert task["status"] == "PENDING"
    assert task["started_at"] is None

    assert storage.decide_confirmation(confirm_id, "APPROVED") is True
    assert storage.decide_confirmation(confirm_id, "REJECTED") is False
    assert storage.get_confirmation(confirm_id)["status"] == "APPROVED"
    assert storage.expire_confirmation(confirm_id) is False


def test_cancel_waiting_task_expires_confirmation(storage: Storage):
    task_id = storage.create_task("sensitive action")
    storage.mark_running(task_id)
    storage.hold_task(task_id)
    confirm_id = storage.create_confirmation("publish", "payload", task_id=task_id)

    ok, reason = storage.cancel_task(task_id)

    assert ok is True
    assert reason == "cancelled_while_waiting"
    assert storage.get_task(task_id)["status"] == "CANCELLED"
    assert storage.get_confirmation(confirm_id)["status"] == "EXPIRED"
    assert storage.release_task(task_id, TaskStatus.PENDING) is False


def test_expire_confirmations_before_bulk(storage: Storage):
    old_id = storage.create_confirmation("publish", "old")
    new_id = storage.create_confirmation("publish", "new")

    expired = storage.expire_confirmations_before("9999-01-01T00:00:00+00:00")
    assert sorted(expired) == [old_id, new_id]
    assert storage.get_confirmation(old_id)["status"] == "EXPIRED"
    assert storage.get_confirmation(new_id)["status"] == "EXPIRED"

    assert storage.expire_confirmations_before("9999-01-01T00:00:00+00:00") == []
