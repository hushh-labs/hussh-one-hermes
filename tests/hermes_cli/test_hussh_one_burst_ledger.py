# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The receipt ledger: bursts have to still be findable tomorrow.

Before this existed, ``run_burst`` handed a receipt to one caller and dropped it
— including the receipts reporting a leaked instance, which are exactly the ones
somebody needs later. These tests hold two lines: everything gets written down,
and writing it down can never cost you the burst.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_burst.execution import BurstRequest, run_burst
from hermes_cli.hussh_one_burst.ledger import (
    leaked_instances,
    read_receipts,
    record_receipt,
)
from hermes_cli.hussh_one_burst.providers import MockBurstProvider

REQUEST = BurstRequest(
    label="ledger test", accelerator_id="nvidia-t4", chip_count=1,
    usd_per_hour=0.35, deadline_minutes=5.0,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import hermes_cli.hussh_one_burst.ledger as mod

    target = tmp_path / "burst-receipts.jsonl"
    monkeypatch.setattr(mod, "default_ledger_path", lambda: target)
    return target


# --------------------------------------------------------------------------
# Everything gets written down
# --------------------------------------------------------------------------


def test_a_burst_records_itself_without_being_asked(ledger):
    run_burst(REQUEST, MockBurstProvider())
    rows = list(read_receipts(ledger))
    assert len(rows) == 1
    assert rows[0]["workload"] == "ledger test"
    assert rows[0]["torn_down"] is True
    assert rows[0]["schema_version"] == 1
    assert rows[0]["recorded_at"] > 0


def test_a_leaked_instance_is_findable_afterwards(ledger):
    """The reason the ledger exists.

    A leak was previously reported once, to one caller, and then gone. It is the
    single most important thing to be able to look up later, because it is the
    one that is still costing money.
    """
    run_burst(REQUEST, MockBurstProvider())  # clean
    run_burst(REQUEST, MockBurstProvider(fail_on_teardown=True))  # leaked
    run_burst(REQUEST, MockBurstProvider())  # clean

    leaked = leaked_instances(ledger)
    assert len(leaked) == 1
    assert leaked[0]["torn_down"] is False
    assert "billing" in leaked[0]["warning"]
    assert leaked[0]["instance_id"]


def test_receipts_accumulate_oldest_first(ledger):
    for n in range(3):
        run_burst(
            BurstRequest(f"job-{n}", "nvidia-t4", 1, 0.35, 5.0), MockBurstProvider()
        )
    assert [r["workload"] for r in read_receipts(ledger)] == ["job-0", "job-1", "job-2"]


def test_a_failed_burst_is_recorded_too(ledger):
    run_burst(REQUEST, MockBurstProvider(fail_on_provision=True))
    rows = list(read_receipts(ledger))
    assert len(rows) == 1
    assert rows[0]["status"] == "provision_failed"
    assert rows[0]["success"] is False


def test_recording_can_be_turned_off(ledger):
    run_burst(REQUEST, MockBurstProvider(), record=False)
    assert list(read_receipts(ledger)) == []


# --------------------------------------------------------------------------
# Writing it down can never cost you the burst
# --------------------------------------------------------------------------


def test_an_unwritable_ledger_does_not_fail_the_burst(tmp_path, monkeypatch):
    """A full disk must not turn a released instance into a reported failure."""
    import hermes_cli.hussh_one_burst.ledger as mod

    # A path whose parent is a file, so mkdir and open both fail.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "default_ledger_path", lambda: blocker / "x.jsonl")

    provider = MockBurstProvider()
    receipt = run_burst(REQUEST, provider)

    assert receipt.status == "completed"
    assert receipt.torn_down
    assert provider.live_instances == []


def test_record_receipt_returns_none_when_it_cannot_write(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")

    class _R:
        def as_dict(self):
            return {"workload": "x", "torn_down": True}

    assert record_receipt(_R(), blocker / "nested.jsonl") is None


def test_a_truncated_line_does_not_make_the_history_unreadable(ledger):
    """An append-only file written by a process that may be killed can end
    mid-line. One bad row must not hide the good ones."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"workload": "first", "torn_down": True}) + "\n"
        + '{"workload": "truncated"' + "\n"
        + json.dumps({"workload": "third", "torn_down": False}) + "\n",
        encoding="utf-8",
    )
    rows = list(read_receipts(ledger))
    assert [r["workload"] for r in rows] == ["first", "third"]
    assert len(leaked_instances(ledger)) == 1


def test_reading_a_ledger_that_does_not_exist_is_empty_not_an_error(tmp_path):
    assert list(read_receipts(tmp_path / "nope.jsonl")) == []
    assert leaked_instances(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------
# Nothing secret reaches the disk
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        {"private_key": "-----BEGIN PRIVATE KEY-----"},
        {"blob": "-----BEGIN RSA PRIVATE KEY-----"},
        {"client_secret": "abc"},
        {"refresh_token": "1//xyz"},
    ],
)
def test_a_receipt_carrying_credential_material_is_refused(tmp_path, secret):
    """Raising is correct here. Losing the row beats writing a key to disk."""

    class _R:
        def as_dict(self):
            return {"workload": "x", **secret}

    with pytest.raises(ValueError, match="refusing to write"):
        record_receipt(_R(), tmp_path / "l.jsonl")
    assert not (tmp_path / "l.jsonl").exists()


def test_a_real_receipt_records_project_and_source_but_no_key(ledger):
    from hermes_cli.hussh_one_burst.credentials import CredentialRef

    provider = MockBurstProvider()
    provider.credential_ref = CredentialRef("proj", "us-central1", "environment")
    run_burst(REQUEST, provider)

    row = next(iter(read_receipts(ledger)))
    assert row["project"] == "proj"
    assert row["credential_source"] == "environment"
    blob = json.dumps(row).lower()
    for forbidden in ("private_key", "begin private", "client_secret", "refresh_token"):
        assert forbidden not in blob
