# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle tests for Xtreme Burst execution.

The bulk of these exist for one invariant: **anything provisioned gets
released.** They are written as "after this run, is anything still live?"
rather than "was teardown called", because the thing that costs money is a
running instance, not a missing function call.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_burst.credentials import (
    CredentialError,
    CredentialRef,
    _decode_sa_json,
    resolve_region,
)
from hermes_cli.hussh_one_burst.execution import BurstRequest, run_burst
from hermes_cli.hussh_one_burst.providers import (
    BurstProvider,
    GcpBurstProvider,
    InstanceSpec,
    MockBurstProvider,
    resolve_provider,
)

REQUEST = BurstRequest(
    label="test workload",
    accelerator_id="a100-40",
    chip_count=2,
    usd_per_hour=8.0,
    deadline_minutes=10.0,
)


# --------------------------------------------------------------------------
# The teardown guarantee
# --------------------------------------------------------------------------


def test_instance_is_released_after_a_successful_run():
    provider = MockBurstProvider()
    receipt = run_burst(REQUEST, provider)
    assert receipt.status == "completed"
    assert receipt.torn_down
    assert not receipt.leaked_instance
    assert provider.live_instances == []


def test_instance_is_released_when_the_workload_raises():
    provider = MockBurstProvider()

    def _boom(_handle):
        raise RuntimeError("the workload exploded")

    receipt = run_burst(REQUEST, provider, execute=_boom)
    assert receipt.status == "failed"
    assert "exploded" in (receipt.error or "")
    assert receipt.torn_down
    assert provider.live_instances == []


def test_instance_is_released_when_the_deadline_is_exceeded():
    provider = MockBurstProvider()
    # start, deadline check, then the elapsed reading taken during release.
    ticks = iter([0.0, 10_000.0, 10_000.0])

    receipt = run_burst(REQUEST, provider, clock=lambda: next(ticks))
    assert receipt.status == "deadline_exceeded"
    assert receipt.torn_down
    assert provider.live_instances == []


def test_instance_is_released_on_keyboard_interrupt_and_the_signal_survives():
    """A person pressing Ctrl-C must not leave an accelerator billing."""
    provider = MockBurstProvider()

    def _interrupt(_handle):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_burst(REQUEST, provider, execute=_interrupt)
    assert provider.live_instances == []
    assert provider.teardown_calls


def test_provision_failure_leaves_nothing_to_release():
    provider = MockBurstProvider(fail_on_provision=True)
    receipt = run_burst(REQUEST, provider)
    assert receipt.status == "provision_failed"
    assert receipt.instance_id is None
    assert not receipt.leaked_instance
    assert provider.teardown_calls == []


def test_a_failed_teardown_is_reported_loudly_rather_than_swallowed():
    provider = MockBurstProvider(fail_on_teardown=True)
    receipt = run_burst(REQUEST, provider)
    assert receipt.leaked_instance
    assert receipt.teardown_error
    payload = receipt.as_dict()
    assert "warning" in payload
    assert "billing" in payload["warning"]


def test_teardown_is_attempted_exactly_once_per_run():
    provider = MockBurstProvider()
    run_burst(REQUEST, provider)
    assert len(provider.teardown_calls) == 1


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def test_receipt_records_cost_and_hardware():
    receipt = run_burst(REQUEST, MockBurstProvider())
    payload = receipt.as_dict()
    assert payload["accelerator"] == "a100-40"
    assert payload["chip_count"] == 2
    assert payload["success"] is True
    assert payload["estimated_cost_usd"] >= 0.0


def test_receipt_never_carries_credential_material():
    ref = CredentialRef(project="someone-project", region="us-central1", source="request")
    receipt = run_burst(REQUEST, MockBurstProvider())
    receipt.credential = ref
    payload = receipt.as_dict()
    assert payload["project"] == "someone-project"
    assert payload["credential_source"] == "request"
    blob = repr(payload).lower()
    for forbidden in ("private_key", "begin private", "client_secret", "refresh_token"):
        assert forbidden not in blob


def test_payload_seam_is_recorded_when_absent():
    """The receipt must not imply a workload ran when none was supplied."""
    receipt = run_burst(REQUEST, MockBurstProvider())
    assert any("lifecycle only" in event for event in receipt.events)


def test_payload_seam_receives_the_handle():
    seen = []
    run_burst(REQUEST, MockBurstProvider(), execute=lambda h: seen.append(h.id))
    assert len(seen) == 1
    assert seen[0].startswith("mock-instance-")


# --------------------------------------------------------------------------
# The provider seam
# --------------------------------------------------------------------------


def test_mock_provider_satisfies_the_protocol():
    assert isinstance(MockBurstProvider(), BurstProvider)


def test_gcp_provider_satisfies_the_protocol():
    assert isinstance(GcpBurstProvider(project="p"), BurstProvider)


def test_resolve_provider_defaults_to_mock_so_spend_is_opt_in():
    assert isinstance(resolve_provider(), MockBurstProvider)
    assert isinstance(resolve_provider("mock"), MockBurstProvider)


def test_resolve_provider_returns_gcp_when_asked():
    assert isinstance(resolve_provider("gcp", project="p"), GcpBurstProvider)


def test_resolve_provider_rejects_an_unknown_backend():
    with pytest.raises(ValueError, match="Unknown burst provider"):
        resolve_provider("aws-but-not-implemented")


def test_gcp_provider_names_its_destination_before_approval():
    text = GcpBurstProvider(project="my-proj", region="europe-west4").describe_destination()
    assert "my-proj" in text
    assert "europe-west4" in text


def test_mock_destination_says_it_is_simulated():
    assert "mock" in MockBurstProvider().describe_destination().lower()


def test_instance_spec_carries_no_workload_contents():
    """The spec is what crosses to the cloud; it must be resource numbers only."""
    spec = InstanceSpec(
        accelerator_id="h100-80", chip_count=1, label="x", deadline_minutes=5.0
    )
    assert set(spec.__dataclass_fields__) == {
        "accelerator_id",
        "chip_count",
        "label",
        "deadline_minutes",
    }


# --------------------------------------------------------------------------
# The credential broker
# --------------------------------------------------------------------------


def test_service_account_json_is_accepted_plain_or_base64():
    import base64
    import json

    info = {"client_email": "burst@example.iam.gserviceaccount.com", "project_id": "p"}
    raw = json.dumps(info)
    assert _decode_sa_json(raw)["project_id"] == "p"
    encoded = base64.b64encode(raw.encode()).decode()
    assert _decode_sa_json(encoded)["project_id"] == "p"


def test_a_malformed_key_fails_closed():
    with pytest.raises(CredentialError):
        _decode_sa_json("not json and not base64 %%%")
    with pytest.raises(CredentialError):
        _decode_sa_json('{"missing": "client_email"}')


def test_region_precedence_prefers_the_explicit_argument(monkeypatch):
    monkeypatch.setenv("HUSSH_BURST_REGION", "europe-west4")
    assert resolve_region("asia-east1") == "asia-east1"
    assert resolve_region() == "europe-west4"
    monkeypatch.delenv("HUSSH_BURST_REGION")
    assert resolve_region() == "us-central1"


def test_credential_ref_exposes_project_and_source_only():
    ref = CredentialRef(project="p", region="r", source="environment")
    assert ref.as_dict() == {
        "project": "p",
        "region": "r",
        "credential_source": "environment",
    }
