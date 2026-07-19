# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[2]
MAINTENANCE = ROOT / "scripts" / "maintenance"


def _load_script(filename: str, module_name: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_session_inventory_never_selects_pairing_or_identity_files(tmp_path):
    sys.path.insert(0, str(MAINTENANCE))
    from whatsapp_session_inventory import scan_session_directory

    session = tmp_path / "whatsapp" / "session"
    session.mkdir(parents=True)
    (session / "creds.json").write_text("credentials")
    (session / "identity-key-owner.json").write_text("identity")
    now = 1_000_000.0
    for index in range(5):
        path = session / f"pre-key-{index}.json"
        path.write_text("regenerable")
        os.utime(path, (now, now))

    inventory = scan_session_directory(
        session, now=now, retention_days=7, keep_per_family=2, max_per_family=3
    )

    assert inventory.total_files == 7
    assert inventory.protected_files == 2
    assert inventory.prunable_count == 2
    assert all(path.name.startswith("pre-key-") for path in inventory.prunable_files)
    assert {"creds.json", "identity-key-owner.json"}.isdisjoint(
        {path.name for path in inventory.prunable_files}
    )


def test_doctor_alert_state_deduplicates_then_reminds_and_recovers(tmp_path, monkeypatch):
    doctor = _load_script("hussh-one-doctor-heal.py", "hussh_one_doctor_test_transitions")
    monkeypatch.setattr(doctor, "STATE_PATH", tmp_path / "health" / "state.json")
    state = {"schema": 1, "active": {}}

    new, recovered, reminders = doctor.transition_report(
        state, [("cron/wiki", "execution error")], 100.0
    )
    assert new == [("cron/wiki", "execution error")]
    assert not recovered and not reminders

    new, recovered, reminders = doctor.transition_report(
        state, [("cron/wiki", "a different dynamic detail")], 101.0
    )
    assert not new and not recovered and not reminders

    new, recovered, reminders = doctor.transition_report(
        state, [("cron/wiki", "execution error")], 100.0 + doctor.REMINDER_SECONDS + 1
    )
    assert reminders == [("cron/wiki", "execution error")]

    new, recovered, reminders = doctor.transition_report(state, [], 1000.0 + doctor.REMINDER_SECONDS)
    assert recovered == [("cron/wiki", "execution error")]
    assert not new and not reminders


def test_doctor_state_recovers_from_corrupt_local_json(tmp_path, monkeypatch):
    doctor = _load_script("hussh-one-doctor-heal.py", "hussh_one_doctor_test_corrupt_state")
    state_path = tmp_path / "health" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(doctor, "STATE_PATH", state_path)

    assert doctor.load_state() == {"schema": 1, "active": {}}


def test_doctor_main_is_silent_when_health_is_unchanged(tmp_path, monkeypatch, capsys):
    doctor = _load_script("hussh-one-doctor-heal.py", "hussh_one_doctor_test_silent")
    monkeypatch.setattr(doctor, "STATE_PATH", tmp_path / "health" / "state.json")
    monkeypatch.setattr(doctor, "repository_root", lambda: ROOT)
    monkeypatch.setattr(doctor, "heal_services", lambda: [])
    monkeypatch.setattr(doctor, "heal_session_bloat", lambda *_args: [])
    monkeypatch.setattr(doctor, "health_index", lambda *_args: {"findings": []})

    assert doctor.main() == 0
    assert capsys.readouterr().out == ""
    assert doctor.main() == 0
    assert capsys.readouterr().out == ""


def test_doctor_main_notifies_once_then_sends_one_recovery(tmp_path, monkeypatch, capsys):
    doctor = _load_script("hussh-one-doctor-heal.py", "hussh_one_doctor_test_recovery")
    monkeypatch.setattr(doctor, "STATE_PATH", tmp_path / "health" / "state.json")
    monkeypatch.setattr(doctor, "repository_root", lambda: ROOT)
    monkeypatch.setattr(doctor, "heal_services", lambda: [])
    monkeypatch.setattr(doctor, "heal_session_bloat", lambda *_args: [])
    health = {"findings": [{"status": "fail", "harness": "cron", "name": "wiki", "detail": "error"}]}
    monkeypatch.setattr(doctor, "health_index", lambda *_args: health)

    doctor.main()
    assert "New failure" in capsys.readouterr().out
    doctor.main()
    assert capsys.readouterr().out == ""
    health["findings"] = []
    doctor.main()
    assert "Recovered" in capsys.readouterr().out


def test_doctor_installer_copies_atomically_and_migrates_existing_job(tmp_path, monkeypatch):
    installer = _load_script("hussh_one_doctor_install.py", "hussh_one_doctor_install_test")
    updated: dict[str, object] = {}
    fake_jobs = types.ModuleType("cron.jobs")
    fake_jobs.list_jobs = lambda: [
        {"id": "doctor-id", "name": installer.DOCTOR_JOB_NAME, "script": "old.py", "no_agent": False}
    ]

    def update_job(job_id, changes):
        updated["id"] = job_id
        updated["changes"] = changes
        return {"id": job_id}

    fake_jobs.update_job = update_job
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_jobs)

    hermes_home = tmp_path / ".hermes"
    job_id = installer.install(ROOT, hermes_home, "/usr/bin/python3")

    installed = hermes_home / "scripts" / installer.RUNTIME_SCRIPT_NAME
    assert job_id == "doctor-id"
    assert installed.read_text(encoding="utf-8") == (
        ROOT / "scripts" / installer.SOURCE_SCRIPT_NAME
    ).read_text(encoding="utf-8")
    assert (hermes_home / "hussh-one-runtime.json").is_file()
    assert updated["id"] == "doctor-id"
    assert updated["changes"] == {
        "script": installer.RUNTIME_SCRIPT_NAME,
        "no_agent": True,
        "prompt": installer.DETERMINISTIC_PROMPT,
    }


def test_dashboard_health_uses_reachable_watchdog_child_not_legacy_exit(monkeypatch):
    index = _load_script("hussh-one-health-index.py", "hussh_one_health_index_dashboard_test")
    monkeypatch.setattr(index.sys, "platform", "darwin")
    monkeypatch.setattr(index.shutil, "which", lambda name: "/usr/bin/launchctl" if name == "launchctl" else None)
    monkeypatch.setattr(index, "_dashboard_reachable", lambda: True)
    monkeypatch.setattr(
        index.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(stdout="-\t1\tai.hussh-one.dashboard\n"),
    )
    index._findings.clear()

    index.probe_services()

    finding = next(item for item in index._findings if item["name"] == "ai.hussh-one.dashboard")
    assert finding["status"] == index.WARN
    assert "reachable" in finding["detail"]
