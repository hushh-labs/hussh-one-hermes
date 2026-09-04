# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle tests for Xtreme Burst execution.

The bulk of these exist for one invariant: **anything provisioned gets
released.** They are written as "after this run, is anything still live?"
rather than "was teardown called", because the thing that costs money is a
running instance, not a missing function call.
"""

from __future__ import annotations

import json

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


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Keep receipt recording out of the real profile.

    ``run_burst`` records by default — that is the point of the ledger — so
    without this every test in this file would append to the owner's
    ``burst-receipts.jsonl``.
    """
    import hermes_cli.hussh_one_burst.ledger as ledger

    target = tmp_path / "burst-receipts.jsonl"
    monkeypatch.setattr(ledger, "default_ledger_path", lambda: target)
    return target


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


def test_the_deadline_reaches_compute_engine_as_the_real_time_bound():
    """`maxRunDuration` is the only brake that works when Hermes is not there.

    `run_burst` *detects* an overrun — it checks the clock after `execute`
    returns, so a workload that never returns never trips it, and teardown never
    runs. What actually stops the billing in that case is Compute Engine
    deleting the instance on its own. That makes this field, and its unit, the
    load-bearing one: seconds, not minutes, and derived from the same deadline
    the person approved. Nothing tested it.
    """
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        provider.provision(InstanceSpec("nvidia-t4", 1, "job", 45.0))

    scheduling = session.post.call_args.kwargs["json"]["scheduling"]
    assert scheduling["maxRunDuration"] == {"seconds": "2700"}, (
        "45 minutes must reach GCP as 2700 seconds — a minutes/seconds mixup "
        "here buys a 60x longer instance than the person approved"
    )
    # SPOT is the other half: it self-terminates, so a stuck burst has two
    # independent ways to stop costing money.
    assert scheduling["provisioningModel"] == "SPOT"


#: A label of the kind a person actually writes.  Every token is distinctive
#: enough that finding one in an outbound request means the label leaked, and
#: none of them collides with anything Compute Engine legitimately needs.
_REVEALING_LABEL = "finetune payroll reconciliation acmecorp"


def _assert_label_absent(*blobs: str) -> None:
    """Fail if *any* token of :data:`_REVEALING_LABEL` appears in ``blobs``.

    Token-by-token on purpose.  Checking for one hand-picked word passes for a
    request carrying every *other* word of the label, which is not the
    invariant.  A label is free text the person wrote — "finetune-on-patient-
    notes" says plenty about a workload before a byte of the workload moves.
    """
    haystack = " ".join(blobs).lower()
    leaked = [tok for tok in _REVEALING_LABEL.split() if tok in haystack]
    assert not leaked, f"label token(s) {leaked} reached the cloud: {haystack[:400]}"


def test_provision_carries_no_workload_information_to_the_cloud():
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        provider.provision(InstanceSpec("a100-40", 1, _REVEALING_LABEL, 30.0))
    call = session.post.call_args
    _assert_label_absent(json.dumps(call.kwargs["json"]), *(str(a) for a in call.args))


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
    # An unconfigured MagicMock compares truthy against 400, so provisioning
    # would "fail" for any test that used this helper end to end.
    session.post.return_value = MagicMock(status_code=200)
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


def test_no_label_token_reaches_the_cloud_anywhere_in_a_whole_burst():
    """Provision *and* teardown, url and body alike — not just the first call.

    The placement decision runs on the person's own machine so that nothing
    about a workload has to leave it.  That guarantee is worth exactly as much
    as the requests the burst actually makes, so this walks the full lifecycle
    and inspects every one of them: a label smuggled into a query parameter or
    a teardown URL would be just as much of a leak as one in the body.
    """
    provider, session, ctx = _gcp_with_fake_session([404])
    with ctx:
        receipt = run_burst(
            BurstRequest(
                label=_REVEALING_LABEL,
                accelerator_id="nvidia-t4",
                chip_count=1,
                usd_per_hour=0.35,
                deadline_minutes=5.0,
            ),
            provider,
            clock=iter([0.0, 1.0, 2.0, 3.0]).__next__,
            record=False,
        )

    assert receipt.status == "completed"
    assert not receipt.leaked_instance
    seen: list[str] = []
    for method in (session.post, session.delete, session.get):
        for call in method.call_args_list:
            seen.extend(str(a) for a in call.args)
            for key, value in call.kwargs.items():
                seen.append(f"{key}={value!r}")
    assert seen, "the burst made no requests — this test would pass vacuously"
    _assert_label_absent(*seen)


