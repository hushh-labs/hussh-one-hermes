# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The Puppy One resource snapshot: omit what you cannot measure.

The rule these pin is the same one the heartbeat allow-list follows. A machine
with no battery is not a machine at 0%, and a probe that failed is not a
reading of zero. Downstream nothing can tell those apart afterwards, so the
snapshot drops a section rather than guessing it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import hussh_one_resources as resources


class TestAgentSection:
    def test_a_local_provider_with_the_gate_on_is_the_full_claim(self) -> None:
        section = resources.agent_section(
            {
                "model": {"provider": "lmstudio", "default": "google/gemma-4-26b-a4b-qat"},
                "hussh_one": {"on_device_only": True},
            }
        )
        assert section == {
            "model": "google/gemma-4-26b-a4b-qat",
            "provider": "lmstudio",
            "on_device": True,
            "on_device_gate": True,
        }

    def test_a_local_pin_without_the_gate_is_reported_separately(self) -> None:
        # Pinning the main turn never covered auxiliary tasks. Collapsing these
        # two into one "on-device" flag is exactly how a PKM save came to think
        # on a vendor model while the config said otherwise.
        section = resources.agent_section({"model": {"provider": "lmstudio", "default": "m"}})
        assert section["on_device"] is True
        assert section["on_device_gate"] is False

    def test_a_cloud_provider_is_not_on_device(self) -> None:
        section = resources.agent_section({"model": {"provider": "anthropic", "default": "claude"}})
        assert section["on_device"] is False

    def test_missing_configuration_yields_nulls_not_invented_names(self) -> None:
        section = resources.agent_section({})
        assert section["model"] is None and section["provider"] is None
        assert section["on_device"] is False


