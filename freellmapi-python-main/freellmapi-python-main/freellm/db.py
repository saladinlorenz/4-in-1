from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .config import get_config

_db: sqlite3.Connection | None = None
_lock = threading.Lock()

def get_default_db_path() -> str:
    cfg = get_config()
    if cfg.db_path:
        return cfg.db_path
    # default: data/freeapi.db relative to project root (freellmapi-python/)
    root = Path(__file__).resolve().parent.parent
    return str(root / "data" / "freeapi.db")

def connect_db(db_path: str | None = None, *, check_same_thread: bool = False) -> sqlite3.Connection:
    global _db
    with _lock:
        if _db is not None:
            try:
                _db.execute("SELECT 1")
                return _db
            except sqlite3.ProgrammingError:
                _db = None
        resolved = db_path or get_default_db_path()
        is_memory = resolved == ":memory:"
        if not is_memory:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved, check_same_thread=check_same_thread, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if not is_memory:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _db = conn
        return conn

def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        return connect_db()
    return _db

def reset_db():
    global _db
    with _lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db = None

def execute(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params)

def fetchone(conn: sqlite3.Connection, sql: str, params=()):
    cur = conn.execute(sql, params)
    return cur.fetchone()

def fetchall(conn: sqlite3.Connection, sql: str, params=()):
    cur = conn.execute(sql, params)
    return cur.fetchall()
