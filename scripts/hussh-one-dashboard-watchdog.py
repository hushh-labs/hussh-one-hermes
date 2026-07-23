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
MEMORY_CAP_GRACE_SECONDS = float(
    os.environ.get("HUSSH_ONE_DASHBOARD_MEM_CAP_GRACE_SECONDS", "900")
)

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


def _tree_snapshot(root_pid: int) -> tuple[int, tuple[str, ...]]:
    """Return total RSS plus command lines for a process tree.

    The dashboard owns MCP children and active embedded TUI processes.  The
    command list lets the watchdog distinguish an idle dashboard leak from an
    in-progress user chat without adding a psutil dependency.
    """
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,rss=,command="], text=True, timeout=4
        )
    except Exception:  # noqa: BLE001
        return 0, ()
    children: dict[int, list[int]] = {}
    processes: dict[int, tuple[int, str]] = {}
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid, parent, memory = (int(value) for value in parts[:3])
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
        processes[pid] = (memory, parts[3])
    total = 0
    commands: list[str] = []
    pending = [root_pid]
    visited: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        memory, command = processes.get(pid, (0, ""))
        total += memory
        if command:
            commands.append(command)
        pending.extend(children.get(pid, ()))
    return total, tuple(commands)


def _tree_has_live_tui(commands: tuple[str, ...]) -> bool:
    """Whether the dashboard currently owns an embedded TUI session."""
    markers = ("tui_gateway", "hermes-ink", "/ui-tui/", "hermes --tui")
    return any(marker in command.lower() for command in commands for marker in markers)


def _memory_cap_action(
    rss_kb: int,
    commands: tuple[str, ...],
    *,
    over_cap_since: float | None,
    now: float,
) -> tuple[str, float | None]:
    """Return the safe action for the dashboard's memory soft limit.

    A dashboard owns the active PTY and its agent process.  Restarting that
    tree to reclaim memory drops the browser WebSocket (normally as code 1006)
    and abandons a live turn.  Only restart a persistently over-limit *idle*
    tree; an active TUI resets the grace timer and remains available.
    """
    if MEMORY_CAP_MB <= 0:
        return "disabled", None
    if rss_kb < MEMORY_CAP_MB * 1024:
        return "below_limit", None
    if _tree_has_live_tui(commands):
        return "defer_live_tui", None
    if over_cap_since is None:
        return "grace", now
    if now - over_cap_since < max(MEMORY_CAP_GRACE_SECONDS, 0.0):
        return "grace", over_cap_since
    return "restart_idle", over_cap_since


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
    _log(
        "[dashboard-watchdog] started "
        f"(mem_cap={MEMORY_CAP_MB} MB, grace={MEMORY_CAP_GRACE_SECONDS:.0f}s)"
    )
    occupied_logged = False
    over_cap_since: float | None = None
    last_memory_action: str | None = None

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

            rss_kb, commands = _tree_snapshot(_child.pid)
            action, over_cap_since = _memory_cap_action(
                rss_kb,
                commands,
                over_cap_since=over_cap_since,
                now=time.monotonic(),
            )
            if action != last_memory_action:
                if action == "defer_live_tui":
                    _log(
                        "[dashboard-watchdog] mem cap exceeded with active TUI; "
                        "preserving live chat"
                    )
                elif action == "grace":
                    _log(
                        "[dashboard-watchdog] mem cap exceeded while idle; "
                        f"waiting {MEMORY_CAP_GRACE_SECONDS:.0f}s before restart"
                    )
                elif action == "restart_idle":
                    _log(
                        f"[dashboard-watchdog] sustained idle mem cap; "
                        f"terminating child {_child.pid} for clean restart"
                    )
                last_memory_action = action
            if action == "restart_idle":
                _terminate_child()
                over_cap_since = None
                last_memory_action = None
            time.sleep(INTERVAL)
        except Exception:  # noqa: BLE001 - watchdog must survive inspection failures
            _log("[dashboard-watchdog] loop error\n" + traceback.format_exc())
            time.sleep(INTERVAL)

    _terminate_child()
    _log("[dashboard-watchdog] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
