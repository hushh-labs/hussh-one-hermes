# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The daily jobs as a versioned product: scripts installed, jobs reconciled by name.

Until 2026-09-02 every job lived only in one machine's ~/.hermes. These pin
the reconciliation contract: create what is missing, update only the fields
the manifest owns, never touch the owner's delivery target or model, never
remove or revive a job the manifest does not name.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SYNC = REPO / "scripts" / "hussh-one-cron" / "hussh-one-cron-sync.py"
MANIFEST = REPO / "scripts" / "hussh-one-cron" / "jobs.manifest.json"


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("hussh_one_cron_sync", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Store:
    def __init__(self, jobs):
        self.jobs = jobs
        self.created = []
        self.updates = []

    def load(self):
        return [dict(j) for j in self.jobs]

    def create(self, **fields):
        self.created.append(fields)
        return fields

    def update(self, job_id, updates):
        self.updates.append((job_id, updates))
        return updates


class TestTheManifestIsComplete:
    def test_every_manifest_script_and_prompt_exists_in_the_repo(self, sync):
        jobs = sync.load_manifest(MANIFEST)
        assert len(jobs) >= 10
        for job in jobs:
            assert (SYNC.parent / job["script"]).exists(), job["script"]
            if not job.get("no_agent"):
                assert job["prompt"].strip(), job["name"]
                assert "enabled_toolsets" in job, job["name"]

    def test_the_disabled_pr_train_jobs_are_not_in_the_manifest(self, sync):
        names = {j["name"] for j in sync.load_manifest(MANIFEST)}
        assert not any("PR Governance" in n or "PR Maintainer" in n for n in names)


class TestReconcile:
    def _entry(self, **overrides):
        entry = {
            "name": "Hushh Core Board Sync", "schedule": "35 5 * * *", "script": "board_sync.py",
            "enabled_toolsets": ["terminal", "file", "no_mcp"], "skills": [], "prompt": "Run the sync\n",
            "default_deliver": "local",
        }
        entry.update(overrides)
        return entry

    def _existing(self, **overrides):
        job = {
            "id": "j1", "name": "Hushh Core Board Sync",
            "schedule": {"kind": "cron", "expr": "35 5 * * *", "display": "35 5 * * *"},
            "script": "board_sync.py", "no_agent": False,
            "enabled_toolsets": ["terminal", "file", "no_mcp"], "skills": [],
            "prompt": "Run the sync\n", "deliver": "local,whatsapp:owner",
            "model": "google/gemma-4-26b-a4b-qat", "provider": "lmstudio",
        }
        job.update(overrides)
        return job

    def test_a_matching_job_is_left_alone(self, sync):
        store = _Store([self._existing()])
        report = sync.reconcile([self._entry()], load=store.load, create=store.create,
                                update=store.update, apply=True)
        assert report["unchanged"] == ["Hushh Core Board Sync"]
        assert store.updates == [] and store.created == []

    def test_only_managed_fields_are_updated_and_deliver_is_never_touched(self, sync):
        store = _Store([self._existing(enabled_toolsets=["terminal", "file"])])
        report = sync.reconcile([self._entry(schedule="40 5 * * *")], load=store.load,
                                create=store.create, update=store.update, apply=True)
        assert set(report["updated"]["Hushh Core Board Sync"]) == {"schedule", "enabled_toolsets"}
        (job_id, updates), = store.updates
        assert job_id == "j1"
        assert updates == {"schedule": "40 5 * * *", "enabled_toolsets": ["terminal", "file", "no_mcp"]}
        assert "deliver" not in updates and "model" not in updates and "provider" not in updates

    def test_a_missing_job_is_created_with_the_default_delivery(self, sync):
        store = _Store([])
        report = sync.reconcile([self._entry()], load=store.load, create=store.create,
                                update=store.update, apply=True)
        assert report["created"] == ["Hushh Core Board Sync"]
        created, = store.created
        assert created["deliver"] == "local" and created["schedule"] == "35 5 * * *"
        assert created["enabled_toolsets"] == ["terminal", "file", "no_mcp"]

    def test_check_mode_reports_without_writing(self, sync):
        store = _Store([self._existing(script="old.py")])
        report = sync.reconcile([self._entry()], load=store.load, create=store.create,
                                update=store.update, apply=False)
        assert "script" in report["updated"]["Hushh Core Board Sync"]
        assert store.updates == []

    def test_jobs_the_manifest_does_not_name_are_ignored(self, sync):
        store = _Store([self._existing(id="pr", name="PR Governance Train", enabled=False)])
        report = sync.reconcile([self._entry()], load=store.load, create=store.create,
                                update=store.update, apply=True)
        assert report["created"] == ["Hushh Core Board Sync"]
        assert store.updates == []  # the disabled job is untouched

    def test_a_script_job_has_no_prompt_or_toolsets_to_manage(self, sync):
        fields = sync.desired_fields({"name": "x", "schedule": "every 15m", "script": "d.py", "no_agent": True})
        assert fields == {"schedule": "every 15m", "script": "d.py", "no_agent": True}


class TestScriptInstall:
    def test_only_differing_scripts_are_copied_and_helpers_travel(self, sync, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.py").write_text("print(1)\n", encoding="utf-8")
        (source / "b.sh").write_text("echo b\n", encoding="utf-8")
        (source / "notes.md").write_text("not a script\n", encoding="utf-8")
        (source / "lib").mkdir()
        (source / "lib" / "helper.py").write_text("x = 1\n", encoding="utf-8")
        target = tmp_path / "scripts"
        target.mkdir()
        (target / "a.py").write_text("print(1)\n", encoding="utf-8")  # identical already
        assert sorted(sync.install_scripts(source, target, apply=False)) == ["b.sh", "lib/"]
        assert not (target / "b.sh").exists()
        assert sorted(sync.install_scripts(source, target, apply=True)) == ["b.sh", "lib/"]
        assert (target / "b.sh").read_text() == "echo b\n"
        assert (target / "lib" / "helper.py").exists()
        assert not (target / "notes.md").exists()
        assert sync.install_scripts(source, target, apply=False) == []
