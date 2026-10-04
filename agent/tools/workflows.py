from __future__ import annotations

import re

from smolagents import tool

from config.logging import redact

from .base import ToolDeps

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_STEPS = 20
MAX_STEP_CHARS = 4000


def make_tools(deps: ToolDeps) -> list[object]:
    @tool
    def workflow_create(name: str, steps: str) -> str:
        """Register a persistent workflow: sequential agent missions stored in
        SQLite and executed one task at a time (retries and restart recovery
        included). Re-registering an existing name replaces its steps.

        Args:
            name: Unique workflow identifier (letters, digits, '_', '-', '.').
            steps: Missions in execution order, one per line.
        """
        clean = (name or "").strip()
        if not _NAME_RE.fullmatch(clean):
            return "Workflow not created: name must be 1-64 chars (letters, digits, _ - .)."
        prompts = [line.strip() for line in (steps or "").splitlines()]
        if not 1 <= len(prompts) <= MAX_STEPS:
            return f"Workflow not created: provide 1 to {MAX_STEPS} steps (one per line)."
        if any(not line for line in prompts):
            return "Workflow not created: empty step lines are not allowed."
        if any(len(line) > MAX_STEP_CHARS for line in prompts):
            return f"Workflow not created: a step exceeds {MAX_STEP_CHARS} chars."
        if deps.workflow_create_fn is None:
            return "Workflow unavailable: engine not attached in this context."
        step_defs = [
            {"name": f"step{index}", "kind": "agent", "prompt": prompt}
            for index, prompt in enumerate(prompts, start=1)
        ]
        try:
            workflow_id = deps.workflow_create_fn(clean, step_defs)
        except Exception as exc:
            return f"Workflow creation failed: {redact(str(exc))}"
        return f"Workflow '{clean}' saved as #{workflow_id} with {len(prompts)} steps."

    @tool
    def workflow_run(name: str, idempotency_key: str = "") -> str:
        """Start a workflow run. With the same idempotency key an active run is
        joined instead of duplicated (useful for daily schedules).

        Args:
            name: Workflow identifier to run.
            idempotency_key: Optional deduplication key, e.g. daily:2026-10-04.
        """
        clean = (name or "").strip()
        if not clean:
            return "Run not started: workflow name is required."
        if deps.workflow_run_fn is None:
            return "Workflow unavailable: engine not attached in this context."
        key = (idempotency_key or "").strip() or None
        try:
            run_id = deps.workflow_run_fn(clean, key)
        except Exception as exc:
            return f"Run failed to start: {redact(str(exc))}"
        if run_id is None:
            return f"Workflow '{clean}' not found. Create it first with workflow_create."
        joined = " (joined existing active run)" if key else ""
        return f"Run #{run_id} of '{clean}' started{joined}."

    @tool
    def workflow_status(workflow: str = "", limit: int = 5) -> str:
        """List recent workflow runs with their status and current step.

        Args:
            workflow: Optional workflow name filter (empty = all workflows).
            limit: Maximum runs to show, between 1 and 50.
        """
        if deps.workflow_runs_fn is None:
            return "Workflow unavailable: engine not attached in this context."
        count = min(50, max(1, int(limit)))
        try:
            rows = deps.workflow_runs_fn((workflow or "").strip(), count)
        except Exception as exc:
            return f"Workflow status failed: {redact(str(exc))}"
        if not rows:
            return "No workflow runs found."
        lines = []
        for run in rows:
            line = (
                f"run #{run['id']} [{run['status']}] {run['workflow_name']}"
                f" step={run.get('current_step', 0)} created={run.get('created_at', '?')}"
            )
            if run.get("error"):
                line += f" error={str(run['error'])[:120]}"
            lines.append(line)
        return "\n".join(lines)

    return [workflow_create, workflow_run, workflow_status]
