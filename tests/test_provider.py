from __future__ import annotations

import httpx
import pytest

from config.logging import redact
from config.settings import LLMEndpoint
from freellmapi_adapter import ErrorKind, LLMError, LLMExhausted, LLMRouter

PAYLOAD = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


def make_router(handler, endpoints=None, attempts=1, **kwargs) -> LLMRouter:
    endpoints = endpoints or [
        LLMEndpoint(base_url="http://one.test/v1", api_key="freellmapi-secret-one", model="model-a"),
        LLMEndpoint(base_url="http://two.test/v1", api_key="freellmapi-secret-two", model="model-b"),
    ]
    return LLMRouter(
        endpoints,
        transport=httpx.MockTransport(handler),
        attempts=attempts,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_success_returns_body_and_uses_endpoint_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    router = make_router(handler, endpoints=[LLMEndpoint(base_url="http://one.test/v1", api_key="k1", model="model-a")])
    result = router.chat(PAYLOAD)
    router.close()

    assert result["choices"][0]["message"]["content"] == "ok"
    assert seen["url"] == "http://one.test/v1/chat/completions"
    assert seen["auth"] == "Bearer k1"
    assert b'"model": "model-a"' in seen["body"] or b'"model":"model-a"' in seen["body"]


def test_falls_back_to_second_endpoint_on_server_error():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "one.test":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "second"}}]})

    router = make_router(handler)
    result = router.chat(PAYLOAD)
    health = router.health()
    router.close()

    assert result["choices"][0]["message"]["content"] == "second"
    assert calls == ["one.test", "two.test"]
    assert health[0]["last_error"] is not None
    assert health[1]["success_count"] == 1


def test_auth_error_skips_endpoint_and_reports_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "one.test":
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    router = make_router(handler)
    router.chat(PAYLOAD)
    health = router.health()
    router.close()

    assert "401" in health[0]["last_error"]
    assert health[0]["state"] == "cooldown"


def test_all_endpoints_failing_raises_exhausted_with_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    router = make_router(handler)
    with pytest.raises(LLMExhausted) as excinfo:
        router.chat(PAYLOAD)
    router.close()

    assert len(excinfo.value.failures) == 2
    assert all(f.kind == ErrorKind.SERVER for f in excinfo.value.failures)


def test_timeout_is_classified_and_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    router = make_router(handler, attempts=1, endpoints=[LLMEndpoint(base_url="http://one.test/v1", model="m")])
    with pytest.raises(LLMExhausted) as excinfo:
        router.chat(PAYLOAD)
    router.close()

    assert excinfo.value.failures[0].kind == ErrorKind.TIMEOUT
    assert excinfo.value.failures[0].retryable is True


def test_tool_choice_dropped_after_http_400():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        bodies.append(body)
        if b"tool_choice" in body:
            return httpx.Response(400, text="unsupported tool_choice value")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    router = make_router(handler, endpoints=[LLMEndpoint(base_url="http://one.test/v1", model="m")])
    result = router.chat({**PAYLOAD, "tool_choice": "required"})
    router.close()

    assert result["choices"]
    assert b"tool_choice" in bodies[0]
    assert b"tool_choice" not in bodies[1]


def test_invalid_json_response_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", headers={"content-type": "application/json"})

    router = make_router(handler, endpoints=[LLMEndpoint(base_url="http://one.test/v1", model="m")])
    with pytest.raises(LLMExhausted) as excinfo:
        router.chat(PAYLOAD)
    router.close()

    assert excinfo.value.failures[0].kind == ErrorKind.INVALID_RESPONSE


def test_empty_endpoint_list_raises_config_error():
    router = LLMRouter([], sleep=lambda _s: None)
    with pytest.raises(LLMError) as excinfo:
        router.chat(PAYLOAD)
    router.close()

    assert excinfo.value.kind == ErrorKind.CONFIG


def test_cooled_endpoint_is_tried_last():
    order = []
    state = {"fail_two": False}

    def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.url.host)
        if request.url.host == "one.test":
            return httpx.Response(500, text="down")
        if state["fail_two"]:
            return httpx.Response(503, text="down too")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    router = make_router(handler, cooldown_seconds=60.0)
    router.chat(PAYLOAD)
    assert order == ["one.test", "two.test"]

    order.clear()
    state["fail_two"] = True
    with pytest.raises(LLMExhausted):
        router.chat(PAYLOAD)
    router.close()

    assert order == ["two.test", "one.test"]


def test_redact_masks_common_secret_shapes():
    text = "key freellmapi-deadbeef1234 and Bearer sk-abcdefghijklmnop and api_key=sk-xyz123456789"
    redacted = redact(text)
    assert "freellmapi-deadbeef1234" not in redacted
    assert "sk-xyz123456789" not in redacted
    assert "***" in redacted
