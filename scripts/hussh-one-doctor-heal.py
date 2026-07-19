#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, stateful Hussh One doctor for the self-chat cron job.

The scheduler delivers a no-agent script's stdout verbatim.  Consequently this
script writes nothing unless a real health failure is new, recovered, or has
remained unresolved for the bounded reminder interval.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
STATE_PATH = HERMES_HOME / "health" / "hussh-one-doctor-alert-state.json"
RUNTIME_METADATA = HERMES_HOME / "hussh-one-runtime.json"
REMINDER_SECONDS = 6 * 60 * 60
PRUNE_COOLDOWN_SECONDS = 24 * 60 * 60
PRUNE_THRESHOLD = 1000
DOCTOR_NAME = "🤫 Hussh One — Doctor"
CORE_SERVICES = (
    "ai.hussh-one.dashboard",
    "ai.hermes.gateway",
    "ai.hushh.one.litellm-proxy",
    "ai.hushh.one.litellm-shim",
    "ai.openwebui.hermes",
)


def _read_runtime_metadata() -> dict[str, Any]:
    try:
        data = json.loads(RUNTIME_METADATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def repository_root() -> Path | None:
    """Locate the checked-out Hussh tree after this file is copied to HERMES_HOME."""
    metadata = _read_runtime_metadata()
    candidates = (
        os.environ.get("HUSSH_ONE_REPO_ROOT"),
        metadata.get("repo_root"),
        Path(__file__).resolve().parents[1],
        Path.home() / "Documents" / "GitHub" / "hussh-one-hermes-agent",
    )
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(str(candidate)).expanduser()
        if (root / "scripts" / "hussh-one-health-index.py").is_file():
            return root
    return None


def python_bin(repo_root: Path) -> str:
    metadata_python = _read_runtime_metadata().get("python_bin")
    for candidate in (metadata_python, repo_root / ".venv" / "bin" / "python", sys.executable):
        if candidate and Path(str(candidate)).is_file():
            return str(candidate)
    return sys.executable


def run(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def health_index(repo_root: Path, interpreter: str) -> dict[str, Any] | None:
    code, stdout, _stderr = run(
        [
            interpreter,
            str(repo_root / "scripts" / "hussh-one-health-index.py"),
            "--json",
            "--write",
            "--skip-fetch",
            "--skip-doctor",
        ],
        timeout=150,
    )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _launchd_state(label: str) -> str:
    if sys.platform != "darwin":
        return "unsupported"
    code, stdout, _stderr = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout=20)
    if code != 0:
        return "not-loaded"
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("state = "):
            return line.split("=", 1)[1].strip()
    return "unknown"


def heal_services() -> list[tuple[str, str]]:
    """Request a restart only for a non-running managed service."""
    unresolved: list[tuple[str, str]] = []
    if sys.platform != "darwin":
        return unresolved
    for label in CORE_SERVICES:
        before = _launchd_state(label)
        if before == "running":
            continue
        if before == "not-loaded":
            unresolved.append((f"services/{label}", "launchd job is not loaded"))
            continue
        run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], timeout=30)
        time.sleep(2)
        after = _launchd_state(label)
        if after != "running":
            unresolved.append((f"services/{label}", f"state={before}; kickstart left state={after}"))
    return unresolved


def _session_inventory(repo_root: Path):
    maintenance_dir = repo_root / "scripts" / "maintenance"
    if str(maintenance_dir) not in sys.path:
        sys.path.insert(0, str(maintenance_dir))
    from whatsapp_session_inventory import scan_session_directory

    session_dir = HERMES_HOME / "whatsapp" / "session"
    if not session_dir.is_dir():
        return None
    return scan_session_directory(session_dir)


def heal_session_bloat(repo_root: Path, interpreter: str, state: dict[str, Any], now: float) -> list[tuple[str, str]]:
    """Run the existing conservative janitor no more than once each day."""
    try:
        inventory = _session_inventory(repo_root)
    except OSError as exc:
        return [("whatsapp/session-dir", f"cannot inventory session files: {exc}")]
    if inventory is None or inventory.prunable_count < PRUNE_THRESHOLD:
        return []
    last_prune_at = float(state.get("last_prune_at") or 0)
    if now - last_prune_at < PRUNE_COOLDOWN_SECONDS:
        return []
    pruner = repo_root / "scripts" / "maintenance" / "prune_whatsapp_session.py"
    code, _stdout, stderr = run([interpreter, str(pruner)], timeout=150)
    if code != 0:
        return [("whatsapp/session-dir", f"conservative cleanup failed: {stderr[:160] or 'unknown error'}")]
    state["last_prune_at"] = now
    return []


