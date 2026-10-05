from __future__ import annotations

import httpx

from config.secrets import SecretStore
from services import AdminAuth, SettingsService

from .test_api import auth_session, make_runner


def make_server(settings, storage, notifier, *, with_services: bool = True):
    from dashboard import HealthServer, LocalApi

    runner = make_runner(settings, storage, notifier)
    secrets = SecretStore(settings.storage_dir / ".env.runtime")
    api = LocalApi(runner, storage)
    if with_services:
        api.secret_store = secrets
        api.settings_service = SettingsService(storage)
    server = HealthServer(
        "127.0.0.1",
        0,
        lambda: {"status": "ok", "version": "0.1.0", "uptime_seconds": 1.0},
        api=api,
        auth=AdminAuth(storage, secrets),
    )
    server.start()
    return server, runner


def test_index_html_served(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier, with_services=False)
    try:
        base = f"http://127.0.0.1:{server.port}"
        page = httpx.get(base + "/", timeout=5)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "AgentOS" in page.text
        assert 'id="ui"' in page.text
        assert 'id="login-form"' in page.text
        alias = httpx.get(base + "/index.html", timeout=5)
        assert alias.status_code == 200
        assert alias.text == page.text

        # the shell itself is public, the API behind it is not
        assert httpx.get(base + "/api/nope", timeout=5).status_code == 404
        assert httpx.post(base + "/api/audit", json={}, timeout=5).status_code == 401
    finally:
        server.stop()
        runner.shutdown()


def test_session_endpoint_drives_auth_reload(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"

        early = httpx.get(base + "/api/session", timeout=5)
        assert early.status_code == 401
        assert early.json()["setup_required"] is True

        headers = auth_session(base)  # setup + login

        session = httpx.get(base + "/api/session", headers=headers, timeout=5)
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["csrf_token"] == headers["X-CSRF-Token"]

        # CSRF recovered from /api/session works for writes (page reload flow)
        resume = {"Cookie": headers["Cookie"], "X-CSRF-Token": session.json()["csrf_token"]}
        write = httpx.post(
            base + "/api/settings",
            json={"key": "instance_name", "value": "Home", "category": "general"},
            headers=resume,
            timeout=5,
        )
        assert write.status_code == 200

        httpx.post(
            base + "/api/logout",
            headers=headers,
            timeout=5,
        )
        after = httpx.get(base + "/api/session", timeout=5)
        assert after.status_code == 401
        assert after.json()["setup_required"] is False  # password already set
    finally:
        server.stop()
        runner.shutdown()


def test_audit_endpoint_lists_auth_actions(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        listing = httpx.get(base + "/api/audit?limit=10", headers=headers, timeout=5)
        assert listing.status_code == 200
        actions = [item["action"] for item in listing.json()["items"]]
        assert "auth.setup" in actions
        assert "auth.login" in actions

        bad = httpx.get(base + "/api/audit?limit=abc", headers=headers, timeout=5)
        assert bad.status_code == 400
    finally:
        server.stop()
        runner.shutdown()


def test_settings_page_uses_schema_and_js_ids_resolve(settings, storage, notifier):
    import re

    server, runner = make_server(settings, storage, notifier, with_services=False)
    try:
        base = f"http://127.0.0.1:{server.port}"
        html = httpx.get(base + "/", timeout=5).text
        assert 'id="set-schema"' in html
        assert 'id="set-restart"' in html
        assert 'id="set-free"' in html
        assert 'id="fadd"' in html
        assert 'data-page="general">Réglages</a>' in html
        assert "loadSchemaSettings" in html
        assert "/api/settings/schema" in html
        # every element id referenced literally from JS must exist in the markup
        for el_id in {m[1] for m in re.findall(r"\$\((['\"])([A-Za-z0-9_-]+)\1\)", html)}:
            assert f'id="{el_id}"' in html, f"#{el_id} referenced by JS is missing"
    finally:
        server.stop()
        runner.shutdown()


def test_settings_endpoint_503_without_service(settings, storage, notifier):
    server, runner = make_server(settings, storage, notifier, with_services=False)
    try:
        base = f"http://127.0.0.1:{server.port}"
        headers = auth_session(base)
        assert httpx.get(base + "/api/settings", headers=headers, timeout=5).status_code == 503
        assert httpx.get(base + "/api/secrets", headers=headers, timeout=5).status_code == 503
        assert httpx.get(base + "/api/settings", timeout=5).status_code == 401
    finally:
        server.stop()
        runner.shutdown()
