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


# --------------------------------------------------------------------------
# GCP shape resolution — what can actually be provisioned
# --------------------------------------------------------------------------


def test_tpu_is_refused_rather_than_provisioned_as_a_gpu_vm():
    """TPUs live behind a different API entirely.

    Before this check the provider would happily build a Compute Engine request
    for ``tpu-v5e`` — booting an accelerator-less VM that bills by the hour while
    doing nothing the person asked for.
    """
    from hermes_cli.hussh_one_burst.providers import UnsupportedAccelerator

    for tpu in ("tpu-v5e", "tpu-v6e", "tpu-v5p"):
        with pytest.raises(UnsupportedAccelerator, match="Cloud TPU"):
            GcpBurstProvider.resolve_shape(tpu, 1)


def test_unknown_accelerator_is_refused():
    from hermes_cli.hussh_one_burst.providers import UnsupportedAccelerator

    with pytest.raises(UnsupportedAccelerator):
        GcpBurstProvider.resolve_shape("nvidia-imaginary", 1)


def test_unsellable_chip_count_is_refused_with_the_valid_counts_named():
    from hermes_cli.hussh_one_burst.providers import UnsupportedAccelerator

    with pytest.raises(UnsupportedAccelerator, match=r"\(8,\)"):
        GcpBurstProvider.resolve_shape("b200-180", 1)


def test_whole_node_parts_resolve_to_their_real_machine_types():
    assert GcpBurstProvider.resolve_shape("h100-80", 8)[0] == "a3-highgpu-8g"
    assert GcpBurstProvider.resolve_shape("h200-141", 8)[0] == "a3-ultragpu-8g"
    assert GcpBurstProvider.resolve_shape("b200-180", 8)[0] == "a4-highgpu-8g"
    assert GcpBurstProvider.resolve_shape("gb200-186", 4)[0] == "a4x-highgpu-4g"


def test_a100_machine_type_scales_with_chip_count():
    assert GcpBurstProvider.resolve_shape("a100-40", 2)[0] == "a2-highgpu-2g"
    assert GcpBurstProvider.resolve_shape("a100-80", 4)[0] == "a2-ultragpu-4g"


def test_t4_attaches_a_guest_accelerator_while_a100_does_not():
    """T4 bolts onto an N1; the A2 family has the GPUs baked into the machine."""
    _machine, accel, _n = GcpBurstProvider.resolve_shape("nvidia-t4", 2)
    assert accel == "nvidia-tesla-t4"
    assert GcpBurstProvider.resolve_shape("a100-40", 2)[1] is None


def test_every_catalog_recommendation_resolves_to_a_real_shape():
    """The recommender and the provisioner must not disagree.

    They previously did: 9 of 14 realistic workloads produced a recommendation
    Compute Engine could not fulfil.
    """
    from hermes_cli.hussh_one_burst import recommend_hardware
    from hermes_cli.hussh_one_burst.providers import UnsupportedAccelerator

    for vram in (8, 20, 30, 50, 64, 90, 120, 160, 200, 300, 400, 640, 900, 1_200):
        rec = recommend_hardware(float(vram), "gpu", 1)
        GcpBurstProvider.resolve_shape(rec.accel.id, rec.count)  # must not raise
        with pytest.raises(UnsupportedAccelerator):
            GcpBurstProvider.resolve_shape("tpu-v5e", rec.count)


def test_instance_names_do_not_collide_across_bursts():
    """Two bursts of the same shape used to produce the same name, and the
    second would fail with 409 ALREADY_EXISTS."""
    import re
    from unittest.mock import MagicMock, patch

    names = set()
    for _ in range(25):
        provider = GcpBurstProvider(project="p", region="us-central1")
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200)
        ref = MagicMock(project="p", region="us-central1")
        with patch.object(provider, "_authed_session", return_value=(session, ref)):
            handle = provider.provision(
                InstanceSpec("a100-40", 2, "job", 30.0)
            )
        names.add(handle.id)
        # GCP resource naming: lowercase, starts with a letter, <= 63 chars.
        assert re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", handle.id)
        assert len(handle.id) <= 63
    assert len(names) == 25


def test_provision_body_carries_what_compute_engine_actually_requires():
    """The body used to contain only name, labels and scheduling — a request
    that would 400 on arrival."""
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        provider.provision(InstanceSpec("nvidia-t4", 2, "job", 30.0))

    body = session.post.call_args.kwargs["json"]
    assert body["machineType"].endswith("/n1-standard-8")
    assert body["disks"][0]["boot"] is True
    assert body["disks"][0]["autoDelete"] is True
    assert body["disks"][0]["initializeParams"]["sourceImage"]
    assert body["networkInterfaces"]
    # Accelerator instances cannot live-migrate, and SPOT forbids auto-restart.
    assert body["scheduling"]["onHostMaintenance"] == "TERMINATE"
    assert body["scheduling"]["automaticRestart"] is False
    assert body["scheduling"]["instanceTerminationAction"] == "DELETE"
    assert body["guestAccelerators"][0]["acceleratorCount"] == 2


