from __future__ import annotations

import json
from typing import Any

import pytest

from agent.tools.base import ToolDeps
from config.settings import LLMEndpoint, Settings
from memory import Storage


class ListNotifier:
    def __init__(self) -> None:
        self.items: list[str] = []

    def send(self, text: str) -> None:
        self.items.append(text)


class FakeRouter:
    def __init__(self, responses: list[dict] | None = None, error: Exception | None = None) -> None:
        self.responses = responses or []
        self.error = error
        self.payloads: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    def health(self) -> list[dict]:
        return [{"endpoint": "fake/model", "state": "ready", "last_error": None}]


def final_answer_payload(answer: str, usage: bool = True) -> dict:
    payload: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "final_answer", "arguments": json.dumps({"answer": answer})},
                        }
                    ],
                }
            }
        ]
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
    return payload


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=tmp_path / "storage",
        llm_endpoints=[
            LLMEndpoint(base_url="http://127.0.0.1:3001/v1", api_key="freellmapi-test-key", model="auto")
        ],
        telegram_allowed_user_ids=[42],
        telegram_admin_chat_id=42,
        telegram_bot_token="123456789:TESTTOKEN_FOR_UNIT_TESTS",
        llm_max_attempts=1,
    )


@pytest.fixture
def storage(settings) -> Storage:
    store = Storage(settings.db_path)
    store.init_schema()
    yield store
    store.close()


@pytest.fixture
def notifier() -> ListNotifier:
    return ListNotifier()


@pytest.fixture
def tool_deps(storage, notifier, settings) -> ToolDeps:
    return ToolDeps(
        storage=storage,
        notifier=notifier,
        status_fn=lambda: {"app": "agentos", "task_counts": {}},
        search_fn=lambda query, count: [
            {"title": f"Result for {query}", "href": "https://example.com/a", "body": "A snippet."}
        ],
        fetch_fn=lambda url: f"fetched:{url}",
        files_dir=settings.files_dir,
        files_max_bytes=settings.files_max_bytes,
        files_max_entries=settings.files_max_entries,
        web_fetch_max_chars=settings.web_fetch_max_chars,
    )
