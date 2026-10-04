from __future__ import annotations

import logging

from config.logging import RedactingFilter, redact, setup_logging
from config.settings import PROJECT_ROOT, Settings, load_settings


def test_llm_endpoints_parsed_from_env_json(monkeypatch):
    monkeypatch.setenv(
        "LLM_ENDPOINTS",
        '[{"base_url":"http://127.0.0.1:3001/v1","api_key":"freellmapi-abc","model":"auto"},'
        '{"base_url":"https://example.test/v1","api_key":"k","model":"m","timeout":12}]',
    )
    settings = Settings(_env_file=None)
    assert len(settings.llm_endpoints) == 2
    assert settings.llm_endpoints[0].model == "auto"
    assert settings.llm_endpoints[1].timeout == 12
    assert settings.llm_endpoints[0].label.startswith("127.0.0.1:3001")


def test_allowed_user_ids_from_comma_string(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11, 22")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "99")
    settings = load_settings(env_file=None)
    assert settings.telegram_allowed_user_ids == [11, 22]
    assert settings.allowed_user_ids == {11, 22, 99}


def test_allowed_user_ids_accepts_json_list(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "[7, 8]")
    settings = Settings(_env_file=None)
    assert settings.telegram_allowed_user_ids == [7, 8]


def test_empty_authorization_is_secure_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    settings = Settings(_env_file=None)
    assert settings.allowed_user_ids == set()


def test_storage_paths_derived_from_storage_dir(tmp_path):
    settings = Settings(_env_file=None, storage_dir=tmp_path / "data")
    settings.ensure_dirs()
    assert settings.db_path.exists() is False
    assert settings.files_dir.is_dir()
    assert settings.logs_dir.is_dir()


def test_env_example_documents_required_keys():
    env_example = PROJECT_ROOT / ".env.example"
    assert env_example.exists()
    content = env_example.read_text(encoding="utf-8")
    for key in ("LLM_ENDPOINTS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "HEALTH_PORT"):
        assert key in content, f"{key} missing from .env.example"


def test_redacting_filter_masks_secrets():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "using Bearer freellmapi-deadbeefcafe1234 and sk-abcdefghijklmnop", None, None
    )
    assert RedactingFilter().filter(record) is True
    message = record.getMessage()
    assert "freellmapi-deadbeefcafe1234" not in message
    assert "sk-abcdefghijklmnop" not in message


def test_redact_handles_url_and_headers():
    assert "secretvalue" not in redact("Authorization: Bearer secretvalue")
    assert "mykey12345" not in redact('{"api_key": "mykey12345"}')


def test_setup_logging_creates_log_file(tmp_path):
    logs_dir = tmp_path / "logs"
    setup_logging("INFO", logs_dir)
    logging.getLogger("agentos.test").info("hello from test")
    log_file = logs_dir / "agentos.log"
    assert log_file.exists()
    assert "hello from test" in log_file.read_text(encoding="utf-8")
    setup_logging("INFO", None)
