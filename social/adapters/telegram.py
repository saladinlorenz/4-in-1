from __future__ import annotations

import asyncio


class TelegramAdapter:
    """Posts a draft into the configured admin chat through the bot API."""

    platform = "telegram"

    def __init__(
        self,
        bot: object,
        chat_id: int,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._loop = loop
        self._timeout = timeout

    def configured(self) -> tuple[bool, str]:
        if self._bot is not None and self._chat_id:
            return True, "bot + chat_id configurés"
        return False, "bot ou telegram_admin_chat_id absent"

    def test(self) -> str:
        if self._bot is None:
            raise RuntimeError("telegram bot is not configured")
        future = asyncio.run_coroutine_threadsafe(self._bot.get_me(), self._loop)  # type: ignore[attr-defined]
        me = future.result(timeout=self._timeout)
        username = getattr(me, "username", None)
        if not username:
            raise RuntimeError("telegram get_me returned no username")
        return f"authenticated as @{username}"

    def publish(self, content: str) -> str:
        if not self._chat_id:
            raise RuntimeError("telegram_admin_chat_id is not configured")
        text = (content or "")[:4000]
        if not text.strip():
            raise RuntimeError("draft content is empty")
        future = asyncio.run_coroutine_threadsafe(
            self._bot.send_message(chat_id=self._chat_id, text=text),  # type: ignore[attr-defined]
            self._loop,
        )
        message = future.result(timeout=self._timeout)
        message_id = getattr(message, "message_id", None)
        if message_id is None:
            raise RuntimeError("telegram send returned no message id")
        return f"telegram:{message_id}"
