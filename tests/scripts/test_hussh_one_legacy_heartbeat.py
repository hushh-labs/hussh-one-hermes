# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Execute the supervisor's migration without touching host services."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("action", ["install", "start", "restart"])
def test_launchd_migration_retires_cross_service_heartbeat(tmp_path, action):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_BIN="/unused/hermes")
    result = subprocess.run(
        ["bash", "scripts/hussh-one-supervisor.sh", action, "--manager", "launchd",
         "--dry-run", "--clean-conflicts"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    actions = result.stdout.splitlines()
    disabled = [line for line in actions if "launchctl disable " in line]
    assert len(disabled) == 1
    assert disabled[0].endswith("/ai.hussh-one.heartbeat")
    retired = next(i for i, line in enumerate(actions)
                   if "launchctl bootout " in line and line.endswith("/ai.hussh-one.heartbeat"))
    starts = [i for i, line in enumerate(actions) if "launchctl kickstart " in line]
    assert all(retired < start for start in starts)


@pytest.mark.parametrize("manager,action", [("launchd", "status"), ("systemd", "restart"), ("screen", "restart")])
def test_other_operations_do_not_disable_launchd_jobs(tmp_path, manager, action):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_BIN="/unused/hermes")
    result = subprocess.run(
        ["bash", "scripts/hussh-one-supervisor.sh", action, "--manager", manager,
         "--dry-run", "--clean-conflicts"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "launchctl disable " not in result.stdout
