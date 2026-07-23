# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Behavior tests for the Hussh dashboard watchdog's memory policy."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / "scripts" / "hussh-one-dashboard-watchdog.py"


def _load_watchdog(monkeypatch, tmp_path):
    monkeypatch.setenv("HUSSH_ONE_REPO_ROOT", str(ROOT))
    monkeypatch.setenv("HUSSH_ONE_DASHBOARD_LOG", str(tmp_path / "dashboard.log"))
    monkeypatch.setenv("HUSSH_ONE_DASHBOARD_ERR_LOG", str(tmp_path / "dashboard.err.log"))
    spec = importlib.util.spec_from_file_location("hussh_dashboard_watchdog", WATCHDOG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_cap_never_restarts_a_live_embedded_tui(monkeypatch, tmp_path):
    watchdog = _load_watchdog(monkeypatch, tmp_path)
    action, since = watchdog._memory_cap_action(
        watchdog.MEMORY_CAP_MB * 1024,
        ("node /repo/ui-tui/dist/hermes-ink.js",),
        over_cap_since=100.0,
        now=10_000.0,
    )
    assert action == "defer_live_tui"
    assert since is None


def test_memory_cap_waits_before_restarting_idle_dashboard(monkeypatch, tmp_path):
    watchdog = _load_watchdog(monkeypatch, tmp_path)
    over_limit = watchdog.MEMORY_CAP_MB * 1024

    action, since = watchdog._memory_cap_action(
        over_limit, ("python -m hermes_cli.main dashboard",), over_cap_since=None, now=100.0
    )
    assert (action, since) == ("grace", 100.0)

    action, since = watchdog._memory_cap_action(
        over_limit,
        ("python -m hermes_cli.main dashboard",),
        over_cap_since=100.0,
        now=100.0 + watchdog.MEMORY_CAP_GRACE_SECONDS - 1,
    )
    assert (action, since) == ("grace", 100.0)

    action, since = watchdog._memory_cap_action(
        over_limit,
        ("python -m hermes_cli.main dashboard",),
        over_cap_since=100.0,
        now=100.0 + watchdog.MEMORY_CAP_GRACE_SECONDS,
    )
    assert (action, since) == ("restart_idle", 100.0)


def test_memory_cap_clears_state_when_usage_recovers(monkeypatch, tmp_path):
    watchdog = _load_watchdog(monkeypatch, tmp_path)
    action, since = watchdog._memory_cap_action(
        watchdog.MEMORY_CAP_MB * 1024 - 1,
        (),
        over_cap_since=100.0,
        now=200.0,
    )
    assert (action, since) == ("below_limit", None)
