from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    chat_id INTEGER,
    task_id INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'chat',
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','WAITING_CONFIRMATION','SUCCESS','FAILED','CANCELLED')),
    chat_id INTEGER,
    result TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
    decided_at TEXT,
    task_id INTEGER
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    platform TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT','APPROVED','PUBLISHING','PUBLISHED','FAILED')),
    scheduled_for TEXT,
    published_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    steps TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workflow_id INTEGER NOT NULL,
    workflow_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','WAITING_CONFIRMATION','SUCCESS','FAILED','CANCELLED')),
    current_step INTEGER NOT NULL DEFAULT 0,
    context TEXT NOT NULL DEFAULT '',
    result TEXT,
    error TEXT,
    idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'agent',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','SUCCESS','FAILED','CANCELLED')),
    task_id INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    last_error TEXT,
    UNIQUE(run_id, position)
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_steps_task ON workflow_steps(task_id);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    workflow_name TEXT NOT NULL,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_enqueued_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value_json TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system'
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
            with self._lock:
                self._connections.append(connection)
        return connection

    def init_schema(self) -> None:
        with self._conn() as connection:
            connection.executescript(SCHEMA)
            confirmation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(confirmations)")
            }
            if "task_id" not in confirmation_columns:
                connection.execute("ALTER TABLE confirmations ADD COLUMN task_id INTEGER")

    def ping(self) -> bool:
        try:
            self._conn().execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
        self._local = threading.local()

    def add_message(self, role: str, content: str, chat_id: int | None = None, task_id: int | None = None) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (created_at, role, content, chat_id, task_id) VALUES (?, ?, ?, ?, ?)",
                (utc_now(), role, content, chat_id, task_id),
            )
            return int(cursor.lastrowid)

    def recent_messages(self, limit: int = 20, chat_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM messages"
        params: list[Any] = []
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_task(self, prompt: str, kind: str = "chat", chat_id: int | None = None) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO tasks (kind, prompt, status, chat_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, prompt, TaskStatus.PENDING.value, chat_id, utc_now()),
            )
            return int(cursor.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn().execute(query, params).fetchall()]

    def task_counts(self) -> dict[str, int]:
        rows = self._conn().execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def reset_interrupted_tasks(self) -> dict[str, int]:
        """Startup recovery: RUNNING rows cannot have a live process after a restart.

        Two atomic updates act as the reservation lock for recovery:
        - a RUNNING task the user asked to cancel becomes CANCELLED (the cancel wins);
        - any other RUNNING task goes back to PENDING so the worker can pick it up.
        Terminal states (SUCCESS/FAILED/CANCELLED) are never touched.
        """
        now = utc_now()
        with self._conn() as connection:
            cancelled = connection.execute(
                "UPDATE tasks SET status = ?, cancel_requested = 1, finished_at = ?"
                " WHERE status = ? AND cancel_requested = 1",
                (TaskStatus.CANCELLED.value, now, TaskStatus.RUNNING.value),
            ).rowcount
            requeued = connection.execute(
                "UPDATE tasks SET status = ?, started_at = NULL WHERE status = ?",
                (TaskStatus.PENDING.value, TaskStatus.RUNNING.value),
            ).rowcount
        return {"cancelled": cancelled, "requeued": requeued}

    def list_pending_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_tasks(limit, status=TaskStatus.PENDING.value)

    def mark_running(self, task_id: int) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, started_at = ?, cancel_requested = 0 WHERE id = ? AND status = ?",
                (TaskStatus.RUNNING.value, utc_now(), task_id, TaskStatus.PENDING.value),
            )
            return cursor.rowcount == 1

    def finish_task(self, task_id: int, status: TaskStatus, result: str | None = None, error: str | None = None) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ? AND status IN (?, ?)",
                (
                    status.value,
                    result,
                    error,
                    utc_now(),
                    task_id,
                    TaskStatus.RUNNING.value,
                    TaskStatus.PENDING.value,
                ),
            )
            return cursor.rowcount == 1

    def cancel_task(self, task_id: int) -> tuple[bool, str]:
        task = self.get_task(task_id)
        if task is None:
            return False, "unknown_task"
        status = task["status"]
        if status == TaskStatus.PENDING.value:
            with self._conn() as connection:
                cursor = connection.execute(
                    "UPDATE tasks SET status = ?, cancel_requested = 1, finished_at = ? WHERE id = ? AND status = ?",
                    (TaskStatus.CANCELLED.value, utc_now(), task_id, TaskStatus.PENDING.value),
                )
            return cursor.rowcount == 1, "cancelled_before_start"
        if status == TaskStatus.RUNNING.value:
            with self._conn() as connection:
                connection.execute(
                    "UPDATE tasks SET cancel_requested = 1 WHERE id = ? AND status = ?",
                    (task_id, TaskStatus.RUNNING.value),
                )
            return True, "interrupt_requested"
        if status == TaskStatus.WAITING_CONFIRMATION.value:
            now = utc_now()
            with self._conn() as connection:
                cursor = connection.execute(
                    "UPDATE tasks SET status = ?, cancel_requested = 1, error = ?, finished_at = ?"
                    " WHERE id = ? AND status = ?",
                    (
                        TaskStatus.CANCELLED.value,
                        "cancelled while waiting for confirmation",
                        now,
                        task_id,
                        TaskStatus.WAITING_CONFIRMATION.value,
                    ),
                )
                connection.execute(
                    "UPDATE confirmations SET status = 'EXPIRED', decided_at = ?"
                    " WHERE task_id = ? AND status = 'PENDING'",
                    (now, task_id),
                )
            return cursor.rowcount == 1, "cancelled_while_waiting"
        return False, f"already_{status.lower()}"

    def is_cancel_requested(self, task_id: int) -> bool:
        row = self._conn().execute("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def add_incident(self, source: str, message: str, detail: str | None = None) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO incidents (created_at, source, message, detail) VALUES (?, ?, ?, ?)",
                (utc_now(), source, message, detail),
            )
            return int(cursor.lastrowid)

    def recent_incidents(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_incidents(self) -> int:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM incidents")
            return max(0, cursor.rowcount)

    def remember(self, text: str, source: str = "agent") -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO memory (created_at, text, source) VALUES (?, ?, ?)",
                (utc_now(), text, source),
            )
            return int(cursor.lastrowid)

    def search_memory(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        rows = self._conn().execute(
            "SELECT * FROM memory WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]
        terms = [term for term in query.split() if len(term) > 2]
        if not terms:
            return []
        clauses = " OR ".join("text LIKE ?" for _ in terms)
        rows = self._conn().execute(
            f"SELECT * FROM memory WHERE {clauses} ORDER BY id DESC LIMIT ?",
            [f"%{term}%" for term in terms] + [limit],
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_facts(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_fact(self, fact_id: int) -> bool:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM memory WHERE id = ?", (fact_id,))
            return cursor.rowcount > 0

    def create_draft(self, platform: str, content: str, scheduled_for: str | None = None) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO drafts (created_at, platform, content, scheduled_for) VALUES (?, ?, ?, ?)",
                (utc_now(), platform, content, scheduled_for),
            )
            return int(cursor.lastrowid)

    def list_drafts(self, limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM drafts"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn().execute(query, params).fetchall()]

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def find_draft(self, platform: str, content: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM drafts WHERE platform = ? AND content = ? ORDER BY id DESC LIMIT 1",
            (platform, content),
        ).fetchone()
        return dict(row) if row else None

    def update_draft(self, draft_id: int, **fields: Any) -> bool:
        allowed = {"status", "scheduled_for", "published_at", "error"}
        return self._update_row("drafts", draft_id, fields, allowed)

    def hold_task(self, task_id: int) -> bool:
        """RUNNING -> WAITING_CONFIRMATION (the agent pauses on a sensitive action)."""
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
                (TaskStatus.WAITING_CONFIRMATION.value, task_id, TaskStatus.RUNNING.value),
            )
            return cursor.rowcount == 1

    def release_task(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> bool:
        """WAITING_CONFIRMATION -> PENDING (resume) or a terminal state."""
        with self._conn() as connection:
            if status == TaskStatus.PENDING:
                cursor = connection.execute(
                    "UPDATE tasks SET status = ?, started_at = NULL WHERE id = ? AND status = ?",
                    (TaskStatus.PENDING.value, task_id, TaskStatus.WAITING_CONFIRMATION.value),
                )
            else:
                cursor = connection.execute(
                    "UPDATE tasks SET status = ?, result = ?, error = ?, finished_at = ?"
                    " WHERE id = ? AND status = ?",
                    (
                        status.value,
                        result,
                        error,
                        utc_now(),
                        task_id,
                        TaskStatus.WAITING_CONFIRMATION.value,
                    ),
                )
            return cursor.rowcount == 1

    def create_confirmation(
        self,
        kind: str,
        payload: str,
        task_id: int | None = None,
    ) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO confirmations (created_at, kind, payload, task_id) VALUES (?, ?, ?, ?)",
                (utc_now(), kind, payload, task_id),
            )
            return int(cursor.lastrowid)

    def get_confirmation(self, confirmation_id: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_confirmations(self, limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM confirmations"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn().execute(query, params).fetchall()]

    def decide_confirmation(self, confirmation_id: int, decision: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE confirmations SET status = ?, decided_at = ? WHERE id = ? AND status = 'PENDING'",
                (decision, utc_now(), confirmation_id),
            )
            return cursor.rowcount == 1

    def expire_confirmation(self, confirmation_id: int) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE confirmations SET status = 'EXPIRED', decided_at = ?"
                " WHERE id = ? AND status = 'PENDING'",
                (utc_now(), confirmation_id),
            )
            return cursor.rowcount == 1

    def expire_confirmations_before(self, cutoff_iso: str) -> list[int]:
        """Expire every PENDING confirmation created before ``cutoff_iso`` (UTC ISO)."""
        with self._conn() as connection:
            rows = connection.execute(
                "SELECT id FROM confirmations WHERE status = 'PENDING' AND created_at < ?",
                (cutoff_iso,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE confirmations SET status = 'EXPIRED', decided_at = ?"
                    f" WHERE id IN ({placeholders})",
                    [utc_now(), *ids],
                )
        return ids

    def find_confirmation(self, kind: str, payload: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM confirmations WHERE kind = ? AND payload = ? ORDER BY id DESC LIMIT 1",
            (kind, payload),
        ).fetchone()
        return dict(row) if row else None

    # --- generic updates -------------------------------------------------

    def _update_row(
        self,
        table: str,
        row_id: int,
        fields: dict[str, Any],
        allowed: set[str],
    ) -> bool:
        clean = [(key, value) for key, value in fields.items() if key in allowed]
        if not clean:
            return False
        assignments = ", ".join(f"{key} = ?" for key, _ in clean)
        params = [value for _, value in clean] + [row_id]
        with self._conn() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                params,
            )
            return cursor.rowcount == 1

    # --- workflows -------------------------------------------------------

    def save_workflow(self, name: str, steps_json: str) -> int:
        with self._conn() as connection:
            existing = connection.execute(
                "SELECT id, steps FROM workflows WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if existing["steps"] != steps_json:
                    connection.execute(
                        "UPDATE workflows SET steps = ? WHERE id = ?",
                        (steps_json, existing["id"]),
                    )
                return int(existing["id"])
            cursor = connection.execute(
                "INSERT INTO workflows (created_at, name, steps) VALUES (?, ?, ?)",
                (utc_now(), name, steps_json),
            )
            return int(cursor.lastrowid)

    def get_workflow(self, name: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM workflows WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_workflows(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM workflows ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def delete_workflow(self, name: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM workflows WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                return False
            connection.execute("DELETE FROM scheduled_jobs WHERE workflow_name = ?", (name,))
            return True

    def create_run(
        self,
        workflow_id: int,
        workflow_name: str,
        *,
        context: str = "",
        idempotency_key: str | None = None,
    ) -> int:
        now = utc_now()
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO workflow_runs"
                " (created_at, updated_at, workflow_id, workflow_name, context, idempotency_key)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, now, workflow_id, workflow_name, context, idempotency_key),
            )
            return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workflow_runs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn().execute(query, params).fetchall()]

    def active_run_with_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM workflow_runs WHERE idempotency_key = ?"
            " AND status NOT IN ('SUCCESS','FAILED','CANCELLED') ORDER BY id DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None

    def update_run(self, run_id: int, **fields: Any) -> bool:
        allowed = {"status", "current_step", "result", "error", "context"}
        if not any(key in allowed for key in fields):
            return False
        payload = dict(fields)
        payload["updated_at"] = utc_now()
        return self._update_row("workflow_runs", run_id, payload, allowed | {"updated_at"})

    def count_steps(self, run_id: int) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM workflow_steps WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["n"])

    def create_step(
        self, run_id: int, position: int, name: str, kind: str = "agent"
    ) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO workflow_steps (created_at, run_id, position, name, kind)"
                " VALUES (?, ?, ?, ?, ?)",
                (utc_now(), run_id, position, name, kind),
            )
            return int(cursor.lastrowid)

    def get_step(self, run_id: int, position: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM workflow_steps WHERE run_id = ? AND position = ?",
            (run_id, position),
        ).fetchone()
        return dict(row) if row else None

    def step_by_task(self, task_id: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM workflow_steps WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_steps(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY position", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def update_step(self, step_id: int, **fields: Any) -> bool:
        allowed = {"status", "task_id", "retry_count", "result", "last_error"}
        return self._update_row("workflow_steps", step_id, fields, allowed)

    # --- scheduled jobs --------------------------------------------------

    def save_scheduled_job(
        self,
        name: str,
        workflow_name: str,
        cron_json: str,
        *,
        enabled: bool = True,
    ) -> int:
        with self._conn() as connection:
            existing = connection.execute(
                "SELECT id FROM scheduled_jobs WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE scheduled_jobs SET workflow_name = ?, cron = ?, enabled = ? WHERE id = ?",
                    (workflow_name, cron_json, int(enabled), existing["id"]),
                )
                return int(existing["id"])
            cursor = connection.execute(
                "INSERT INTO scheduled_jobs (created_at, name, workflow_name, cron, enabled)"
                " VALUES (?, ?, ?, ?, ?)",
                (utc_now(), name, workflow_name, cron_json, int(enabled)),
            )
            return int(cursor.lastrowid)

    def get_scheduled_job(self, name: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM scheduled_jobs WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def list_scheduled_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM scheduled_jobs ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def mark_job_enqueued(self, name: str, enqueued_at: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_jobs SET last_enqueued_at = ? WHERE name = ?",
                (enqueued_at, name),
            )
            return cursor.rowcount == 1

    def delete_scheduled_job(self, name: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "DELETE FROM scheduled_jobs WHERE name = ?", (name,)
            )
            return cursor.rowcount >= 1

    # --- settings (non-sensitive) / audit --------------------------------

    def get_setting(self, key: str) -> Any | None:
        row = self._conn().execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return None

    def set_setting(
        self,
        key: str,
        value: Any,
        *,
        category: str = "general",
        updated_by: str = "system",
    ) -> bool:
        payload = json.dumps(value, ensure_ascii=False)
        now = utc_now()
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO settings (key, value_json, category, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value_json = excluded.value_json,"
                " category = excluded.category,"
                " updated_at = excluded.updated_at,"
                " updated_by = excluded.updated_by",
                (key, payload, category, now, updated_by),
            )
        return True

    def list_settings(self, category: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM settings"
        params: list[Any] = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY key"
        rows = self._conn().execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["value"] = json.loads(item.pop("value_json"))
            except ValueError:
                item["value"] = None
            result.append(item)
        return result

    def delete_setting(self, key: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount == 1

    def add_audit(self, actor: str, action: str, detail: str = "") -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO audit_log (created_at, actor, action, detail)"
                " VALUES (?, ?, ?, ?)",
                (utc_now(), str(actor), str(action), str(detail)[:500]),
            )
            return int(cursor.lastrowid)

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # --- admin sessions ---------------------------------------------------

    def create_session(self, token_hash: str, csrf_token: str, expires_at: str) -> int:
        now = utc_now()
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO admin_sessions"
                " (token_hash, csrf_token, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token_hash, csrf_token, now, expires_at),
            )
            return int(cursor.lastrowid)

    def find_session(self, token_hash: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        session = dict(row)
        if str(session["expires_at"]) <= utc_now():
            self.delete_session(token_hash)
            return None
        return session

    def delete_session(self, token_hash: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,)
            )
            return cursor.rowcount == 1

    def list_sessions(self) -> list[dict[str, Any]]:
        """Active sessions as SAFE columns only (never token/csrf material)."""
        rows = self._conn().execute(
            "SELECT id, created_at, expires_at FROM admin_sessions"
            " WHERE expires_at > ? ORDER BY id",
            (utc_now(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_session_by_id(self, session_id: int) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount == 1

    def purge_expired_sessions(self) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (utc_now(),)
            )
            return cursor.rowcount

    # --- supervision -----------------------------------------------------

    def stale_tasks(self, cutoff_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM tasks WHERE status = 'RUNNING' AND started_at IS NOT NULL"
            " AND started_at < ? ORDER BY id LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()
        return [dict(row) for row in rows]
