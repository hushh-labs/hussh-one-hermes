# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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
        "scripts/hussh-one-copilot-setup.sh",
        "scripts/setup_open_webui.sh",
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
    assert 'WHATSAPP_PORT="${HUSSH_ONE_WHATSAPP_PORT:-8473}"' in text


def test_supervisor_service_definitions_restart_and_raise_fd_limit():
    text = (ROOT / "scripts/hussh-one-supervisor.sh").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts/hussh-one-dashboard-watchdog.py").read_text(encoding="utf-8")

    assert "SERVICE_NOFILE_LIMIT" in text
    assert "<key>SoftResourceLimits</key>" in text
    assert "<key>HardResourceLimits</key>" in text
    assert "LimitNOFILE=$SERVICE_NOFILE_LIMIT" in text
    assert "DASHBOARD_WATCHDOG_PID" in text
    assert "DASHBOARD_WATCHDOG_SCRIPT" in text
    assert '<string>$(xml_escape "$DASHBOARD_WATCHDOG_SCRIPT")</string>' in text
    assert "retire_legacy_dashboard_watchdog" in text
    assert "start_dashboard_watchdog" not in text
    assert "launchd_start_gateway" in text
    assert 'launchctl kickstart -k "$(launchd_gateway_target)"' in text
    assert 'gateway_service restart\n        launchd_start_dashboard' not in text
    assert 'launchctl bootstrap "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true' not in text
    assert 'for attempt in 1 2 3; do' in text
    assert 'failed to bootstrap dashboard launchd service after 3 attempts' in text
    assert "socket.create_connection" in watchdog
    assert "hermes_cli.main" in watchdog
    assert "start_new_session=True" in watchdog
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
    assert "model.default gemini-3.6-flash" in text
    assert "plugins enable web-ddgs" in text
    assert "tools post-setup ddgs" in text
    assert "web.search_backend ddgs" in text
    assert "WHATSAPP_REPLY_PREFIX" not in text


def test_bootstrap_auto_provisions_companions_only_when_prerequisites_exist():
    text = (ROOT / "scripts/hussh-one-bootstrap.sh").read_text(encoding="utf-8")

    assert 'SETUP_COPILOT="${HUSSH_ONE_SETUP_COPILOT:-auto}"' in text
    assert 'SETUP_OPEN_WEBUI="${HUSSH_ONE_SETUP_OPEN_WEBUI:-auto}"' in text
    assert "No supported VS Code user profile found" in text
    assert "Vertex ADC and an active GCP project are required" in text
    assert "--allow-unauthenticated-loopback" in text
    assert "setup_open_webui" in text
    assert "install_managed_doctor" in text
    assert "hussh_one_doctor_install.py" in text
    assert 'OPEN_WEBUI_ENABLE_SERVICE=auto' in text


