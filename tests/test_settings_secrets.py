from __future__ import annotations

import os

from config.secrets import SecretStore
from services import SettingsService


def test_settings_roundtrip_and_audit(storage):
    service = SettingsService(storage, actor="tester")
    assert service.get("missing", default="fallback") == "fallback"

    service.set("agent.max_steps", 12, category="agent")
    service.set("general.language", "fr")
    assert service.get("agent.max_steps") == 12
    assert service.get("general.language") == "fr"

    rows = service.list("agent")
    assert [row["key"] for row in rows] == ["agent.max_steps"]
    assert rows[0]["value"] == 12
    assert rows[0]["updated_by"] == "tester"

    actions = [row["action"] for row in storage.recent_audit(10)]
    assert actions == [
        "settings.set:general.language",
        "settings.set:agent.max_steps",
    ]

    assert service.delete("general.language") is True
    assert service.delete("general.language") is False
    assert service.get("general.language") is None
    assert "settings.delete:general.language" in [
        row["action"] for row in storage.recent_audit(10)
    ]


def test_settings_validation(storage):
    service = SettingsService(storage)
    for bad in ("", "a" * 65, "bad key", "no/slash"):
        try:
            service.set(bad, 1)
            raise AssertionError(f"key {bad!r} must be rejected")
        except ValueError:
            pass
    try:
        service.set("ok", 1, category="nope")
        raise AssertionError("unknown category must be rejected")
    except ValueError as exc:
        assert "category" in str(exc)
    try:
        service.set("ok", "x" * 5000)
        raise AssertionError("oversized value must be rejected")
    except ValueError as exc:
        assert "too large" in str(exc)
    assert storage.recent_audit(10) == []


def test_settings_never_hold_secrets(storage):
    service = SettingsService(storage)
    service.set("models.endpoint_note", "key configured elsewhere")
    rows = service.list()
    assert all("value_json" not in row for row in rows)
    # the secret store is a separate door
    assert storage.recent_audit(10)[0]["action"] == "settings.set:models.endpoint_note"


def test_secret_store_roundtrip(tmp_path):
    store = SecretStore(tmp_path / ".env.runtime")
    assert store.get("LLM_KEY") is None
    assert store.mask("LLM_KEY") is None

    store.set("LLM_KEY", "sk-abcdefghijkl")
    assert store.get("LLM_KEY") == "sk-abcdefghijkl"
    assert store.has("LLM_KEY")

    mask = store.mask("LLM_KEY")
    assert mask == "Configured (ends ...ijkl)"
    assert "sk-abcdefghijkl" not in mask
    assert store.masked_items(["LLM_KEY", "OTHER"]) == {"LLM_KEY": mask, "OTHER": None}

    assert store.delete("LLM_KEY") is True
    assert store.delete("LLM_KEY") is False
    assert store.get("LLM_KEY") is None


def test_secret_store_validation_and_persistence(tmp_path):
    store = SecretStore(tmp_path / "sub" / ".env.runtime")
    for bad in ("", "with space", "1LEADING", "dash-name"):
        try:
            store.set(bad, "v")
            raise AssertionError(f"name {bad!r} must be rejected")
        except ValueError:
            pass
    try:
        store.set("GOOD", "line1\nline2")
        raise AssertionError("multiline value must be rejected")
    except ValueError:
        pass
    try:
        store.set("GOOD", "")
        raise AssertionError("empty value must be rejected")
    except ValueError:
        pass

    store.set("GOOD", "value=with=equals")
    fresh = SecretStore(store.path)
    assert fresh.get("GOOD") == "value=with=equals"
    assert "value=with=equals" in store.path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert (os.stat(store.path).st_mode & 0o777) == 0o600
