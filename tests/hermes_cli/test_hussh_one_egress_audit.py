# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""What leaves this machine.

The tool's only job is to be right about egress. A version that overstates the
problem gets ignored; one that understates it gets someone's data sent to a
vendor. Both failures have already happened in this file's history, so both are
tested.
"""

from __future__ import annotations

import pytest

from hermes_cli import hussh_one_egress_audit as audit


CONFIG = {
    "model": {"provider": "lmstudio", "default": "google/gemma-4-26b-a4b-qat"},
    "auxiliary": {
        "transient_retries": 2,
        "free_only": False,
        "compression": {"provider": "gemini", "model": "gemini-3.7-flash"},
        "vision": {"provider": "auto", "model": ""},
        "review": {"provider": "auto", "model": ""},
    },
}


@pytest.fixture
def local_router(monkeypatch):
    """A router where `auto` lands on the local provider, as it does here."""

    def _resolve(provider, model, task):
        if (provider or "auto") == "auto":
            return "lmstudio", "google/gemma-4-26b-a4b-qat"
        return provider, model

    monkeypatch.setattr(audit, "_resolve_effective", _resolve)


class TestAutoIsNotAssumedToLeak:
    def test_auto_tasks_that_resolve_locally_are_reported_as_staying(
        self, local_router
    ):
        # The first version of this tool assumed `auto` fell straight through to
        # a cloud provider and reported every auto task as leaking. Step 1 of
        # auto-route uses the MAIN provider, which is local here, so that
        # overstated the problem by eighteen tasks. An egress tool that cries
        # wolf gets switched off.
        rows = {r["task"]: r for r in audit.audit_tasks(CONFIG, gate_on=False)}
        assert rows["vision"]["verdict"] == audit.STAYS
        assert rows["review"]["verdict"] == audit.STAYS

    def test_the_configured_value_is_shown_beside_the_effective_one(
        self, local_router
    ):
        # The gap between them is the entire reason a config can look
        # on-device and not be.
        rows = {r["task"]: r for r in audit.audit_tasks(CONFIG, gate_on=False)}
        assert rows["vision"]["configured"] == "auto"
        assert rows["vision"]["effective"] == "lmstudio"


class TestExplicitCloudIsCaught:
    def test_an_explicit_cloud_provider_is_reported_as_leaving(self, local_router):
        rows = {r["task"]: r for r in audit.audit_tasks(CONFIG, gate_on=False)}
        assert rows["compression"]["verdict"] == audit.LEAVES
        assert rows["compression"]["effective"] == "gemini"

    def test_a_refused_route_is_not_reported_as_staying(self, monkeypatch):
        # A refusal means the work did not happen. Reporting it as "stays"
        # would read as "ran locally", which is a different and better claim
        # than the truth.
        monkeypatch.setattr(
            audit, "_resolve_effective", lambda provider, model, task: ("", "")
        )
        rows = audit.audit_tasks(CONFIG, gate_on=True)
        assert {r["verdict"] for r in rows} == {audit.REFUSED}


class TestNonTaskKeysAreSkipped:
    def test_scalars_under_auxiliary_are_not_audited_as_tasks(self, local_router):
        tasks = {r["task"] for r in audit.audit_tasks(CONFIG, gate_on=False)}
        assert "transient_retries" not in tasks
        assert "free_only" not in tasks


class TestMainTurn:
    def test_a_local_main_provider_stays(self):
        assert audit.main_turn(CONFIG)["verdict"] == audit.STAYS

    def test_a_cloud_main_provider_leaves(self):
        cfg = {"model": {"provider": "openai", "default": "gpt-5"}}
        assert audit.main_turn(cfg)["verdict"] == audit.LEAVES


class TestSimulationActuallySimulates:
    def test_the_gate_is_really_replaced_inside_the_block(self):
        # A flag that only LABELS a report "gate on" would print gate-off
        # behaviour under a gate-on heading, showing the gate changing nothing
        # and inviting the owner to conclude it does nothing.
        from agent import auxiliary_client

        original = auxiliary_client._on_device_only_enabled
        with audit.simulated_gate(True):
            assert auxiliary_client._on_device_only_enabled() is True
        with audit.simulated_gate(False):
            assert auxiliary_client._on_device_only_enabled() is False
        assert auxiliary_client._on_device_only_enabled is original

    def test_the_gate_is_restored_even_when_the_body_raises(self):
        from agent import auxiliary_client

        original = auxiliary_client._on_device_only_enabled
        with pytest.raises(RuntimeError):
            with audit.simulated_gate(True):
                raise RuntimeError("audit blew up")
        assert auxiliary_client._on_device_only_enabled is original


class TestReportShape:
    def test_leaves_total_counts_the_main_turn_too(self, local_router):
        # The owner does not experience the main turn and the side tasks
        # separately, so neither does the headline number.
        cfg = {**CONFIG, "model": {"provider": "openai", "default": "gpt-5"}}
        report = audit.build_report(cfg, gate_on=False)
        assert report["leaves_total"] == report["counts"][audit.LEAVES] + 1

    def test_a_clean_machine_reports_zero(self, monkeypatch):
        monkeypatch.setattr(
            audit,
            "_resolve_effective",
            lambda provider, model, task: ("lmstudio", "m"),
        )
        report = audit.build_report(CONFIG, gate_on=True)
        assert report["leaves_total"] == 0