def test_the_label_does_survive_locally_so_the_person_can_read_their_receipt():
    """The counterpart: withholding it from the cloud is not losing it.

    Without this, :func:`_assert_label_absent` would still pass if the label
    were dropped on the floor everywhere, and a receipt nobody can identify is
    not a receipt.
    """
    provider = MockBurstProvider()
    receipt = run_burst(
        BurstRequest(
            label=_REVEALING_LABEL,
            accelerator_id="nvidia-t4",
            chip_count=1,
            usd_per_hour=0.35,
        ),
        provider,
        record=False,
    )
    assert receipt.label == _REVEALING_LABEL
    assert receipt.as_dict()["workload"] == _REVEALING_LABEL


# --------------------------------------------------------------------------
# What the decide tool tells the person about their machine
# --------------------------------------------------------------------------


def _stressed_device(**overrides):
    """A machine that is unplugged *and* running hot, with room for the job."""
    from hermes_cli.hussh_one_burst.telemetry import MeasuredDevice

    base = dict(
        label="Laptop On A Sofa",
        cpu_cores=8,
        cpu_load_pct=20.0,
        ram_total_gb=64.0,
        ram_available_gb=48.0,
        disk_free_gb=500.0,
        online=True,
        on_ac_power=False,  # -> on_battery
        max_temp_c=95.0,  # -> throttled
    )
    base.update(overrides)
    return MeasuredDevice(**base)


def _decide(device, **args):
    """Call hussh_burst_decide against a fixed, fake machine."""
    import asyncio
    from unittest.mock import patch

    import hermes_cli.hussh_one_burst.mcp_server as srv_mod

    with patch.object(srv_mod, "measure_device", return_value=device) as probe:
        result = asyncio.run(
            _run_tool().call_tool("hussh_burst_decide", {"vram_gb": 1.0, **args})
        )
    payload = result[1] if isinstance(result, tuple) else result
    return payload, probe


def test_a_battery_warning_is_not_erased_by_a_thermal_one():
    """Both were written to a single `advisory` slot, so the second won.

    A person on battery who is also thermally throttled got told about the heat
    and never about the drain — and the drain is the one that ends the run.
    """
    payload, _probe = _decide(_stressed_device())

    assert payload["target"] == "device", "the fake machine must fit the job"
    advisories = " ".join(payload["advisories"])
    assert "battery" in advisories
    assert "throttled" in advisories
    assert len(payload["advisories"]) == 2


def test_no_advisories_key_when_the_machine_is_healthy():
    payload, _probe = _decide(_stressed_device(on_ac_power=True, max_temp_c=55.0))
    assert "advisories" not in payload


def test_advisories_are_withheld_when_the_answer_is_the_cloud():
    """Neither warning is about a cloud run, so neither belongs on one."""
    payload, _probe = _decide(_stressed_device(), vram_gb=5000.0)
    assert payload["target"] == "cloud"
    assert "advisories" not in payload


def test_the_machine_shown_is_the_machine_decided_from():
    """It was measured twice: once to decide, once to display.

    Two probes are two different moments, so `measured_device` could disagree
    with the `reason` printed beside it — evidence that did not produce the
    conclusion it is offered as.
    """
    payload, probe = _decide(_stressed_device())
    assert probe.call_count == 1, "one decision, one measurement"
    assert payload["measured_device"]["label"] == "Laptop On A Sofa"


# --------------------------------------------------------------------------
# Pre-flight: refuse before the money, on evidence
# --------------------------------------------------------------------------


def _preflight_provider(*, zone_status=200, quotas=None):
    """A GcpBurstProvider whose project answers a scripted zone + quota check."""
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    session = MagicMock()
    session.get.side_effect = [
        MagicMock(status_code=zone_status),
        MagicMock(ok=True, json=lambda: {"quotas": quotas or []}),
    ]
    ref = MagicMock(project="p", region="us-central1")
    return provider, patch.object(provider, "_authed_session", return_value=(session, ref))


def test_a_part_the_zone_does_not_carry_is_a_blocker():
    """Verified live: `us-central1-a` carried GB200 and H100 and neither H200
    nor B200, while the recommender quotes both at $88 and $110 an hour."""
    provider, ctx = _preflight_provider(zone_status=404)
    with ctx:
        result = provider.preflight("b200-180", 8)
    assert result.ok is False
    assert "nvidia-b200" in " ".join(result.blockers)
    assert "us-central1-a" in " ".join(result.blockers)


