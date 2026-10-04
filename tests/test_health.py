from __future__ import annotations

import httpx

from dashboard import HealthServer


def test_health_endpoint_reports_ok():
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0, "task_counts": {}},
    )
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        health = httpx.get(base + "/health", timeout=5)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        status = httpx.get(base + "/api/status", timeout=5)
        assert status.status_code == 200
        assert status.json()["version"] == "0.1.0"

        missing = httpx.get(base + "/nope", timeout=5)
        assert missing.status_code == 404
    finally:
        server.stop()


def test_health_endpoint_reports_degraded_and_errors():
    def degraded():
        return {"status": "degraded", "version": "0.1.0", "uptime_seconds": 2.0}

    server = HealthServer("127.0.0.1", 0, degraded)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(base + "/health", timeout=5).status_code == 503
    finally:
        server.stop()

    def broken():
        raise RuntimeError("boom")

    server = HealthServer("127.0.0.1", 0, broken)
    server.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{server.port}/health", timeout=5)
        assert response.status_code == 503
        assert response.json()["status"] == "error"
    finally:
        server.stop()


def test_health_server_start_stop_is_idempotent_enough():
    server = HealthServer("127.0.0.1", 0, lambda: {"status": "ok", "version": "x", "uptime_seconds": 0})
    server.start()
    port = server.port
    server.stop()
    server.stop()
    assert port > 0
