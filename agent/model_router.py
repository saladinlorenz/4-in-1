from __future__ import annotations

from typing import Any

from smolagents import ChatMessage, MessageRole, Model, TokenUsage

from freellmapi_adapter import LLMRouter


class RoutedModel(Model):
    def __init__(
        self,
        router: LLMRouter,
        model_id: str = "auto",
        tool_choice: str = "required",
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id=model_id, **kwargs)
        self.router = router
        self.tool_choice = tool_choice

    def generate(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            tool_choice=self.tool_choice if tools_to_call_from else None,
            model=self.model_id,
            convert_images_to_image_urls=True,
            **kwargs,
        )
        raw = self.router.chat(completion_kwargs)
        choice = raw["choices"][0]
        message = choice.get("message") or {}
        usage = raw.get("usage") or {}
        token_usage = None
        if usage:
            token_usage = TokenUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        role = message.get("role") or "assistant"
        return ChatMessage(
            role=MessageRole(role),
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
            raw=raw,
            token_usage=token_usage,
        )
