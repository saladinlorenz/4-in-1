from __future__ import annotations

import time

import httpx

from agent import AgentRunner
from config.secrets import SecretStore
from dashboard import HealthServer, LocalApi
from services import AdminAuth

from .conftest import FakeRouter, final_answer_payload

PASSWORD = "supersecret42"


def make_runner(settings, storage, notifier, responses=None) -> AgentRunner:
    return AgentRunner(
        settings,
        storage,
        FakeRouter(responses),
        notifier,
        search_fn=lambda query, count: [],
        fetch_fn=lambda url: "fetched",
    )


def make_server(settings, storage, notifier, responses=None):
    runner = make_runner(settings, storage, notifier, responses)
    auth = AdminAuth(storage, SecretStore(settings.storage_dir / ".env.runtime"))
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=LocalApi(runner, storage),
        auth=auth,
    )
    server.start()
    return server, runner


def auth_session(base: str) -> dict[str, str]:
    setup = httpx.post(base + "/api/setup", json={"password": PASSWORD}, timeout=5)
    assert setup.status_code == 200, setup.text
    login = httpx.post(base + "/api/login", json={"password": PASSWORD}, timeout=5)
    assert login.status_code == 200, login.text
    token = login.cookies.get("agentos_session")
    assert token
    return {
        "Cookie": f"agentos_session={token}",
        "X-CSRF-Token": login.json()["csrf_token"],
    }


def wait_status(storage, task_id: int, *statuses: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = storage.get_task(task_id)
        if task is not None and task["status"] in statuses:
            return True
        time.sleep(0.02)
    return False


def test_get_api_routes_return_items(settings, storage, notifier):
    storage.create_task("first task")
    storage.add_incident("test", "something broke", "detail")
    storage.remember("fact one", source="test")
    storage.create_draft("telegram", "draft content")
    storage.create_confirmation("publish_draft", "draft#1")

    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"

        tasks = httpx.get(base + "/api/tasks", timeout=5)
        assert tasks.status_code == 200
        assert len(tasks.json()["items"]) == 1
        assert tasks.json()["items"][0]["prompt"] == "first task"

        incidents = httpx.get(base + "/api/incidents", timeout=5)
        assert incidents.json()["items"][0]["source"] == "test"

        memory = httpx.get(base + "/api/memory", timeout=5)
        assert memory.json()["items"][0]["text"] == "fact one"

        drafts = httpx.get(base + "/api/drafts?status=DRAFT", timeout=5)
        assert drafts.json()["items"][0]["status"] == "DRAFT"

        confirmations = httpx.get(base + "/api/confirmations?status=PENDING", timeout=5)
        assert confirmations.json()["items"][0]["kind"] == "publish_draft"

        empty = httpx.get(base + "/api/drafts?status=PUBLISHED", timeout=5)
        assert empty.json()["items"] == []
    finally:
        server.stop()
        runner.shutdown()


def test_get_api_validates_limit(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(base + "/api/tasks?limit=abc", timeout=5).status_code == 400
        assert httpx.get(base + "/api/tasks?limit=0", timeout=5).status_code == 400
        ok = httpx.get(base + "/api/tasks?limit=999", timeout=5)
        assert ok.status_code == 200
    finally:
        server.stop()
        runner.shutdown()


def test_post_cancel_task(settings, storage, notifier):
    task_id = storage.create_task("cancel me")
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        denied = httpx.post(base + f"/api/tasks/{task_id}/cancel", timeout=5)
        assert denied.status_code == 401

        done = httpx.post(
            base + f"/api/tasks/{task_id}/cancel", headers=headers, timeout=5
        )
        assert done.status_code == 200
        assert done.json() == {"ok": True, "reason": "cancelled_before_start"}
        assert storage.get_task(task_id)["status"] == "CANCELLED"

        again = httpx.post(
            base + f"/api/tasks/{task_id}/cancel", headers=headers, timeout=5
        )
        assert again.json()["ok"] is False

        unknown = httpx.post(
            base + "/api/tasks/9999/cancel", headers=headers, timeout=5
        )
        assert unknown.json() == {"ok": False, "reason": "unknown_task"}
    finally:
        server.stop()
        runner.shutdown()


def test_post_confirmation_decision(settings, storage, notifier):
    task_id = storage.create_task("publish it")
    storage.mark_running(task_id)
    responses = [final_answer_payload("done")]
    server, runner = make_server(settings, storage, notifier, responses)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        draft_id = storage.create_draft("telegram", "content")
        confirmation_id = runner.request_confirmation(
            task_id, "publish_draft", f"draft#{draft_id}"
        )

        approve = httpx.post(
            base + f"/api/confirmations/{confirmation_id}/approve",
            headers=headers,
            timeout=5,
        )
        assert approve.json() == {"ok": True, "reason": "approved"}
        assert storage.get_confirmation(confirmation_id)["status"] == "APPROVED"
        assert wait_status(storage, task_id, "SUCCESS")
        assert storage.get_task(task_id)["status"] != "CANCELLED"

        again = httpx.post(
            base + f"/api/confirmations/{confirmation_id}/reject",
            headers=headers,
            timeout=5,
        )
        assert again.json()["ok"] is False

        unknown = httpx.post(
            base + "/api/confirmations/4242/approve", headers=headers, timeout=5
        )
        assert unknown.json()["ok"] is False
    finally:
        server.stop()
        runner.shutdown()


def test_post_reject_cancels_task(settings, storage, notifier):
    task_id = storage.create_task("needs consent")
    storage.mark_running(task_id)
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        confirmation_id = runner.request_confirmation(task_id, "publish_draft", "draft#1")

        reject = httpx.post(
            base + f"/api/confirmations/{confirmation_id}/reject",
            headers=headers,
            timeout=5,
        )
        assert reject.json() == {"ok": True, "reason": "rejected"}
        assert storage.get_task(task_id)["status"] == "CANCELLED"
    finally:
        server.stop()
        runner.shutdown()


def test_post_bad_ids_methods_and_unknown_paths(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        assert (
            httpx.post(base + "/api/tasks/abc/cancel", headers=headers, timeout=5).status_code
            == 400
        )
        assert (
            httpx.post(
                base + "/api/confirmations/0/approve", headers=headers, timeout=5
            ).status_code
            == 400
        )
        assert httpx.post(base + "/health", headers=headers, timeout=5).status_code == 405
        assert httpx.post(base + "/api/unknown", headers=headers, timeout=5).status_code == 404
        assert httpx.get(base + "/api/nope", timeout=5).status_code == 404
        assert (
            httpx.post(base + "/api/tasks/1/nothing", headers=headers, timeout=5).status_code
            == 404
        )
        # a session without the CSRF header is refused
        cookie = {"Cookie": headers["Cookie"]}
        assert (
            httpx.post(base + "/api/tasks/1/cancel", headers=cookie, timeout=5).status_code
            == 403
        )
    finally:
        server.stop()
        runner.shutdown()


def test_api_is_absent_without_controller(settings):
    server = HealthServer(
        "127.0.0.1", 0, lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0}
    )
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(base + "/api/tasks", timeout=5).status_code == 404
        assert httpx.post(base + "/api/tasks/1/cancel", timeout=5).status_code == 404
    finally:
        server.stop()