class TestMachineSection:
    def test_it_reports_specs_memory_disk_and_power(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_cli import hussh_one_host_metrics as host

        monkeypatch.setattr(
            host, "host_hardware", lambda: {"brand": "Mac16,5", "processor": "Apple M4 Max", "cpu_cores": 16}
        )
        monkeypatch.setattr(
            host,
            "host_memory_sample",
            lambda: {"mem_total_kib": 128 * 1024 * 1024, "mem_available_kib": 64 * 1024 * 1024},
        )
        monkeypatch.setattr(
            host, "host_battery", lambda: {"present": True, "percent": 27, "charging": False}
        )

        section = resources.machine_section()

        assert section["brand"] == "Mac16,5"
        assert section["ram_total_gb"] == 128.0
        assert section["ram_available_gb"] == 64.0
        assert section["ram_used_pct"] == 50.0
        assert section["battery"] == {"present": True, "percent": 27, "charging": False}
        assert section["disk_free_gb"] > 0

    def test_a_desktop_reports_no_battery_rather_than_zero_percent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli import hussh_one_host_metrics as host

        monkeypatch.setattr(host, "host_hardware", lambda: {})
        monkeypatch.setattr(host, "host_memory_sample", lambda: {})
        monkeypatch.setattr(host, "host_battery", lambda: {"present": False})

        section = resources.machine_section()

        assert section["battery"] == {"present": False}
        assert "percent" not in section["battery"]

    def test_a_failed_probe_drops_its_keys_instead_of_reporting_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli import hussh_one_host_metrics as host

        def _boom():
            raise OSError("probe unavailable")

        monkeypatch.setattr(host, "host_hardware", _boom)
        monkeypatch.setattr(host, "host_memory_sample", _boom)
        monkeypatch.setattr(host, "host_battery", _boom)

        section = resources.machine_section()

        for key in ("brand", "processor", "ram_total_gb", "ram_used_pct", "battery"):
            assert key not in section
        # Disk still answers: sections fail independently.
        assert "disk_free_gb" in section


class TestModelsSection:
    def test_it_totals_resident_size_and_marks_the_current_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli import hussh_one_lmstudio as lms

        monkeypatch.setattr(
            lms,
            "loaded_models",
            lambda: [
                {"identifier": "a/one", "size_gb": 15.6, "status": "IDLE", "context": 262144},
                {"identifier": "b/two", "size_gb": 4.4, "status": "LOADED", "context": 8192},
            ],
        )
        monkeypatch.setattr(lms, "host_memory", lambda: {"available_gb": 67.9})

        section = resources.models_section("a/one")

        assert section["resident_gb"] == 20.0
        assert section["available_gb"] == 67.9
        assert [row["is_current"] for row in section["resident"]] == [True, False]

    def test_nothing_resident_is_an_empty_list_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli import hussh_one_lmstudio as lms

        monkeypatch.setattr(lms, "loaded_models", lambda: [])
        monkeypatch.setattr(lms, "host_memory", lambda: {})

        section = resources.models_section("a/one")

        assert section["resident"] == []
        assert section["resident_gb"] == 0.0
        # No reading is not a reading of zero headroom.
        assert "available_gb" not in section


class TestJobsSection:
    def _fixture(self, tmp_path: Path, *, now: float) -> tuple[Path, Path]:
        jobs = tmp_path / "jobs.json"
        jobs.write_text(
            json.dumps(
                {
                    "jobs": [
                        {"id": "j1", "name": "Auto-Dream", "next_run_at": "2026-09-03T03:10:00-07:00"},
                        {"id": "j2", "name": "Doctor", "next_run_at": "2026-09-02T14:16:00-07:00"},
                        {"id": "j3", "name": "PR train", "state": "paused"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        db = tmp_path / "executions.db"
        connection = sqlite3.connect(db)
        connection.execute("create table executions (id text, status text, claimed_at text)")
        recent = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 3600))
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 72 * 3600))
        connection.executemany(
            "insert into executions values (?, ?, ?)",
            [
                ("a", "completed", recent),
                ("b", "completed", recent),
                ("c", "failed", recent),
                ("d", "unknown", recent),
                ("e", "completed", old),
            ],
        )
        connection.commit()
        connection.close()
        return jobs, db

    def test_it_names_the_next_run_and_the_last_day(self, tmp_path: Path) -> None:
        now = time.time()
        jobs, db = self._fixture(tmp_path, now=now)

        section = resources.jobs_section(jobs_path=jobs, executions_db=db, now=now)

        assert section["enabled"] == 2
        # A job the owner disabled is counted, not hidden: a monitor that drops
        # it cannot explain why something it used to show no longer runs.
        assert section["disabled"] == 1
        assert section["next"]["name"] == "Doctor"
        assert section["last_24h"] == {"completed": 2, "failed": 1, "other": 1}

    def test_a_missing_store_drops_its_part_rather_than_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        section = resources.jobs_section(
            jobs_path=tmp_path / "absent.json", executions_db=tmp_path / "absent.db"
        )
        assert section == {}


class TestCollectResources:
    def test_the_snapshot_carries_every_section_and_the_runtime_counters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resources, "machine_section", lambda: {"brand": "Mac16,5"})
        monkeypatch.setattr(resources, "models_section", lambda model: {"resident": [], "asked": model})
        monkeypatch.setattr(resources, "jobs_section", lambda: {"enabled": 11})

        snapshot = resources.collect_resources(
            config={"model": {"provider": "lmstudio", "default": "m"}},
            active_agents=2,
            busy=True,
            version="0.20.5",
        )

        assert snapshot["agent"]["active_agents"] == 2
        assert snapshot["agent"]["busy"] is True
        assert snapshot["agent"]["version"] == "0.20.5"
        assert snapshot["models"]["asked"] == "m"
        assert snapshot["jobs"] == {"enabled": 11}
        assert isinstance(snapshot["generated_at"], int)

    def test_one_failing_section_never_takes_down_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args, **_kwargs):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(resources, "machine_section", _boom)
        monkeypatch.setattr(resources, "models_section", _boom)
        monkeypatch.setattr(resources, "jobs_section", _boom)

        snapshot = resources.collect_resources(config={"model": {"provider": "lmstudio"}})

        assert snapshot["machine"] == {}
        assert snapshot["models"] == {}
        assert snapshot["jobs"] == {}
        assert snapshot["agent"]["on_device"] is True
