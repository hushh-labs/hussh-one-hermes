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
        assert jobs
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

    def test_reasoning_effort_is_reconciled_when_manifest_pins_it(self, sync):
        store = _Store([self._existing(reasoning_effort="xhigh")])
        entry = self._entry(reasoning_effort="high")
        report = sync.reconcile([entry], load=store.load, create=store.create,
                                update=store.update, apply=True)
        assert "reasoning_effort" in report["updated"]["Hushh Core Board Sync"]
        assert store.updates[0][1]["reasoning_effort"] == "high"

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


def test_removed_jobs_preserve_history_and_do_not_return(sync, tmp_path, monkeypatch):
    from cron import jobs
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    removed = []
    for name, script in sync.REMOVED_JOBS.items():
        (scripts / script).write_text('# removed feature\n')
        job = jobs.create_job(prompt=None, name=name, script=script, no_agent=True, schedule='0 3 * * *')
        removed.append(job['id'])
        jobs.save_job_output(job['id'], 'Historical evidence must remain')
    other = jobs.create_job(name='Owner job', prompt='Read only', schedule='0 5 * * *', deliver='local')
    before = jobs.get_job(other['id'])
    report = sync.remove_auto_dream(home=tmp_path, store=jobs, active=lambda _: False, apply=False)
    assert len(report['jobs']) == 2
    assert len(jobs.load_jobs()) == 3
    report = sync.remove_auto_dream(home=tmp_path, store=jobs, active=lambda _: False, apply=True)
    assert not report['errors']
    assert jobs.get_job(other['id']) == before
    assert all(jobs.get_job(j) is None for j in removed)
    assert all(list((tmp_path / 'cron' / 'output' / j).glob('*.md')) for j in removed)
    assert all(not (scripts / f).exists() for f in sync.REMOVED_JOBS.values())
    assert not sync.remove_auto_dream(home=tmp_path, store=jobs, active=lambda _: False, apply=True)['jobs']
    manifest = sync.load_manifest()
    sync.reconcile(manifest, load=jobs.load_jobs, create=jobs.create_job, update=jobs.update_job, apply=True)
    assert not any(j['name'] in sync.REMOVED_JOBS for j in jobs.load_jobs())


def test_active_removed_job_is_paused_without_removing_scripts(sync, tmp_path, monkeypatch):
    from cron import jobs
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    name, script = next(iter(sync.REMOVED_JOBS.items()))
    path = tmp_path / 'scripts' / script
    path.parent.mkdir(); path.write_text('# running\n')
    job = jobs.create_job(prompt=None, name=name, script=script, no_agent=True, schedule='0 3 * * *')
    report = sync.remove_auto_dream(home=tmp_path, store=jobs, active=lambda _: True, apply=True)
    assert report['errors'] and path.exists()
    assert jobs.get_job(job['id'])['enabled'] is False
