from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from smolagents import ToolCallingAgent

from config.logging import redact
from config.settings import Settings
from freellmapi_adapter import LLMRouter
from memory import Storage, TaskStatus

from .model_router import RoutedModel
from .permissions import filter_tools
from .tools import ToolDeps, build_tools, default_fetch, default_search

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4000


def chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    for start in range(0, len(text), limit):
        chunks.append(text[start : start + limit])
    return chunks


class AgentRunner:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        router: LLMRouter,
        notifier: Any,
        *,
        agent_factory: Callable[[], Any] | None = None,
        search_fn: Callable[[str, int], list[dict]] | None = None,
        fetch_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.router = router
        self.notifier = notifier
        self.started_at = time.time()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-run")
        self._lock = threading.Lock()
        self._current: tuple[int, Any] | None = None
        self._search_fn = search_fn or (
            lambda query, count: default_search(query, count, settings.web_search_timeout)
        )
        self._fetch_fn = fetch_fn or (
            lambda url: default_fetch(url, settings.web_fetch_timeout, settings.web_fetch_max_bytes)
        )
        self._agent_factory = agent_factory or self._build_agent

    def _build_agent(self) -> ToolCallingAgent:
        deps = ToolDeps(
            storage=self.storage,
            notifier=self.notifier,
            status_fn=self.status,
            search_fn=self._search_fn,
            fetch_fn=self._fetch_fn,
            files_dir=self.settings.files_dir,
            files_max_bytes=self.settings.files_max_bytes,
            files_max_entries=self.settings.files_max_entries,
            web_fetch_max_chars=self.settings.web_fetch_max_chars,
        )
        tools = filter_tools(build_tools(deps))
        model = RoutedModel(
            self.router,
            model_id="auto",
            tool_choice=self.settings.llm_tool_choice,
            temperature=0.3,
        )
        return ToolCallingAgent(
            tools=tools,
            model=model,
            max_steps=self.settings.agent_max_steps,
        )

    def submit(self, prompt: str, *, chat_id: int | None = None, kind: str = "chat") -> int:
        task_id = self.storage.create_task(prompt, kind=kind, chat_id=chat_id)
        self._executor.submit(self._run_task, task_id, prompt, chat_id)
        return task_id

    def _run_task(self, task_id: int, prompt: str, chat_id: int | None) -> None:
        if not self.storage.mark_running(task_id):
            logger.info("task %s skipped: no longer pending", task_id)
            return
        agent: Any = None
        try:
            agent = self._agent_factory()
            with self._lock:
                self._current = (task_id, agent)
            if self.storage.is_cancel_requested(task_id):
                agent.interrupt_switch = True
            answer = agent.run(prompt)
            text = str(answer)
            limit = self.settings.agent_max_output_chars
            if len(text) > limit:
                text = text[:limit] + "\n...[truncated]"
            self.storage.add_message("user", prompt, chat_id=chat_id, task_id=task_id)
            self.storage.add_message("assistant", text, chat_id=chat_id, task_id=task_id)
            if self.storage.finish_task(task_id, TaskStatus.SUCCESS, result=text):
                self._notify(f"Task #{task_id} completed\n{text}")
            else:
                logger.info("task %s finished but was already terminal", task_id)
        except Exception as exc:
            detail = redact(f"{type(exc).__name__}: {exc}")[:1000]
            cancelled = self.storage.is_cancel_requested(task_id)
            if cancelled:
                self.storage.finish_task(task_id, TaskStatus.CANCELLED, error=detail)
                self._notify(f"Task #{task_id} cancelled.")
                logger.info("task %s cancelled: %s", task_id, detail)
            else:
                self.storage.finish_task(task_id, TaskStatus.FAILED, error=detail)
                self.storage.add_incident("agent", f"task {task_id} failed", detail)
                self._notify(f"Task #{task_id} failed: {detail}")
                logger.warning("task %s failed: %s", task_id, detail)
        finally:
            with self._lock:
                self._current = None

    def cancel(self, task_id: int) -> tuple[bool, str]:
        ok, reason = self.storage.cancel_task(task_id)
        if reason == "interrupt_requested":
            with self._lock:
                current = self._current
            if current is not None and current[0] == task_id:
                current[1].interrupt_switch = True
                return True, reason
            return True, reason
        return ok, reason

    def interrupt_current(self) -> None:
        with self._lock:
            current = self._current
        if current is not None:
            current[1].interrupt_switch = True

    def status(self) -> dict[str, Any]:
        with self._lock:
            current_task = self._current[0] if self._current else None
        try:
            counts = self.storage.task_counts()
        except Exception:
            counts = {}
        return {
            "app": self.settings.app_name,
            "version": self.settings.app_version,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "current_task": current_task,
            "task_counts": counts,
            "llm_endpoints": self.router.health(),
            "telegram_configured": bool(self.settings.telegram_bot_token),
        }

    def wait_idle(self, timeout: float | None = None) -> bool:
        future = self._executor.submit(lambda: None)
        try:
            future.result(timeout=timeout)
        except Exception:
            return False
        return True

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _notify(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception:
            logger.warning("notification delivery failed", exc_info=True)