def test_bootstrap_skips_copilot_without_adc_but_keeps_setup_nonfatal(tmp_path):
    home = tmp_path / "home"
    (home / "Library/Application Support/Code/User").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gcloud = fake_bin / "gcloud"
    gcloud.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    gcloud.chmod(0o755)

    result = run_script(
        "bash",
        "scripts/hussh-one-bootstrap.sh",
        "--skip-install",
        "--skip-build",
        "--manager",
        "screen",
        "--dry-run",
        env={
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Copilot BYOK skipped: Vertex ADC and an active GCP project are required" in result.stderr
    assert "Setting up Open WebUI companion service" in result.stdout


def test_open_webui_setup_stays_on_the_selected_hermes_runtime_and_brand():
    text = (ROOT / "scripts/setup_open_webui.sh").read_text(encoding="utf-8")

    assert 'HERMES_BIN="${HERMES_BIN:-$REPO_ROOT/.venv/bin/hermes}"' in text
    assert "command -v hermes" not in text
    assert "Repository Hermes binary not found" in text
    assert 'export HERMES_HOME=${quoted_home}' in text
    assert 'Environment=HERMES_HOME=$HERMES_HOME' in text
    assert "KeepAlive" in text
    assert 'RUNTIME_CONFIG_PATH="$HERMES_HOME/open-webui.env"' in text
    assert "write_runtime_config" in text
    assert 'OPEN_WEBUI_NAME="${OPEN_WEBUI_NAME:-🤫 Hussh One}"' in text
    for stale in ("Google Ads", "Google Agent Development Kit", "Hussh One ADK", "applyAdkLogo"):
        assert stale not in text


def test_copilot_setup_writes_a_loopback_vertex_endpoint_to_a_temp_editor_profile(tmp_path):
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes"
    editor = home / "Library/Application Support/Code/User"
    editor.mkdir(parents=True)
    litellm = hermes_home / "litellm-venv/bin/litellm"
    litellm.parent.mkdir(parents=True)
    litellm.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    litellm.chmod(0o755)

    result = run_script(
        "bash",
        "scripts/hussh-one-copilot-setup.sh",
        "--project",
        "test-project",
        env={"HOME": str(home), "HERMES_HOME": str(hermes_home)},
    )

    assert result.returncode == 0, result.stderr
    endpoint_config = json.loads((editor / "chatLanguageModels.json").read_text())
    endpoint = next(item for item in endpoint_config if item["name"] == "Hussh One Vertex ADC")
    assert endpoint["apiKey"]
    assert "${input:" not in endpoint["apiKey"]
    assert {model["url"] for model in endpoint["models"]} == {"http://127.0.0.1:8644/v1"}

    proxy_launcher = (hermes_home / "scripts/start_litellm_proxy.sh").read_text()
    shim_launcher = (hermes_home / "scripts/start_litellm_shim.sh").read_text()
    assert f"export HERMES_HOME={hermes_home}" in proxy_launcher
    assert f"export HERMES_HOME={hermes_home}" in shim_launcher
    assert "$HOME/.hermes" not in proxy_launcher
    assert "$HOME/.hermes" not in shim_launcher


def test_supervisor_probes_and_restarts_open_webui_when_managed(tmp_path):
    home = tmp_path / "home"
    unit = home / ".config/systemd/user/openwebui-hermes.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    launcher = home / ".local/bin/start-open-webui-hermes.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("export HOST=127.0.0.1\nexport PORT=65531\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = run_script(
        "bash",
        "scripts/hussh-one-supervisor.sh",
        "restart",
        "--manager",
        "systemd",
        "--dry-run",
        env={
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "HERMES_BIN": str(tmp_path / "bin" / "hermes"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Open WebUI: unhealthy at http://127.0.0.1:65531" in result.stdout
    assert "systemctl --user restart openwebui-hermes.service" in result.stdout


def _load_changelog_checker():
    path = ROOT / "scripts/hussh-one-changelog-check.py"
    spec = importlib.util.spec_from_file_location("hussh_one_changelog_check_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_changelog_checker_exempts_only_changelog_only_commits(monkeypatch):
    checker = _load_changelog_checker()

    paths = {
        "docs-only": "docs/hussh-one/CHANGELOG.md",
        "feature-docs": "docs/hussh-one/CHANGELOG.md\ndocs/hussh-one/features/open-webui.md",
    }

    def fake_git(*args: str) -> str:
        if args[0] == "log":
            return "docs-only|2026-07-17|docs: changelog\nfeature-docs|2026-07-17|docs: feature"
        if args[:3] == ("show", "--format=", "--name-only"):
            return paths[args[3]]
        raise AssertionError(args)

    monkeypatch.setattr(checker, "_git", fake_git)

    assert checker.is_changelog_only_commit("docs-only")
    assert not checker.is_changelog_only_commit("feature-docs")
    assert checker.find_candidate_commits("base") == [
        ("feature-docs", "2026-07-17", "docs: feature"),
    ]


def test_changelog_checker_uses_recorded_fork_base_without_upstream(monkeypatch):
    checker = _load_changelog_checker()
    monkeypatch.setattr(checker, "_merge_base_with_upstream", lambda: None)
    monkeypatch.setattr(checker, "_recorded_fork_base", lambda: "recorded-base")

    seen_args: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        seen_args.append(args)
        if args[0] == "log":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(checker, "_git", fake_git)

    assert checker.find_candidate_commits(None) == []
    assert any("recorded-base..HEAD" in args for args in seen_args)


def test_changelog_checker_uses_a_glob_for_hussh_script_paths():
    checker = _load_changelog_checker()

    assert ":(glob)scripts/hussh-one-*" in checker.HUSSH_ONE_PATHS


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
    assert "OPEN_WEBUI_URL" in text
    assert "check_open_webui_health" in text
    assert "read_open_webui_setting" in text
    assert "loopback compatibility mode accepts missing or blank-bearer" in text
