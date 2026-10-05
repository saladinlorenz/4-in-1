from __future__ import annotations

import httpx

from .test_api import auth_session, make_server

STEPS = [{"name": "one", "kind": "agent", "prompt": "p"}]


def test_submit_task_and_show_detail(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        denied = httpx.post(base + "/api/tasks/submit", json={"prompt": "x"}, timeout=5)
        assert denied.status_code == 401

        created = httpx.post(
            base + "/api/tasks/submit",
            json={"prompt": "Liste les faits mémorisés"},
            headers=headers,
            timeout=30,
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        assert task_id >= 1

        assert httpx.post(
            base + "/api/tasks/submit", json={"prompt": ""}, headers=headers, timeout=5
        ).status_code == 400
        assert httpx.post(
            base + "/api/tasks/submit",
            json={"prompt": "x", "chat_id": 1},
            headers=headers,
            timeout=5,
        ).status_code == 400

        runner.wait_idle(timeout=10)
        detail = httpx.get(base + f"/api/tasks/{task_id}", headers=headers, timeout=5)
        assert detail.status_code == 200, detail.text
        item = detail.json()["item"]
        assert item["prompt"] == "Liste les faits mémorisés"
        assert item["status"] in {"PENDING", "RUNNING", "SUCCESS", "FAILED"}

        assert httpx.get(base + "/api/tasks/999999", headers=headers, timeout=5).status_code == 400
        assert httpx.get(base + f"/api/tasks/{task_id}", timeout=5).status_code == 401

        actions = [row["action"] for row in storage.recent_audit(10)]
        assert "task.submit" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_memory_add_and_delete(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        created = httpx.post(
            base + "/api/memory/add",
            json={"text": "Préfère les réponses courtes"},
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text
        fact_id = created.json()["id"]

        listing = httpx.get(base + "/api/memory?limit=5", headers=headers, timeout=5)
        assert any(item["id"] == fact_id for item in listing.json()["items"])

        assert httpx.post(
            base + "/api/memory/add", json={"text": ""}, headers=headers, timeout=5
        ).status_code == 400
        assert httpx.post(
            base + "/api/memory/add",
            json={"text": "x", "source": "evil"},
            headers=headers,
            timeout=5,
        ).status_code == 400

        removed = httpx.post(
            base + "/api/memory/delete", json={"id": fact_id}, headers=headers, timeout=5
        )
        assert removed.status_code == 200 and removed.json()["ok"] is True

        missing = httpx.post(
            base + "/api/memory/delete", json={"id": fact_id}, headers=headers, timeout=5
        )
        assert missing.json()["ok"] is False

        actions = [row["action"] for row in storage.recent_audit(10)]
        assert "memory.add" in actions
        assert "memory.delete" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_draft_create_validation(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        created = httpx.post(
            base + "/api/drafts/create",
            json={"platform": "devto", "content": "Un article sur AgentOS"},
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text
        draft_id = created.json()["id"]

        listing = httpx.get(base + "/api/drafts?limit=5", headers=headers, timeout=5)
        assert any(item["id"] == draft_id for item in listing.json()["items"])

        assert httpx.post(
            base + "/api/drafts/create",
            json={"platform": "devto", "content": ""},
            headers=headers,
            timeout=5,
        ).status_code == 400
        assert httpx.post(
            base + "/api/drafts/create",
            json={"platform": "bad platform!", "content": "x"},
            headers=headers,
            timeout=5,
        ).status_code == 400

        actions = [row["action"] for row in storage.recent_audit(10)]
        assert "draft.create" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_workflow_delete_removes_definition_and_jobs(settings, storage, notifier):
    from .test_workflow_api import make_server as make_wf_server

    server, runner, _engine, _scheduler = make_wf_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)

        created = httpx.post(
            base + "/api/workflows",
            json={"name": "doomed", "steps": STEPS},
            headers=headers,
            timeout=5,
        )
        assert created.status_code == 200, created.text
        httpx.post(
            base + "/api/workflows/schedule",
            json={"name": "doomed_0800", "workflow_name": "doomed", "hour": 8, "minute": 0},
            headers=headers,
            timeout=5,
        )

        removed = httpx.post(
            base + "/api/workflows/delete",
            json={"name": "doomed"},
            headers=headers,
            timeout=5,
        )
        assert removed.status_code == 200 and removed.json()["ok"] is True
        assert storage.get_workflow("doomed") is None
        assert storage.get_scheduled_job("doomed_0800") is None

        again = httpx.post(
            base + "/api/workflows/delete",
            json={"name": "doomed"},
            headers=headers,
            timeout=5,
        )
        assert again.json()["ok"] is False
        assert httpx.post(
            base + "/api/workflows/delete", json={"name": ""}, headers=headers, timeout=5
        ).status_code == 400

        actions = [row["action"] for row in storage.recent_audit(10)]
        assert "workflow.delete" in actions
    finally:
        server.stop()
        runner.shutdown()


def test_incidents_clear(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        storage.add_incident("test", "boom happened", "detail")

        cleared = httpx.post(base + "/api/incidents/clear", json={}, headers=headers, timeout=5)
        assert cleared.status_code == 200 and cleared.json()["deleted"] == 1

        listing = httpx.get(base + "/api/incidents?limit=5", headers=headers, timeout=5)
        assert listing.json()["items"] == []

        assert httpx.post(
            base + "/api/incidents/clear", json={"keep": 1}, headers=headers, timeout=5
        ).status_code == 400

        actions = [row["action"] for row in storage.recent_audit(10)]
        assert "incidents.clear" in actions
    finally:
        server.stop()
        runner.shutdown()
