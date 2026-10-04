from __future__ import annotations

import json

import pytest
from smolagents import MessageRole, tool

from agent.model_router import RoutedModel
from freellmapi_adapter import ErrorKind, LLMError


@tool
def sample_tool(text: str) -> str:
    """Return the given text.

    Args:
        text: Text to echo back.
    """
    return text


class StubRouter:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.payloads: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


def test_generate_returns_content_message_with_usage():
    router = StubRouter(
        response={
            "choices": [{"message": {"role": "assistant", "content": "bonjour"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    model = RoutedModel(router, model_id="auto")
    message = model.generate([{"role": "user", "content": "salut"}])

    assert message.role == MessageRole.ASSISTANT
    assert message.content == "bonjour"
    assert message.token_usage.input_tokens == 12
    assert message.token_usage.output_tokens == 3
    assert router.payloads[0]["model"] == "auto"


def test_generate_parses_tool_calls():
    router = StubRouter(
        response={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {"name": "sample_tool", "arguments": json.dumps({"text": "x"})},
                            }
                        ],
                    }
                }
            ]
        }
    )
    model = RoutedModel(router, model_id="auto", tool_choice="required")
    message = model.generate([{"role": "user", "content": "salut"}], tools_to_call_from=[sample_tool])

    assert message.tool_calls is not None
    assert message.tool_calls[0].function.name == "sample_tool"
    assert json.loads(message.tool_calls[0].function.arguments) == {"text": "x"}
    payload = router.payloads[0]
    assert payload["tool_choice"] == "required"
    assert any(tool_def["function"]["name"] == "sample_tool" for tool_def in payload["tools"])


def test_generate_without_tools_omits_tool_choice():
    router = StubRouter(response={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    model = RoutedModel(router, model_id="auto", tool_choice="required")
    model.generate([{"role": "user", "content": "salut"}])

    assert "tool_choice" not in router.payloads[0]
    assert "tools" not in router.payloads[0]


def test_router_errors_propagate():
    router = StubRouter(error=LLMError(ErrorKind.AUTH, "bad key", endpoint="x"))
    model = RoutedModel(router, model_id="auto")

    with pytest.raises(LLMError):
        model.generate([{"role": "user", "content": "salut"}])


def test_stop_sequences_forwarded_when_supported():
    router = StubRouter(response={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    model = RoutedModel(router, model_id="gpt-test")
    model.generate([{"role": "user", "content": "salut"}], stop_sequences=["Observation:"])

    assert router.payloads[0].get("stop") == ["Observation:"]
