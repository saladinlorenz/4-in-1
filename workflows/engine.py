from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

from freellmapi_adapter.fallback import backoff_delay
from memory import Storage, TaskStatus

from .validation import parse_workflow

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATUSES = ("PENDING", "RUNNING", "WAITING_CONFIRMATION")
TERMINAL_RUN_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")


def _default_schedule(delay: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


class WorkflowEngine:
    """Persistent workflow runs executed by the existing AgentRunner worker.

    Responsibilities:
    - store definitions (``workflows``), runs and steps in SQLite;
    - drive one step at a time through ``runner.submit_existing``;
    - pause on confirmation steps until the operator decides;
    - retry failed steps with backoff (``retry_limit``);
    - resume incomplete runs after a restart (``resume``).

    The engine contains no business logic of its own: every agent step is an
    ordinary task executed by the single-worker AgentRunner pool.
    """

    def __init__(
        self,
        storage: Storage,
        runner: object,
        *,
        retry_limit: int = 1,
        retry_backoff_base: float = 1.5,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
    ) -> None:
        self.storage = storage
        self.runner = runner
        self.retry_limit = retry_limit
        self.retry_backoff_base = retry_backoff_base
        self._schedule = schedule or _default_schedule
        self._lock = threading.RLock()

    # --- definitions -----------------------------------------------------

    def register(self, name: str, steps: list[dict]) -> int:
        """Store a workflow definition; strict closed-schema validation.

        Raises ``ValueError`` on any unknown key, bad kind, empty prompt or
        out-of-range step count (both API and agent-tool paths go through it).
        """
        clean_name, normalized = parse_workflow({"name": name, "steps": steps})
        return self.storage.save_workflow(clean_name, json.dumps(normalized, ensure_ascii=False))

    # --- cancel ----------------------------------------------------------

    def cancel(self, run_id: int) -> tuple[bool, str]:
        """Cancel an active run from the dashboard (same paths as Telegram).

        - ``WAITING_CONFIRMATION``: reject the pending confirmation (single
          gate: ``runner.resolve_confirmation`` fires the listeners);
        - ``RUNNING``: cancel the current step task, then force the run
          terminal if the listener has not caught up;
        - ``PENDING``: mark the run cancelled directly.
        """
        with self._lock:
            run = self.storage.get_run(run_id)
            if run is None:
                return False, "unknown run"
            status = str(run["status"])
            if status in TERMINAL_RUN_STATUSES:
                return False, f"run already {status}"
            if status == TaskStatus.WAITING_CONFIRMATION.value:
                confirmation = self.storage.find_confirmation(
                    "workflow_run", _workflow_payload(run_id, run["current_step"])
                )
                if confirmation is not None and confirmation["status"] == "PENDING":
                    ok, reason = self.runner.resolve_confirmation(
                        int(confirmation["id"]), "REJECTED"
                    )
                    if not ok:
                        return False, str(reason)
                    fresh = self.storage.get_run(run_id)
                    if fresh is not None and fresh["status"] not in TERMINAL_RUN_STATUSES:
                        self._finish(run_id, TaskStatus.CANCELLED, error="confirmation rejected")
                    return True, "cancelled"
                self._finish(run_id, TaskStatus.CANCELLED, error="cancelled by operator")
                return True, "cancelled"
            if status == TaskStatus.RUNNING.value:
                step = self.storage.get_step(run_id, int(run["current_step"]))
                if step is not None and step.get("task_id") and step["status"] == "RUNNING":
                    ok, reason = self.runner.cancel(int(step["task_id"]))
                    fresh = self.storage.get_run(run_id)
                    if (
                        fresh is not None
                        and fresh["status"] not in TERMINAL_RUN_STATUSES
                        and int(fresh["current_step"]) == int(run["current_step"])
                    ):
                        self.storage.update_step(step["id"], status="CANCELLED")
                        self._finish(run_id, TaskStatus.CANCELLED, error="step cancelled")
                    if not ok and reason:
                        return True, str(reason)
                    return True, "cancelled"
            self._finish(run_id, TaskStatus.CANCELLED, error="cancelled by operator")
            return True, "cancelled"

    # --- enqueue ---------------------------------------------------------

    def enqueue(
        self,
        name: str,
        *,
        context: str = "",
        idempotency_key: str | None = None,
    ) -> int | None:
        workflow = self.storage.get_workflow(name)
        if workflow is None:
            logger.warning("enqueue refused: unknown workflow %s", name)
            return None
        if idempotency_key:
            existing = self.storage.active_run_with_key(idempotency_key)
            if existing is not None:
                logger.info(
                    "enqueue deduplicated: run %s already active for key %s",
                    existing["id"],
                    idempotency_key,
                )
                return existing["id"]
        run_id = self.storage.create_run(
            workflow["id"], name, context=context, idempotency_key=idempotency_key
        )
        self._start_step(run_id, 0)
        return run_id

    # --- listeners (wired by main) --------------------------------------

    def on_task_finished(self, task_id: int, status: str, result: str | None) -> None:
        step = self.storage.step_by_task(task_id)
        if step is None:
            return
        with self._lock:
            step = self.storage.step_by_task(task_id)
            if step is None or step["task_id"] != task_id:
                return
            run = self.storage.get_run(step["run_id"])
            if run is None or run["status"] not in ("PENDING", "RUNNING"):
                return
            if status == "SUCCESS":
                self.storage.update_step(step["id"], status="SUCCESS", result=result)
                self._start_step(run["id"], step["position"] + 1, previous_result=result)
            elif status == "CANCELLED":
                self.storage.update_step(step["id"], status="CANCELLED", last_error=result)
                self._finish(run["id"], TaskStatus.CANCELLED, error="step cancelled")
            else:
                self._retry_or_fail(run, step, result)

    def on_confirmation_resolved(self, confirmation_id: int, decision: str) -> None:
        confirmation = self.storage.get_confirmation(confirmation_id)
        if confirmation is None or confirmation["kind"] != "workflow_run":
            return
        parsed = _parse_workflow_payload(confirmation["payload"])
        if parsed is None:
            logger.warning("workflow confirmation %s has an invalid payload", confirmation_id)
            return
        run_id, position = parsed
        with self._lock:
            run = self.storage.get_run(run_id)
            if run is None or run["status"] != TaskStatus.WAITING_CONFIRMATION.value:
                return
            if decision == "APPROVED":
                self.storage.update_run(run_id, status=TaskStatus.RUNNING.value)
                self._start_step(
                    run_id, position + 1, previous_result=f"Confirmation #{confirmation_id} approved"
                )
            else:
                self._finish(run_id, TaskStatus.CANCELLED, error=f"confirmation {decision.lower()}")

    # --- restart recovery ------------------------------------------------

    def resume(self) -> dict[str, int]:
        counts = {"waiting": 0, "advanced": 0, "restarted": 0, "finished": 0, "requeued": 0}
        for run in self.storage.list_runs(limit=1000):
            if run["status"] in TERMINAL_RUN_STATUSES:
                continue
            with self._lock:
                self._resume_run(run["id"], counts)
        if any(counts.values()):
            logger.info(
                "workflow resume: waiting=%s advanced=%s restarted=%s finished=%s requeued=%s",
                counts["waiting"],
                counts["advanced"],
                counts["restarted"],
                counts["finished"],
                counts["requeued"],
            )
        return counts

    def _resume_run(self, run_id: int, counts: dict[str, int]) -> None:
        run = self.storage.get_run(run_id)
        if run is None or run["status"] in TERMINAL_RUN_STATUSES:
            return
        if run["status"] == "PENDING":
            self._start_step(run_id, run["current_step"])
            counts["restarted"] += 1
            return
        if run["status"] == TaskStatus.WAITING_CONFIRMATION.value:
            self._resume_waiting_run(run, counts)
            return
        self._resume_running_run(run, counts)

    def _resume_waiting_run(self, run: dict, counts: dict[str, int]) -> None:
        run_id = run["id"]
        position = run["current_step"]
        confirmation = self.storage.find_confirmation(
            "workflow_run", _workflow_payload(run_id, position)
        )
        if confirmation is None:
            self.storage.update_run(run_id, status=TaskStatus.RUNNING.value)
            self._start_step(run_id, position)
            counts["restarted"] += 1
        elif confirmation["status"] == "PENDING":
            counts["waiting"] += 1
        elif confirmation["status"] == "APPROVED":
            self.storage.update_run(run_id, status=TaskStatus.RUNNING.value)
            self._start_step(run_id, position + 1, previous_result="Confirmation approved")
            counts["advanced"] += 1
        else:
            self._finish(
                run_id, TaskStatus.CANCELLED, error=f"confirmation {confirmation['status'].lower()}"
            )
            counts["finished"] += 1

    def _resume_running_run(self, run: dict, counts: dict[str, int]) -> None:
        run_id = run["id"]
        position = run["current_step"]
        steps = self._steps_of(run)
        if position >= len(steps):
            self._finish(run_id, TaskStatus.SUCCESS, result=run.get("result"))
            counts["finished"] += 1
            return
        step = self.storage.get_step(run_id, position)
        if step is None:
            self._start_step(run_id, position)
            counts["restarted"] += 1
            return
        if step["status"] == "SUCCESS":
            self._start_step(run_id, position + 1, previous_result=step.get("result"))
            counts["advanced"] += 1
            return
        if step["status"] in ("FAILED", "CANCELLED"):
            error = step.get("last_error") or "step did not complete"
            self._finish(
                run_id,
                TaskStatus.FAILED if step["status"] == "FAILED" else TaskStatus.CANCELLED,
                error=error,
            )
            counts["finished"] += 1
            return
        if not step["task_id"]:
            self._start_step(run_id, position)
            counts["restarted"] += 1
            return
        task = self.storage.get_task(int(step["task_id"]))
        if task is None:
            self._start_step(run_id, position)
            counts["restarted"] += 1
        elif task["status"] in ("PENDING", "RUNNING"):
            counts["requeued"] += 1
        else:
            detail = task.get("result") if task["status"] == "SUCCESS" else task.get("error")
            self.on_task_finished(int(task["id"]), task["status"], detail)
            counts["advanced" if task["status"] == "SUCCESS" else "finished"] += 1

    # --- internals -------------------------------------------------------

    def _start_step(
        self, run_id: int, position: int, previous_result: str | None = None
    ) -> None:
        with self._lock:
            run = self.storage.get_run(run_id)
            if run is None or run["status"] not in ("PENDING", "RUNNING"):
                return
            steps = self._steps_of(run)
            if position >= len(steps):
                result = previous_result if previous_result is not None else run.get("result")
                self._finish(run_id, TaskStatus.SUCCESS, result=result)
                return
            step_def = steps[position]
            step = self.storage.get_step(run_id, position)
            if step is None:
                self.storage.create_step(run_id, position, step_def["name"], step_def["kind"])
                step = self.storage.get_step(run_id, position)
            self.storage.update_run(run_id, status=TaskStatus.RUNNING.value, current_step=position)
            if step_def["kind"] == "confirmation":
                self._hold_for_confirmation(run, position, step, step_def)
                return
            prompt = self._render(step_def, run, self._previous_result(run, position))
            if step["status"] == "RUNNING" and step["task_id"]:
                task = self.storage.get_task(int(step["task_id"]))
                if task and task["status"] in ("PENDING", "RUNNING"):
                    return
            task_id = self.storage.create_task(prompt, kind="workflow_step")
            self.storage.update_step(step["id"], status="RUNNING", task_id=task_id, result=None)
            self.runner.submit_existing(task_id, prompt)

    def _hold_for_confirmation(
        self, run: dict, position: int, step: dict, step_def: dict
    ) -> None:
        run_id = run["id"]
        payload = _workflow_payload(run_id, position)
        existing = self.storage.find_confirmation("workflow_run", payload)
        if existing is not None:
            if existing["status"] == "PENDING":
                self.storage.update_run(run_id, status=TaskStatus.WAITING_CONFIRMATION.value)
            else:
                self.storage.update_run(run_id, status=TaskStatus.RUNNING.value)
                if existing["status"] == "APPROVED":
                    self._start_step(run_id, position + 1, previous_result="Confirmation approved")
                else:
                    self._finish(
                        run_id,
                        TaskStatus.CANCELLED,
                        error=f"confirmation {existing['status'].lower()}",
                    )
            return
        self.storage.update_step(step["id"], status="PENDING", result=None)
        # Create the confirmation BEFORE flipping the run status: readers that
        # observe WAITING_CONFIRMATION are then guaranteed to find it.
        confirmation_id = self.storage.create_confirmation("workflow_run", payload)
        self.storage.update_run(run_id, status=TaskStatus.WAITING_CONFIRMATION.value)
        label = step_def.get("name") or f"step {position}"
        prompt = step_def.get("prompt") or "Proceed?"
        self.runner.notify(
            f"Workflow run #{run_id}: confirmation required ({label}): {prompt}\n"
            f"Approve with /approve {confirmation_id} or reject with /reject {confirmation_id}"
        )

    def _restart_step(self, run_id: int, position: int) -> None:
        with self._lock:
            run = self.storage.get_run(run_id)
            if run is None or run["status"] != TaskStatus.RUNNING.value:
                return
            steps = self._steps_of(run)
            if position >= len(steps):
                return
            step = self.storage.get_step(run_id, position)
            if step is None or step["status"] == "SUCCESS":
                return
            step_def = steps[position]
            prompt = self._render(step_def, run, self._previous_result(run, position))
            task_id = self.storage.create_task(prompt, kind="workflow_step")
            self.storage.update_step(step["id"], status="RUNNING", task_id=task_id)
            self.runner.submit_existing(task_id, prompt)

    def _retry_or_fail(self, run: dict, step: dict, error: str | None) -> None:
        detail = error or "step failed"
        if step["retry_count"] < self.retry_limit:
            retry = int(step["retry_count"]) + 1
            self.storage.update_step(
                step["id"], status="PENDING", retry_count=retry, last_error=detail
            )
            delay = backoff_delay(retry, base=self.retry_backoff_base)
            logger.info(
                "workflow run %s step %s retry %s/%s in %.2fs",
                run["id"],
                step["position"],
                retry,
                self.retry_limit,
                delay,
            )
            self._schedule(
                delay,
                lambda run_id=run["id"], position=step["position"]: self._restart_step(
                    run_id, position
                ),
            )
            return
        self.storage.update_step(step["id"], status="FAILED", last_error=detail)
        self._finish(run["id"], TaskStatus.FAILED, error=detail)

    def _finish(
        self,
        run_id: int,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            run = self.storage.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                return
            self.storage.update_run(run_id, status=status.value, result=result, error=error)
            name = run["workflow_name"]
        suffix = f" — {error}" if error else ""
        self.runner.notify(f"Workflow '{name}' run #{run_id}: {status.value}{suffix}")

    def _steps_of(self, run: dict) -> list[dict]:
        workflow = self.storage.get_workflow(run["workflow_name"])
        if workflow is None:
            logger.error("workflow definition %s is missing", run["workflow_name"])
            return []
        try:
            steps = json.loads(workflow["steps"])
        except ValueError:
            logger.error("workflow %s has invalid steps JSON", run["workflow_name"])
            return []
        return steps if isinstance(steps, list) else []

    def _previous_result(self, run: dict, position: int) -> str | None:
        if position <= 0:
            return None
        previous = self.storage.get_step(run["id"], position - 1)
        if previous is None:
            return None
        value = previous.get("result")
        return str(value) if value else None

    @staticmethod
    def _render(step_def: dict, run: dict, previous_result: str | None) -> str:
        prompt = str(step_def.get("prompt") or "").replace("{context}", str(run.get("context") or ""))
        if previous_result:
            prompt += "\n\nPrevious step result:\n" + previous_result[:2000]
        return prompt


def _workflow_payload(run_id: int, position: int) -> str:
    return f"workflow_run#{run_id}.step{position}"


def _parse_workflow_payload(payload: str) -> tuple[int, int] | None:
    run_part, separator, step_part = payload.partition(".step")
    if not separator or not run_part.startswith("workflow_run#"):
        return None
    try:
        run_id = int(run_part.removeprefix("workflow_run#"))
        position = int(step_part)
    except ValueError:
        return None
    return run_id, position
