from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class FnNotifier:
    def __init__(self, sender: Callable[[str], None]) -> None:
        self._sender = sender

    def send(self, text: str) -> None:
        self._sender(text)


class LogNotifier:
    def send(self, text: str) -> None:
        logger.info("notification: %s", text.replace("\n", " ")[:500])


class TelegramNotifier:
    def __init__(self, bot: object, chat_id: int, loop: asyncio.AbstractEventLoop, timeout: float = 30.0) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._loop = loop
        self._timeout = timeout

    def send(self, text: str) -> None:
        if not self._chat_id:
            raise RuntimeError("telegram_admin_chat_id is not configured")
        for chunk in _split(text, 4000):
            future = asyncio.run_coroutine_threadsafe(
                self._bot.send_message(chat_id=self._chat_id, text=chunk),  # type: ignore[attr-defined]
                self._loop,
            )
            future.result(timeout=self._timeout)


def _split(text: str, limit: int) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    return [text[start : start + limit] for start in range(0, len(text), limit)]
