# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Durable, redacted attempt and checkpoint records for local inference.

The conversation transcript remains authoritative.  This ledger records only
the recovery metadata needed to decide whether a failed local request may be
retried, so a gateway restart cannot turn a completed tool call into a second
side effect.  It intentionally stores no prompt, response, credentials, or
tool arguments.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from hermes_constants import get_hermes_home

_LOCK = threading.RLock()
_MAX_ERROR = 240


def _error_class(error: BaseException | str | None) -> str:
    if error is None:
        return ""
    return type(error).__name__ if isinstance(error, BaseException) else "RuntimeError"


def _fingerprint(error: BaseException | str | None) -> str:
    if error is None:
        return ""
    text = str(error).splitlines()[0][:_MAX_ERROR]
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _db_path():
    return get_hermes_home().resolve() / "local-runtime.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS local_attempts (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             session_id TEXT NOT NULL,
             request_id TEXT NOT NULL,
             model TEXT NOT NULL,
             base_url TEXT NOT NULL,
             context_tokens INTEGER NOT NULL DEFAULT 0,
             message_cursor INTEGER NOT NULL DEFAULT 0,
             model_generation TEXT NOT NULL DEFAULT '',
             deadline REAL,
             attempt INTEGER NOT NULL,
             status TEXT NOT NULL CHECK(status IN ('started','checkpointed','completed','failed')),
             failure_class TEXT NOT NULL DEFAULT '',
             failure_fingerprint TEXT NOT NULL DEFAULT '',
             committed_tool_ids TEXT NOT NULL DEFAULT '[]',
             started_at REAL NOT NULL,
             finished_at REAL,
             UNIQUE(session_id, request_id, attempt)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_attempts_session "
        "ON local_attempts(session_id, started_at DESC)"
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(local_attempts)")}
    for name, definition in (
        ("message_cursor", "INTEGER NOT NULL DEFAULT 0"),
        ("model_generation", "TEXT NOT NULL DEFAULT ''"),
        ("deadline", "REAL"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE local_attempts ADD COLUMN {name} {definition}")
    return conn


@dataclass(frozen=True)
class AttemptRecord:
    id: int
    session_id: str
    request_id: str
    attempt: int
    message_cursor: int = 0
    model_generation: str = ""
    deadline: float | None = None


class LocalAttemptLedger:
    """Small SQLite ledger used by the local-call boundary."""

    def __init__(self, *, session_id: str) -> None:
        self.session_id = str(session_id or "")

    @classmethod
    def for_agent(cls, agent: Any) -> Optional["LocalAttemptLedger"]:
        session_id = str(getattr(agent, "session_id", "") or "")
        if not session_id:
            return None
        return cls(session_id=session_id)

    def begin(
        self,
        *,
        request_id: str,
        model: str,
        base_url: str,
        context_tokens: int = 0,
        message_cursor: int = 0,
        model_generation: str = "",
        deadline: float | None = None,
    ) -> int:
        request_id = str(request_id or "turn")
        now = time.time()
        with _LOCK:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt "
                    "FROM local_attempts WHERE session_id=? AND request_id=?",
                    (self.session_id, request_id),
                ).fetchone()
                attempt = int(row["next_attempt"])
                cursor = conn.execute(
                    """INSERT INTO local_attempts
                       (session_id, request_id, model, base_url, context_tokens,
                        message_cursor, model_generation, deadline, attempt,
                        status, started_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)""",
                    (
                        self.session_id,
                        request_id,
                        str(model or "")[:128],
                        str(base_url or "")[:256],
                        max(0, int(context_tokens or 0)),
                        max(0, int(message_cursor or 0)),
                        str(model_generation or "")[:128],
                        float(deadline) if deadline is not None else None,
                        attempt,
                        now,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)
            finally:
                conn.close()

    def checkpoint(
        self,
        attempt_id: int,
        *,
        committed_tool_ids: list[str] | tuple[str, ...] = (),
        message_cursor: int | None = None,
        model_generation: str | None = None,
        deadline: float | None = None,
    ) -> None:
        ids = [str(value)[:128] for value in committed_tool_ids if str(value)]
        with _LOCK:
            conn = _connect()
            try:
                updates = ["status='checkpointed'", "committed_tool_ids=?"]
                values: list[Any] = [json.dumps(ids, separators=(",", ":"))]
                if message_cursor is not None:
                    updates.append("message_cursor=?")
                    values.append(max(0, int(message_cursor)))
                if model_generation is not None:
                    updates.append("model_generation=?")
                    values.append(str(model_generation)[:128])
                if deadline is not None:
                    updates.append("deadline=?")
                    values.append(float(deadline))
                values.append(int(attempt_id))
                conn.execute(
                    f"UPDATE local_attempts SET {', '.join(updates)} "
                    "WHERE id=? AND status='started'",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    def finish(
        self,
        attempt_id: int,
        *,
        success: bool,
        error: BaseException | str | None = None,
        committed_tool_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        ids = [str(value)[:128] for value in committed_tool_ids if str(value)]
        with _LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """UPDATE local_attempts
                       SET status=?, failure_class=?, failure_fingerprint=?,
                           committed_tool_ids=?, finished_at=?
                       WHERE id=? AND status IN ('started', 'checkpointed')""",
                    (
                        "completed" if success else "failed",
                        "" if success else _error_class(error),
                        "" if success else _fingerprint(error),
                        json.dumps(ids, separators=(",", ":")),
                        time.time(),
                        int(attempt_id),
                    ),
                )
                conn.commit()
            finally:
                conn.close()


def recovery_allowed(
    *,
    attempts: int,
    identical_failures: int,
    max_attempts: int = 3,
) -> bool:
    """Return whether a resumable turn has finite retry budget remaining."""
    limit = max(1, int(max_attempts))
    return int(attempts) < limit and int(identical_failures) < limit


__all__ = ["AttemptRecord", "LocalAttemptLedger", "recovery_allowed"]
