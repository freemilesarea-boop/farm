"""SQLite persistence (sessions, run history, logs). Thread-safe via a single lock."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .security import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    scenario     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_status  TEXT NOT NULL DEFAULT 'idle'
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    end_reason   TEXT,
    iterations   INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    avg_load_ms  REAL
);
CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    level        TEXT NOT NULL,
    session_id   TEXT,
    message      TEXT NOT NULL,
    data         TEXT
);
CREATE TABLE IF NOT EXISTS tracked_processes (
    pid          INTEGER NOT NULL,
    create_time  REAL NOT NULL,
    session_id   TEXT NOT NULL,
    server_pid   INTEGER NOT NULL,
    PRIMARY KEY (pid, create_time)
);
CREATE INDEX IF NOT EXISTS idx_logs_session ON logs(session_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, id);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            if "config" not in cols:  # migration: per-session config (custom URL scenario)
                self._conn.execute("ALTER TABLE sessions ADD COLUMN config TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- sessions
    def insert_session(self, sid: str, name: str, scenario: str, created_at: float,
                       config: dict | None = None) -> None:
        self._exec(
            "INSERT INTO sessions(id, name, scenario, created_at, config) VALUES (?,?,?,?,?)",
            (sid, name, scenario, created_at, json.dumps(config) if config else None),
        )

    def delete_session(self, sid: str) -> None:
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    def set_session_status(self, sid: str, status: str) -> None:
        self._exec("UPDATE sessions SET last_status=? WHERE id=?", (status, sid))

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM sessions ORDER BY created_at, id")
        for r in rows:
            r["config"] = json.loads(r["config"]) if r.get("config") else None
        return rows

    def mark_unclean_sessions(self) -> int:
        """After a crash, sessions recorded as active are reset; returns how many."""
        cur = self._exec(
            "UPDATE sessions SET last_status='idle' WHERE last_status IN ('starting','running','stopping')"
        )
        self._exec(
            "UPDATE runs SET ended_at=?, end_reason='recovered_after_crash' WHERE ended_at IS NULL",
            (time.time(),),
        )
        return cur.rowcount

    # ---- tracked browser processes (for cleanup / crash recovery)
    def track_processes(self, session_id: str, server_pid: int, pids) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO tracked_processes(pid, create_time, session_id, server_pid) VALUES (?,?,?,?)",
                [(pid, ct, session_id, server_pid) for pid, ct in pids],
            )

    def untrack_session(self, session_id: str, server_pid: int) -> None:
        self._exec("DELETE FROM tracked_processes WHERE session_id=? AND server_pid=?", (session_id, server_pid))

    def tracked_by_server(self) -> dict[int, list[tuple[int, float]]]:
        out: dict[int, list[tuple[int, float]]] = {}
        for r in self._all("SELECT pid, create_time, server_pid FROM tracked_processes"):
            out.setdefault(r["server_pid"], []).append((r["pid"], r["create_time"]))
        return out

    def untrack_server(self, server_pid: int) -> None:
        self._exec("DELETE FROM tracked_processes WHERE server_pid=?", (server_pid,))

    # ---- runs
    def start_run(self, sid: str, started_at: float) -> int:
        cur = self._exec("INSERT INTO runs(session_id, started_at) VALUES (?,?)", (sid, started_at))
        return int(cur.lastrowid)

    def end_run(self, run_id: int, reason: str, iterations: int, errors: int, avg_ms: float | None) -> None:
        self._exec(
            "UPDATE runs SET ended_at=?, end_reason=?, iterations=?, errors=?, avg_load_ms=? WHERE id=?",
            (time.time(), reason, iterations, errors, avg_ms, run_id),
        )

    def list_runs(self, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            return self._all(
                "SELECT * FROM runs WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)
            )
        return self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    # ---- logs (always redacted before storage)
    def log(self, level: str, message: str, session_id: str | None = None, data: dict | None = None) -> dict:
        ts = time.time()
        msg = redact(message)
        data_s = redact(json.dumps(data, default=str)) if data else None
        cur = self._exec(
            "INSERT INTO logs(ts, level, session_id, message, data) VALUES (?,?,?,?,?)",
            (ts, level, session_id, msg, data_s),
        )
        return {"id": cur.lastrowid, "ts": ts, "level": level, "session_id": session_id,
                "message": msg, "data": data_s}

    def recent_logs(self, limit: int = 100, session_id: str | None = None, level: str | None = None,
                    after_id: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM logs WHERE id > ?"
        params: list[Any] = [after_id]
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if level:
            sql += " AND level=?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))
