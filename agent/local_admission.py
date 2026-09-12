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


def acquire_process_permit(key: str, capacity: int, wait: float, priority: str) -> ProcessPermit:
    """Claim a permit atomically; never expire a live process's active claim.

    Capacity is pinned by the first claimant for a key. Changing per-call
    settings cannot replace a pool with active requests. Dead-process cleanup
    uses both PID and birth time to distinguish PID reuse.
    """
    ticket = uuid.uuid4().hex
    permit = ProcessPermit(ticket)
    deadline = time.monotonic() + max(0.0, wait)
    conn = _queue_connection()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO inference_limits VALUES (?, ?)", (key, max(1, capacity)))
            conn.execute("INSERT INTO inference_queue VALUES (?, ?, ?, ?, ?, ?, 0)",
                         (ticket, key, os.getpid(), psutil.Process().create_time(),
                          0 if priority == "interactive" else 1, time.time()))
        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT ticket,pid,birth FROM inference_queue WHERE key=?", (key,)).fetchall()
                for row in rows:
                    if _dead(row['pid'], row['birth']):
                        conn.execute("DELETE FROM inference_queue WHERE ticket=?", (row['ticket'],))
                limit = conn.execute("SELECT capacity FROM inference_limits WHERE key=?", (key,)).fetchone()[0]
                active = conn.execute("SELECT count(*) FROM inference_queue WHERE key=? AND active=1", (key,)).fetchone()[0]
                first = conn.execute("SELECT ticket FROM inference_queue WHERE key=? AND active=0 ORDER BY priority,queued,ticket LIMIT 1", (key,)).fetchone()
                granted = active < limit and first is not None and first[0] == ticket
                if granted:
                    conn.execute("UPDATE inference_queue SET active=1 WHERE ticket=?", (ticket,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            if granted:
                permit.prevent_idle_sleep()
                return permit
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