def test_a_quota_below_the_order_is_a_blocker():
    """`hushh-pda-dev` publishes a spot A100-80GB limit of 0 and quotes it anyway."""
    provider, ctx = _preflight_provider(
        quotas=[{"metric": "PREEMPTIBLE_NVIDIA_A100_80GB_GPUS", "limit": 0}]
    )
    with ctx:
        result = provider.preflight("a100-80", 2)
    assert result.ok is False
    assert "quota" in " ".join(result.blockers).lower()


def test_a_quota_that_covers_the_order_passes():
    provider, ctx = _preflight_provider(
        quotas=[{"metric": "PREEMPTIBLE_NVIDIA_T4_GPUS", "limit": 4}]
    )
    with ctx:
        result = provider.preflight("nvidia-t4", 1)
    assert result.ok is True
    assert not result.warnings


def test_an_unpublished_quota_warns_rather_than_refusing():
    """Compute v1 publishes no metric for H100 and newer — verified across five
    regions. Refusing on an absent reading is a confident guess pointed the
    other way, so it warns and lets the person decide."""
    provider, ctx = _preflight_provider()
    with ctx:
        result = provider.preflight("h100-80", 8)
    assert result.ok is True
    assert any("no spot-quota figure" in w for w in result.warnings)


def test_an_unreachable_project_warns_rather_than_blocking_a_valid_burst():
    """A pre-flight that failed closed on a network hiccup would block a burst
    the person could have run."""
    from unittest.mock import patch

    provider = GcpBurstProvider(project="p", region="us-central1")
    with patch.object(provider, "_authed_session", side_effect=RuntimeError("no route")):
        result = provider.preflight("nvidia-t4", 1)
    assert result.ok is True
    assert any("no route" in w for w in result.warnings)


def test_every_catalog_part_has_a_zone_name_and_a_known_quota_stance():
    """The two lookup tables must cover the catalog, or the pre-flight is blind."""
    from hermes_cli.hussh_one_burst.hardware import ACCEL_CATALOG
    from hermes_cli.hussh_one_burst.providers import _GCP_SHAPES

    for accel in ACCEL_CATALOG:
        shape = _GCP_SHAPES.get(accel.id)
        if shape is None:
            continue  # TPUs are refused by resolve_shape, not pre-flighted
        assert shape.get("zone_accelerator"), f"{accel.id} has no zone name"
        # `quota_metric` may be absent — that is the documented "cannot tell"
        # case — but it must never be an empty string pretending to be one.
        assert shape.get("quota_metric", "x") != ""


def test_the_run_tool_refuses_an_unprovisionable_burst_before_asking_for_money():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import hermes_cli.hussh_one_burst.providers as prov_mod
    from hermes_cli.hussh_one_burst.providers import Preflight

    ctx = MagicMock()
    ctx.elicit = AsyncMock()
    blocked = Preflight(blockers=("nvidia-b200 is not offered in us-central1-a.",))
    with patch.object(prov_mod.GcpBurstProvider, "preflight", return_value=blocked):
        result = asyncio.run(
            _run_tool().call_tool(
                "hussh_burst_run",
                {"preset_id": "finetune-70b", "provider": "gcp", "project": "p"},
            )
        )
    payload = result[1] if isinstance(result, tuple) else result
    assert payload["success"] is False
    assert payload["status"] == "not_provisionable"
    ctx.elicit.assert_not_called()


# --------------------------------------------------------------------------
# A create that failed is not proof that nothing was created
# --------------------------------------------------------------------------


def _provision_with_dropped_post(get_statuses):
    """A provider whose create raises after the request may already have landed."""
    from unittest.mock import MagicMock, patch

    provider = GcpBurstProvider(
        project="p", region="us-central1", sleep=lambda _s: None,
        teardown_confirm_seconds=5.0,
    )
    session = MagicMock()
    session.post.side_effect = OSError("connection reset by peer")
    session.get.side_effect = [MagicMock(status_code=c) for c in get_statuses]
    session.delete.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")
    return provider, session, patch.object(
        provider, "_authed_session", return_value=(session, ref)
    )


def test_a_dropped_create_that_left_nothing_behind_is_just_a_failure():
    provider, session, ctx = _provision_with_dropped_post([404])
    with ctx, pytest.raises(RuntimeError, match="Could not reach Compute Engine"):
        provider.provision(InstanceSpec("nvidia-t4", 1, "job", 5.0))
    session.delete.assert_not_called()


