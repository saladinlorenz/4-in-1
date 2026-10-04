from __future__ import annotations

import importlib.resources
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yaml
from smolagents import ToolCallingAgent

from config.logging import redact
from config.settings import Settings
from freellmapi_adapter import LLMRouter
from memory import Storage, TaskStatus

from .backends import get_backend
from .model_router import RoutedModel
from .permissions import ALLOWED_TOOL_NAMES, filter_tools
from .tools import ToolDeps, build_tools, default_fetch, default_search

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4000
PROMPT_RESOURCE = "smolagents.prompts"


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
        agent_config: Any | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.router = router
        self.notifier = notifier
        self.agent_config = agent_config
        self.started_at = time.time()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-run")
        self._lock = threading.Lock()
        self._current: tuple[int, Any] | None = None
        self._active_task_id: int | None = None
        self.task_listeners: list[Callable[[int, str, str | None], None]] = []
        self.confirmation_listeners: list[Callable[[int, str], None]] = []
        self.workflow_engine: Any = None
        self._search_fn = search_fn or (
            lambda query, count: default_search(query, count, settings.web_search_timeout)
        )
        self._fetch_fn = fetch_fn or (
            lambda url: default_fetch(url, settings.web_fetch_timeout, settings.web_fetch_max_bytes)
        )
        self._agent_factory = agent_factory or self._build_backend_agent

    def _build_backend_agent(self) -> Any:
        """Resolve the active backend (settings) and build its runnable agent.

        Exactly one backend runs per task: unknown or unavailable backends
        raise instead of silently falling back to a second engine.
        """
        name = "smolagents"
        if self.agent_config is not None:
            try:
                name = str(self.agent_config.effective().get("backend") or "smolagents")
            except Exception:
                logger.warning("cannot read backend setting, using smolagents", exc_info=True)
        backend = get_backend(name)
        if backend is None:
            raise RuntimeError(f"agent backend unavailable: {name}")
        ok, reason = backend.availability()
        if not ok:
            raise RuntimeError(f"agent backend {name} unavailable: {reason}")
        return backend.build(self)

    def _agent_options(self) -> dict[str, Any]:
        """Effective agent options: `.env` defaults overridden by SQLite settings.

        Without an ``agent_config`` service (tests, minimal wiring) the pure
        `.env` defaults apply unchanged.
        """
        defaults = {
            "system_prompt": "",
            "max_steps": self.settings.agent_max_steps,
            "max_output_chars": self.settings.agent_max_output_chars,
            "temperature": 0.3,
            "allowed_tools": frozenset(ALLOWED_TOOL_NAMES),
        }
        if self.agent_config is None:
            return defaults
        try:
            config = self.agent_config.effective()
            allowed = self.agent_config.allowed_tools()
        except Exception:
            logger.warning("agent settings unreadable, using defaults", exc_info=True)
            return defaults
        return {
            "system_prompt": str(config.get("system_prompt") or ""),
            "max_steps": int(config.get("max_steps") or defaults["max_steps"]),
            "max_output_chars": int(
                config.get("max_output_chars") or defaults["max_output_chars"]
            ),
            "temperature": float(config.get("temperature", defaults["temperature"])),
            "allowed_tools": allowed,
        }

    @staticmethod
    def _prompt_templates(system_prompt: str) -> dict[str, Any]:
        """Default ToolCallingAgent templates + custom instructions appended."""
        templates = yaml.safe_load(
            importlib.resources.files(PROMPT_RESOURCE)
            .joinpath("toolcalling_agent.yaml")
            .read_text()
        )
        custom = system_prompt.strip()
        if custom:
            templates["system_prompt"] = templates["system_prompt"].rstrip() + "\n\n" + custom
        return templates

    def _build_agent(self) -> ToolCallingAgent:
        active_task_id = self._active_task_id
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
            request_confirmation=(
                (lambda kind, payload: self.request_confirmation(active_task_id, kind, payload))
                if active_task_id is not None
                else None
            ),
            generate_fn=self._generate_text,
            workflow_create_fn=self._workflow_create,
            workflow_run_fn=self._workflow_run,
            workflow_runs_fn=self._workflow_runs,
        )
        options = self._agent_options()
        tools = filter_tools(build_tools(deps), allowed=options["allowed_tools"])
        model = RoutedModel(
            self.router,
            model_id="auto",
            tool_choice=self.settings.llm_tool_choice,
            temperature=options["temperature"],
        )
        return ToolCallingAgent(
            tools=tools,
            model=model,
            max_steps=options["max_steps"],
            prompt_templates=self._prompt_templates(options["system_prompt"]),
        )

    def submit(self, prompt: str, *, chat_id: int | None = None, kind: str = "chat") -> int:
        task_id = self.storage.create_task(prompt, kind=kind, chat_id=chat_id)
        self._executor.submit(self._run_task, task_id, prompt, chat_id)
        return task_id

    def submit_existing(self, task_id: int, prompt: str, chat_id: int | None = None) -> None:
        """Queue a task row that already exists (used by the workflow engine)."""
        self._executor.submit(self._run_task, task_id, prompt, chat_id)

    def add_task_listener(self, listener: Callable[[int, str, str | None], None]) -> None:
        self.task_listeners.append(listener)

    def add_confirmation_listener(self, listener: Callable[[int, str], None]) -> None:
        self.confirmation_listeners.append(listener)

    def notify(self, text: str) -> None:
        """Public notification path (shared with the workflow engine)."""
        self._notify(text)

    def _emit_task(self, task_id: int, status: str, result: str | None) -> None:
        for listener in list(self.task_listeners):
            try:
                listener(task_id, status, result)
            except Exception:
                logger.warning("task listener failed for task %s", task_id, exc_info=True)

    def _emit_confirmation(self, confirmation_id: int, decision: str) -> None:
        for listener in list(self.confirmation_listeners):
            try:
                listener(confirmation_id, decision)
            except Exception:
                logger.warning(
                    "confirmation listener failed for confirmation %s",
                    confirmation_id,
                    exc_info=True,
                )

    # --- model / workflow helpers exposed to agent tools -----------------

    def _generate_text(self, prompt: str) -> str:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        raw = self.router.chat(payload)
        choices = raw.get("choices") or []
        if not choices:
            raise RuntimeError("model returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if content is None:
            raise RuntimeError("model returned no content")
        return str(content)[:16_000]

    def _workflow_create(self, name: str, steps: list[dict]) -> int:
        engine = self.workflow_engine
        if engine is None:
            raise RuntimeError("workflow engine is not attached")
        return int(engine.register(name, steps))

    def _workflow_run(self, name: str, idempotency_key: str | None) -> int | None:
        engine = self.workflow_engine
        if engine is None:
            raise RuntimeError("workflow engine is not attached")
        return engine.enqueue(name, idempotency_key=idempotency_key)

    def _workflow_runs(self, workflow: str, limit: int) -> list[dict]:
        rows = self.storage.list_runs(limit=limit)
        if workflow:
            rows = [row for row in rows if row.get("workflow_name") == workflow]
        return rows

    def resume(self) -> dict[str, int]:
        """Restart recovery: requeue work left behind by a previous process.

        Safety against double execution relies on the atomic reservation in
        ``Storage.mark_running`` (UPDATE ... WHERE status = 'PENDING'): whoever
        flips the row first runs the task, any other queued attempt skips it.
        """
        recovery = self.storage.reset_interrupted_tasks()
        pending = self.storage.list_pending_tasks()
        for row in pending:
            self._executor.submit(self._run_task, int(row["id"]), row["prompt"], row["chat_id"])
        stats = {
            "requeued_running": recovery["requeued"],
            "cancelled_running": recovery["cancelled"],
            "resumed_pending": len(pending),
        }
        if any(stats.values()):
            logger.info(
                "resume: %s running requeued, %s running cancelled, %s pending resubmitted",
                stats["requeued_running"],
                stats["cancelled_running"],
                stats["resumed_pending"],
            )
        return stats

    def request_confirmation(self, task_id: int, kind: str, payload: str) -> int | None:
        """Park a RUNNING task until the operator approves or rejects the pending action.

        Returns the confirmation id, or None when the task is not RUNNING
        (a sensitive action can never be requested outside a live run).
        """
        if not self.storage.hold_task(task_id):
            logger.warning("confirmation refused: task %s is not running", task_id)
            return None
        confirmation_id = self.storage.create_confirmation(kind, payload, task_id=task_id)
        self._notify(
            f"Confirmation #{confirmation_id} required ({kind}): {payload}\n"
            f"Approve with /approve {confirmation_id} or reject with /reject {confirmation_id}"
        )
        return confirmation_id

    def expire_stale_confirmations(self, cutoff_iso: str) -> int:
        """Expire every confirmation older than ``cutoff_iso`` (full path: listeners + tasks)."""
        expired = 0
        for row in self.storage.list_confirmations(limit=500, status="PENDING"):
            if str(row["created_at"]) < cutoff_iso:
                ok, _ = self.expire_confirmation(int(row["id"]))
                if ok:
                    expired += 1
        return expired

    def resolve_confirmation(self, confirmation_id: int, decision: str) -> tuple[bool, str]:
        """Approve or reject a pending confirmation (APPROVED / REJECTED).

        Thread-safe: Telegram handlers must call it through ``asyncio.to_thread``
        so notifications never block the event loop.
        """
        confirmation = self.storage.get_confirmation(confirmation_id)
        if confirmation is None:
            return False, "unknown_confirmation"
        if decision not in ("APPROVED", "REJECTED"):
            return False, "invalid_decision"
        if not self.storage.decide_confirmation(confirmation_id, decision):
            return False, f"already_{confirmation['status'].lower()}"
        self._emit_confirmation(confirmation_id, decision)
        reason = decision.lower()
        task_id = confirmation.get("task_id")
        if task_id:
            task = self.storage.get_task(task_id)
            if task and task["status"] == TaskStatus.WAITING_CONFIRMATION.value:
                if decision == "APPROVED":
                    if self.storage.release_task(task_id, TaskStatus.PENDING):
                        self._executor.submit(
                            self._run_task, task_id, task["prompt"], task["chat_id"]
                        )
                        self._notify(
                            f"Confirmation #{confirmation_id} approved: resuming task #{task_id}."
                        )
                else:
                    if self.storage.release_task(
                        task_id, TaskStatus.CANCELLED, error="confirmation rejected"
                    ):
                        self._notify(
                            f"Confirmation #{confirmation_id} rejected: task #{task_id} cancelled."
                        )
        return True, reason

    def expire_confirmation(self, confirmation_id: int) -> tuple[bool, str]:
        """Expire a pending confirmation; a waiting task is cancelled with it."""
        confirmation = self.storage.get_confirmation(confirmation_id)
        if confirmation is None:
            return False, "unknown_confirmation"
        if not self.storage.expire_confirmation(confirmation_id):
            return False, f"already_{confirmation['status'].lower()}"
        self._emit_confirmation(confirmation_id, "EXPIRED")
        task_id = confirmation.get("task_id")
        if task_id:
            task = self.storage.get_task(task_id)
            if (
                task
                and task["status"] == TaskStatus.WAITING_CONFIRMATION.value
                and self.storage.release_task(task_id, TaskStatus.CANCELLED, error="confirmation expired")
            ):
                self._notify(f"Confirmation #{confirmation_id} expired: task #{task_id} cancelled.")
        return True, "expired"

    def _run_task(self, task_id: int, prompt: str, chat_id: int | None) -> None:
        if not self.storage.mark_running(task_id):
            logger.info("task %s skipped: no longer pending", task_id)
            return
        agent: Any = None
        try:
            self._active_task_id = task_id
            agent = self._agent_factory()
            self._active_task_id = None
            with self._lock:
                self._current = (task_id, agent)
            if self.storage.is_cancel_requested(task_id):
                agent.interrupt_switch = True
            answer = agent.run(prompt)
            text = str(answer)
            limit = self._agent_options()["max_output_chars"]
            if len(text) > limit:
                text = text[:limit] + "\n...[truncated]"
            self.storage.add_message("user", prompt, chat_id=chat_id, task_id=task_id)
            self.storage.add_message("assistant", text, chat_id=chat_id, task_id=task_id)
            if self.storage.finish_task(task_id, TaskStatus.SUCCESS, result=text):
                self._emit_task(task_id, "SUCCESS", text)
                self._notify(f"Task #{task_id} completed\n{text}")
            else:
                logger.info("task %s finished but was already terminal", task_id)
        except Exception as exc:
            detail = redact(f"{type(exc).__name__}: {exc}")[:1000]
            cancelled = self.storage.is_cancel_requested(task_id)
            if cancelled:
                self.storage.finish_task(task_id, TaskStatus.CANCELLED, error=detail)
                self._emit_task(task_id, "CANCELLED", detail)
                self._notify(f"Task #{task_id} cancelled.")
                logger.info("task %s cancelled: %s", task_id, detail)
            else:
                self.storage.finish_task(task_id, TaskStatus.FAILED, error=detail)
                self.storage.add_incident("agent", f"task {task_id} failed", detail)
                self._emit_task(task_id, "FAILED", detail)
                self._notify(f"Task #{task_id} failed: {detail}")
                logger.warning("task %s failed: %s", task_id, detail)
        finally:
            self._active_task_id = None
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
