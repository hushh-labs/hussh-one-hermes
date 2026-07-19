#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Launchd-owned Hussh One dashboard watchdog.

The watchdog is the only process launchd starts.  It owns the dashboard child,
applies the resource limits previously held by the shell launcher, and keeps a
single child healthy without racing another supervisor for port 9119.
"""
from __future__ import annotations

import os
from pathlib import Path
import resource
import signal
import socket
import subprocess
import sys
import time
import traceback


HOST = os.environ.get("HUSSH_ONE_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("HUSSH_ONE_DASHBOARD_PORT", "9119"))
INTERVAL = float(os.environ.get("HUSSH_ONE_DASHBOARD_WATCHDOG_INTERVAL", "5"))
REPO_ROOT = Path(os.environ["HUSSH_ONE_REPO_ROOT"])
OUT_PATH = Path(os.environ["HUSSH_ONE_DASHBOARD_LOG"])
ERR_PATH = Path(os.environ["HUSSH_ONE_DASHBOARD_ERR_LOG"])
NOFILE_LIMIT = int(os.environ.get("HUSSH_ONE_NOFILE_LIMIT", "65536"))
MEMORY_CAP_MB = int(os.environ.get("HUSSH_ONE_DASHBOARD_MEM_CAP_MB", "6144"))

_running = True
_child: subprocess.Popen[bytes] | None = None


def _log(message: str) -> None:
    try:
        ERR_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ERR_PATH.open("ab", buffering=0) as stream:
            stream.write((message.rstrip() + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def _raise_nofile_limit() -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = NOFILE_LIMIT if hard == resource.RLIM_INFINITY else min(NOFILE_LIMIT, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:  # noqa: BLE001 - a limit failure must not prevent recovery
        pass


def _listening() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _tree_rss_kb(root_pid: int) -> int:
    """Sum a process tree's RSS without adding a psutil dependency."""
    try:
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True, timeout=4)
    except Exception:  # noqa: BLE001
        return 0
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, parent, memory = (int(value) for value in parts)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
        rss[pid] = memory
    total = 0
    pending = [root_pid]
    visited: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        total += rss.get(pid, 0)
        pending.extend(children.get(pid, ()))
    return total


def _terminate_child() -> None:
    global _child
    child = _child
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _log(f"[dashboard-watchdog] child {child.pid} ignored SIGTERM; SIGKILL")
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    except ProcessLookupError:
        pass
    except Exception:  # noqa: BLE001
        _log("[dashboard-watchdog] could not stop child\n" + traceback.format_exc())
    finally:
        _child = None


def _handle_stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False


def _start_child() -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "dashboard",
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--tui",
        "--no-open",
    ]
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("ab", buffering=0) as stdout, ERR_PATH.open("ab", buffering=0) as stderr:
        return subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=os.environ.copy(),
            start_new_session=True,
        )


def main() -> int:
    global _child
    _raise_nofile_limit()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    _log(f"[dashboard-watchdog] started (mem_cap={MEMORY_CAP_MB} MB)")
    occupied_logged = False

    while _running:
        try:
            if _child is None:
                if _listening():
                    if not occupied_logged:
                        _log("[dashboard-watchdog] dashboard port already occupied; waiting for owner")
                        occupied_logged = True
                    time.sleep(INTERVAL)
                    continue
                occupied_logged = False
                _log("[dashboard-watchdog] dashboard port down; starting child")
                _child = _start_child()
                continue

            return_code = _child.poll()
            if return_code is not None:
                _log(f"[dashboard-watchdog] child exited rc={return_code}")
                _child = None
                time.sleep(INTERVAL)
                continue

            if MEMORY_CAP_MB > 0 and _tree_rss_kb(_child.pid) // 1024 >= MEMORY_CAP_MB:
                _log(
                    f"[dashboard-watchdog] mem cap hit; terminating child {_child.pid} for clean restart"
                )
                _terminate_child()
            time.sleep(INTERVAL)
        except Exception:  # noqa: BLE001 - watchdog must survive inspection failures
            _log("[dashboard-watchdog] loop error\n" + traceback.format_exc())
            time.sleep(INTERVAL)

    _terminate_child()
    _log("[dashboard-watchdog] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