def test_a_dropped_create_that_did_land_is_swept():
    """The name is chosen before the request, which is what makes this fixable."""
    # GET: exists -> then teardown's confirm loop sees it gone.
    provider, session, ctx = _provision_with_dropped_post([200, 404])
    with ctx, pytest.raises(RuntimeError, match="Could not reach Compute Engine"):
        provider.provision(InstanceSpec("nvidia-t4", 1, "job", 5.0))
    session.delete.assert_called_once()


def test_an_orphan_that_cannot_be_confirmed_gone_is_named_on_the_receipt():
    """`provision_failed` used to mean "nothing to release" unconditionally.

    A POST that times out says nothing about whether Compute Engine accepted the
    create. If one is there and cannot be confirmed released, the receipt has to
    carry the id — that is the difference between an incident someone can act on
    and a bill nobody can trace.
    """
    from unittest.mock import MagicMock, patch

    ticks = iter([0.0, 0.0, 999.0])
    provider = GcpBurstProvider(
        project="p", region="us-central1", sleep=lambda _s: None,
        teardown_confirm_seconds=1.0, clock=lambda: next(ticks),
    )
    session = MagicMock()
    session.post.side_effect = OSError("connection reset by peer")
    session.get.return_value = MagicMock(status_code=200)  # never disappears
    session.delete.return_value = MagicMock(status_code=200)
    ref = MagicMock(project="p", region="us-central1")

    with patch.object(provider, "_authed_session", return_value=(session, ref)):
        receipt = run_burst(
            BurstRequest(
                label="job", accelerator_id="nvidia-t4", chip_count=1,
                usd_per_hour=0.35, deadline_minutes=5.0,
            ),
            provider,
            record=False,
        )

    assert receipt.status == "provision_failed"
    assert receipt.instance_id and receipt.instance_id.startswith("hussh-burst-nvidia-t4-")
    assert receipt.leaked_instance is True
    assert "ORPHAN" in " ".join(receipt.events)
    assert "may still be running and billing" in receipt.as_dict()["warning"]


def test_the_mock_path_runs_with_every_credential_stripped_from_the_environment(monkeypatch):
    """"Credential-free" has to be checked without credentials in the room.

    This container has `GCP_DEPLOY_SA_KEY_B64` set, so a mock-path test can pass
    while quietly depending on it. Strip every variable the broker consults, and
    every variable Google's own ADC discovery consults, and run the full
    lifecycle: it must still complete and release.
    """
    for var in (
        "GCP_DEPLOY_SA_KEY_B64",
        "HUSSH_BURST_SA_KEY_B64",
        "GCP_DEPLOY_REF",
        "GCP_DEPLOY_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    provider = resolve_provider("mock")
    receipt = run_burst(
        BurstRequest(
            label="no-creds", accelerator_id="nvidia-t4", chip_count=1, usd_per_hour=0.35
        ),
        provider,
        record=False,
    )
    assert receipt.status == "completed"
    assert receipt.leaked_instance is False
    assert receipt.credential is None, "the mock must not resolve a credential at all"
    assert provider.live_instances == []


def test_plan_stays_credential_free_unless_a_project_is_named():
    """A quote must not need a cloud account to produce.

    The whole point of deciding locally is that nothing has to leave the device
    to get an answer. `plan` gains a pre-flight only when someone names a real
    project — the default `mock` provider has no `preflight` at all, so the code
    path is absent rather than merely skipped.
    """
    import asyncio

    result = asyncio.run(_run_tool().call_tool("hussh_burst_plan", {"preset_id": "finetune-70b"}))
    payload = result[1] if isinstance(result, tuple) else result
    assert payload["recommended"]["count"] >= 1
    assert "provisionable" not in payload


def test_plan_says_up_front_when_the_named_project_cannot_get_the_part():
    """Otherwise a person reads a $110/hour quote that `run` will then refuse."""
    import asyncio
    from unittest.mock import patch

    import hermes_cli.hussh_one_burst.providers as prov_mod
    from hermes_cli.hussh_one_burst.providers import Preflight

    blocked = Preflight(blockers=("nvidia-b200 is not offered in us-central1-a.",))
    with patch.object(prov_mod.GcpBurstProvider, "preflight", return_value=blocked):
        result = asyncio.run(
            _run_tool().call_tool(
                "hussh_burst_plan",
                {"preset_id": "finetune-70b", "provider": "gcp", "project": "p"},
            )
        )
    payload = result[1] if isinstance(result, tuple) else result
    assert payload["provisionable"]["ok"] is False
    assert payload["provisionable"]["blockers"]