def health_failures(health: dict[str, Any]) -> list[tuple[str, str]]:
    findings = health.get("findings")
    if not isinstance(findings, list):
        return [("doctor/health-index", "health index returned no findings")]
    failures: list[tuple[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or str(finding.get("status", "")).lower() != "fail":
            continue
        harness = str(finding.get("harness") or "health")
        name = str(finding.get("name") or "unknown")
        detail = str(finding.get("detail") or "health check failed")
        failures.append((f"{harness}/{name}", detail))
    return failures


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 1, "active": {}}
    if not isinstance(data, dict) or not isinstance(data.get("active"), dict):
        return {"schema": 1, "active": {}}
    data["schema"] = 1
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{STATE_PATH.name}.", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def transition_report(
    state: dict[str, Any], current: list[tuple[str, str]], now: float
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Return newly failing, recovered, and reminder findings; update state."""
    previous = state.get("active", {})
    assert isinstance(previous, dict)
    current_by_key = dict(current)
    new = [(key, detail) for key, detail in current if key not in previous]
    recovered = [
        (key, str(value.get("detail") or "recovered"))
        for key, value in previous.items()
        if key not in current_by_key and isinstance(value, dict)
    ]
    reminders: list[tuple[str, str]] = []
    if not new:
        for key, detail in current:
            prior = previous.get(key)
            if not isinstance(prior, dict) or now - float(prior.get("last_notified") or 0) >= REMINDER_SECONDS:
                reminders.append((key, detail))

    updated: dict[str, dict[str, Any]] = {}
    for key, detail in current:
        prior = previous.get(key) if isinstance(previous.get(key), dict) else {}
        notified = float(prior.get("last_notified") or 0)
        if key in {item[0] for item in new} or key in {item[0] for item in reminders}:
            notified = now
        updated[key] = {
            "detail": detail,
            "first_seen": float(prior.get("first_seen") or now),
            "last_notified": notified,
        }
    state["active"] = updated
    state["last_checked_at"] = now
    return new, recovered, reminders


def render_report(
    new: list[tuple[str, str]], recovered: list[tuple[str, str]], reminders: list[tuple[str, str]]
) -> str:
    sections: list[str] = [f"{DOCTOR_NAME} update"]
    if new:
        sections.extend(["", "New failure:"])
        sections.extend(f"  • FAIL {key}: {detail}" for key, detail in new)
    if reminders:
        sections.extend(["", "Still unresolved:"])
        sections.extend(f"  • FAIL {key}: {detail}" for key, detail in reminders)
    if recovered:
        sections.extend(["", "Recovered:"])
        sections.extend(f"  • {key}" for key, _detail in recovered)
    return "\n".join(sections)


def main() -> int:
    repo_root = repository_root()
    if repo_root is None:
        # This is the only setup failure worth surfacing: it prevents all doctor work.
        print(f"{DOCTOR_NAME} update\n\nNew failure:\n  • FAIL doctor/runtime: Hussh repository is unavailable")
        return 0
    interpreter = python_bin(repo_root)
    state = load_state()
    now = time.time()

    unresolved = heal_services()
    unresolved.extend(heal_session_bloat(repo_root, interpreter, state, now))
    health = health_index(repo_root, interpreter)
    if health is None:
        unresolved.append(("doctor/health-index", "health index returned invalid JSON"))
    else:
        unresolved.extend(health_failures(health))

    # A key is intentionally stable across changing numeric details, preventing
    # session counts or exit codes from resetting the six-hour notification cap.
    by_key: dict[str, str] = {}
    for key, detail in unresolved:
        by_key[key] = detail
    deduplicated = list(by_key.items())
    new, recovered, reminders = transition_report(state, deduplicated, now)
    save_state(state)
    if new or recovered or reminders:
        print(render_report(new, recovered, reminders))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
