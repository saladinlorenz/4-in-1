from __future__ import annotations

import contextlib
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
    decided_at TEXT
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

    def create_confirmation(self, kind: str, payload: str) -> int:
        with self._conn() as connection:
            cursor = connection.execute(
                "INSERT INTO confirmations (created_at, kind, payload) VALUES (?, ?, ?)",
                (utc_now(), kind, payload),
            )
            return int(cursor.lastrowid)

    def decide_confirmation(self, confirmation_id: int, decision: str) -> bool:
        with self._conn() as connection:
            cursor = connection.execute(
                "UPDATE confirmations SET status = ?, decided_at = ? WHERE id = ? AND status = 'PENDING'",
                (decision, utc_now(), confirmation_id),
            )
            return cursor.rowcount == 1
