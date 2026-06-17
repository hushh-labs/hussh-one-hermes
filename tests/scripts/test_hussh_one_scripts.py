from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(env or {})
    return subprocess.run(
        list(args),
        cwd=ROOT,
        env=full_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_hussh_one_shell_scripts_are_syntax_valid():
    result = run_script(
        "bash",
        "-n",
        "scripts/hussh-one-bootstrap.sh",
        "scripts/hussh-one-supervisor.sh",
        "scripts/hussh-one-doctor.sh",
        "scripts/hussh-one-restart.sh",
    )

    assert result.returncode == 0, result.stderr


def test_restart_wrapper_delegates_to_supervisor_restart():
    text = (ROOT / "scripts/hussh-one-restart.sh").read_text(encoding="utf-8")

    assert "hussh-one-supervisor.sh" in text
    assert " restart " in f" {text} "
    assert "screen -dmS" not in text
    assert "lsof -t" not in text


def test_supervisor_supports_expected_managers_and_conflict_guard():
    text = (ROOT / "scripts/hussh-one-supervisor.sh").read_text(encoding="utf-8")

    for manager in ("launchd", "systemd", "s6", "screen"):
        assert manager in text
    assert "--clean-conflicts" in text
    assert "require_no_conflicts" in text
    assert "hermes dashboard --host" not in text  # command is argv-built, not shell-hardcoded
    assert "--tui --no-open" in text


def test_supervisor_service_definitions_restart_and_raise_fd_limit():
    text = (ROOT / "scripts/hussh-one-supervisor.sh").read_text(encoding="utf-8")

    assert "SERVICE_NOFILE_LIMIT" in text
    assert "<key>SoftResourceLimits</key>" in text
    assert "<key>HardResourceLimits</key>" in text
    assert "LimitNOFILE=$SERVICE_NOFILE_LIMIT" in text
    assert "DASHBOARD_WATCHDOG_PID" in text
    assert "start_dashboard_watchdog" in text
    assert "socket.socket(socket.AF_INET, socket.SOCK_STREAM)" in text
    assert "hermes_cli.main\", \"dashboard\"" in text
    assert "while true; do $(dashboard_command_line)" in text
    assert "while true; do $(shell_quote \"$HERMES_BIN\") gateway run --replace" in text


def test_supervisor_dry_run_covers_launchd_systemd_and_screen(tmp_path):
    env = {
        "HUSSH_ONE_DRY_RUN": "1",
        "HERMES_HOME": str(tmp_path / "home"),
        "HERMES_BIN": str(tmp_path / "bin" / "hermes"),
    }

    for manager in ("launchd", "systemd", "screen"):
        result = run_script(
            "bash",
            "scripts/hussh-one-supervisor.sh",
            "restart",
            "--manager",
            manager,
            "--clean-conflicts",
            "--dry-run",
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert f"Hussh One supervisor manager: {manager}" in result.stdout
        assert "dry-run:" in result.stdout or manager == "screen"


def test_bootstrap_documents_safe_gcp_and_whatsapp_setup():
    text = (ROOT / "scripts/hussh-one-bootstrap.sh").read_text(encoding="utf-8")

    assert "gcloud auth application-default print-access-token >/dev/null" in text
    assert "safe_suffix" in text
    assert "WhatsApp pairing is per-machine" in text
    assert "model.provider gemini" in text
    assert "model.default gemini-3.5-flash" in text
    assert "WHATSAPP_REPLY_PREFIX" not in text


def test_bootstrap_dry_run_with_temp_home_is_non_mutating(tmp_path):
    env = {
        "HUSSH_ONE_DRY_RUN": "1",
        "HERMES_HOME": str(tmp_path / "home"),
    }

    result = run_script(
        "bash",
        "scripts/hussh-one-bootstrap.sh",
        "--skip-install",
        "--skip-build",
        "--manager",
        "screen",
        "--dry-run",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "dry-run:" in result.stdout
    assert "model.provider" in result.stdout
    assert "hussh-one-doctor.sh" in result.stdout


def test_doctor_checks_clone_health_surfaces():
    text = (ROOT / "scripts/hussh-one-doctor.sh").read_text(encoding="utf-8")

    assert "hussh-one-hermes" in text
    assert "hushh-labs/hussh-one-hermes" in text
    assert "__HERMES_DASHBOARD_EMBEDDED_CHAT__=true" in text
    assert "google-vertex-claude" in text
    assert "claude-opus-4-8" in text
    assert "claude-sonnet-4-6" in text