def test_provision_carries_no_workload_information_to_the_cloud():
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        provider.provision(InstanceSpec("a100-40", 1, "my secret research project", 30.0))
    assert "secret" not in repr(session.post.call_args.kwargs["json"]).lower()


# --------------------------------------------------------------------------
# The run tool refuses before it asks for money
# --------------------------------------------------------------------------


def _run_tool():
    from hermes_cli.hussh_one_burst.mcp_server import _build_server

    return _build_server()


def test_a_job_too_large_for_one_node_is_refused_before_approval():
    """It used to ask a person to approve $110/hour of hardware that could not
    hold the job, and only then fail."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    srv = _run_tool()
    ctx = MagicMock()
    ctx.elicit = AsyncMock()
    result = asyncio.run(
        srv.call_tool("hussh_burst_run", {"vram_gb": 5000.0, "minutes": 60.0})
    )
    payload = result[1] if isinstance(result, tuple) else result
    assert payload["success"] is False
    assert payload["status"] == "does_not_fit"
    ctx.elicit.assert_not_called()


def test_a_gcp_unsupported_accelerator_is_refused_before_approval():
    import asyncio

    srv = _run_tool()
    result = asyncio.run(
        srv.call_tool(
            "hussh_burst_run",
            {"preset_id": "fold-protein", "provider": "gcp", "project": "p"},
        )
    )
    payload = result[1] if isinstance(result, tuple) else result
    assert payload["success"] is False
    assert payload["status"] == "unsupported_hardware"
    assert "TPU" in payload["reason"]
    assert "nothing was billed" in payload["advice"].lower()


# --------------------------------------------------------------------------
# Teardown confirms absence, rather than trusting an accepted delete
# --------------------------------------------------------------------------


def _gcp_with_fake_session(responses, **kw):
    """A GcpBurstProvider whose session returns scripted GET status codes."""
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(
        project="p", region="us-central1", sleep=lambda _s: None, **kw
    )
    session = MagicMock()
    session.delete.return_value = MagicMock(status_code=200)
    session.get.side_effect = [MagicMock(status_code=code) for code in responses]
    ref = MagicMock(project="p", region="us-central1")
    return provider, session, patch.object(
        provider, "_authed_session", return_value=(session, ref)
    )


def test_teardown_waits_until_the_instance_is_actually_gone():
    """Verified against real GCP: delete is asynchronous.

    The first live burst reported torn_down=true while the instance was still
    STAGING with a T4 attached and billing.
    """
    from hermes_cli.hussh_one_burst.providers import InstanceHandle

    provider, session, ctx = _gcp_with_fake_session([200, 200, 404])
    handle = InstanceHandle(id="i-1", destination="p/us-central1-a", detail={"zone": "us-central1-a"})
    with ctx:
        assert provider.teardown(handle) is True
    assert handle.torn_down
    assert session.get.call_count == 3


def test_teardown_that_cannot_confirm_reports_a_leak_rather_than_success():
    from hermes_cli.hussh_one_burst.providers import InstanceHandle

    ticks = iter([0.0, 1.0, 2.0, 999.0])
    provider, _session, ctx = _gcp_with_fake_session(
        [200] * 10, teardown_confirm_seconds=10.0, clock=lambda: next(ticks)
    )
    handle = InstanceHandle(id="i-2", destination="p/us-central1-a", detail={"zone": "us-central1-a"})
    with ctx, pytest.raises(RuntimeError, match="still present"):
        provider.teardown(handle)
    assert handle.torn_down is False


def test_an_unconfirmed_teardown_surfaces_on_the_receipt_as_a_leak():
    """The receipt must say "I do not know this is off", not claim release."""
    from unittest.mock import MagicMock, patch

    ticks = iter([0.0, 999.0])
    provider = GcpBurstProvider(
        project="p", region="us-central1", teardown_confirm_seconds=1.0,
        clock=lambda: next(ticks), sleep=lambda _s: None,
    )
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200)
    session.delete.return_value = MagicMock(status_code=200)
    session.get.return_value = MagicMock(status_code=200)  # never disappears
    ref = MagicMock(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        receipt = run_burst(
            BurstRequest("job", "nvidia-t4", 1, 0.35, 5.0), provider
        )
    assert receipt.leaked_instance
    assert "still present" in (receipt.teardown_error or "")
    assert "billing" in receipt.as_dict()["warning"]


def test_a_delete_that_returns_404_is_already_gone_and_needs_no_polling():
    from unittest.mock import MagicMock, patch

    from hermes_cli.hussh_one_burst.providers import InstanceHandle

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.delete.return_value = MagicMock(status_code=404)
    ref = MagicMock(project="p", region="us-central1")
    handle = InstanceHandle(id="i-3", destination="p/us-central1-a", detail={"zone": "us-central1-a"})
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        assert provider.teardown(handle) is True
    session.get.assert_not_called()
