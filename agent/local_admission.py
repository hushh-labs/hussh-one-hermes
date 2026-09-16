# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Host-local inference queue shared by CLI, gateway and scheduler processes."""
from __future__ import annotations

import os
import logging
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import psutil

from agent.local_recovery import _connect

logger = logging.getLogger(__name__)


def _queue_connection():
    conn = _connect()
    conn.execute("CREATE TABLE IF NOT EXISTS inference_limits (key TEXT PRIMARY KEY, capacity INTEGER NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS inference_queue (
        ticket TEXT PRIMARY KEY, key TEXT NOT NULL, pid INTEGER NOT NULL,
        birth REAL NOT NULL, priority INTEGER NOT NULL, queued REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 0)""")
    conn.commit()
    return conn


def _dead(pid: int, birth: float) -> bool:
    try:
        return psutil.Process(pid).create_time() != birth
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


@dataclass
class ProcessPermit:
    ticket: str
    _awake_process: subprocess.Popen | None = field(default=None, repr=False)

    def prevent_idle_sleep(self) -> None:
        if sys.platform != "darwin" or self._awake_process is not None:
            return
        from hermes_cli.config import load_config_readonly
        if load_config_readonly().get("agent", {}).get("local_keep_awake", True) is False:
            return
        # A task-scoped assertion, not a persistent power-setting change.
        # -w also releases it if the Hermes process dies before normal cleanup.
        self._awake_process = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("local_inference_idle_sleep_prevention active=true")

    def release(self) -> None:
        conn = _queue_connection()
        try:
            with conn:
                conn.execute("DELETE FROM inference_queue WHERE ticket=?", (self.ticket,))
        finally:
            conn.close()
            if self._awake_process is not None:
                process, self._awake_process = self._awake_process, None
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                logger.info("local_inference_idle_sleep_prevention active=false")


def acquire_process_permit(
    key: str,
    capacity: Optional[int],
    wait: float,
    priority: str,
    check_cancel=None,
    on_wait=None,
) -> ProcessPermit:
    """Record ownership across processes without imposing a default cap.

    ``capacity=None`` is the normal local-runtime path.  It records an active
    ticket for diagnostics and crash recovery, but grants immediately so the
    provider (LM Studio, Ollama, or another loopback server) controls its own
    parallelism.  A positive capacity remains available only for an explicit
    caller policy.  Existing rows created by the old single-flight default are
    migrated to ``0`` (the durable unlimited sentinel) when an unbounded caller
    arrives, so a stale database row cannot keep blocking every future turn.
    """
    ticket = uuid.uuid4().hex
    permit = ProcessPermit(ticket)
    deadline = time.monotonic() + max(0.0, wait)
    requested_capacity = None
    if capacity is not None:
        try:
            requested_capacity = int(capacity)
        except (TypeError, ValueError):
            requested_capacity = None
        if requested_capacity is not None and requested_capacity <= 0:
            requested_capacity = None
    conn = _queue_connection()
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO inference_limits VALUES (?, ?)",
                (key, requested_capacity if requested_capacity is not None else 0),
            )
            # Unbounded callers do not enter the wait loop below, so they used
            # to skip the dead-process sweep entirely.  A crashed Hermes
            # process could therefore leave occupied-looking tickets in the
            # durable ledger forever.  Capacity=0 still means the provider
            # owns parallelism, but stale rows must be removed before the
            # current ticket is recorded so diagnostics and later explicit
            # policies see the real live owners.
            rows = conn.execute(
                "SELECT ticket,pid,birth FROM inference_queue WHERE key=?",
                (key,),
            ).fetchall()
            stale_tickets = [
                row["ticket"]
                for row in rows
                if _dead(row["pid"], row["birth"])
            ]
            if stale_tickets:
                conn.executemany(
                    "DELETE FROM inference_queue WHERE ticket=?",
                    [(ticket,) for ticket in stale_tickets],
                )
                logger.info(
                    "local_admission reaped_stale=%d key=%s",
                    len(stale_tickets),
                    key,
                )
            if requested_capacity is None:
                # 0 is the backwards-compatible unlimited sentinel. This is
                # also the migration for rows pinned to capacity=1 by older
                # Hermes processes.
                conn.execute(
                    "UPDATE inference_limits SET capacity=0 WHERE key=?",
                    (key,),
                )
            conn.execute("INSERT INTO inference_queue VALUES (?, ?, ?, ?, ?, ?, 0)",
                         (ticket, key, os.getpid(), psutil.Process().create_time(),
                          0 if priority == "interactive" else 1, time.time()))
            if requested_capacity is None:
                conn.execute(
                    "UPDATE inference_queue SET active=1 WHERE ticket=?",
                    (ticket,),
                )
                permit.prevent_idle_sleep()
                return permit
        waiting_reported = False
        while True:
            if check_cancel is not None:
                check_cancel()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT ticket,pid,birth FROM inference_queue WHERE key=?", (key,)).fetchall()
                for row in rows:
                    if _dead(row['pid'], row['birth']):
                        conn.execute("DELETE FROM inference_queue WHERE ticket=?", (row['ticket'],))
                limit = conn.execute("SELECT capacity FROM inference_limits WHERE key=?", (key,)).fetchone()[0]
                active = conn.execute("SELECT count(*) FROM inference_queue WHERE key=? AND active=1", (key,)).fetchone()[0]
                first = conn.execute("SELECT ticket FROM inference_queue WHERE key=? AND active=0 ORDER BY priority,queued,ticket LIMIT 1", (key,)).fetchone()
                # A non-positive persisted capacity is the durable unlimited
                # mode. It never waits for another Hermes process to release a
                # ticket, while still retaining live ownership rows for
                # diagnostics and dead-process cleanup.
                granted = (
                    limit <= 0
                    or (active < limit and first is not None and first[0] == ticket)
                )
                if granted:
                    conn.execute("UPDATE inference_queue SET active=1 WHERE ticket=?", (ticket,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            if granted:
                permit.prevent_idle_sleep()
                return permit
            if not waiting_reported and on_wait is not None:
                on_wait()
                waiting_reported = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Local inference capacity is occupied")
            time.sleep(min(0.05, remaining))
    except BaseException:
        conn.rollback()
        with conn:
            conn.execute("DELETE FROM inference_queue WHERE ticket=?", (ticket,))
        raise
    finally:
        conn.close()
