# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for live device measurement and the discrete-accelerator placement path.

These cover the boundary the pure engine deliberately does not: reading a real
machine, degrading when a probe is absent, and the two-pool memory model that a
workstation with a discrete card needs.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli.hussh_one_burst import decide_placement
from hermes_cli.hussh_one_burst.telemetry import (
    GpuInfo,
    MeasuredDevice,
    _run,
    measure_device,
)
from hermes_cli.hussh_one_burst.types import DeviceProfile, WorkloadEstimate


def _measured(**overrides) -> MeasuredDevice:
    base = dict(
        label="Test Machine",
        cpu_cores=8,
        cpu_load_pct=10.0,
        ram_total_gb=64.0,
        ram_available_gb=48.0,
        disk_free_gb=500.0,
        online=True,
    )
    base.update(overrides)
    return MeasuredDevice(**base)


# --------------------------------------------------------------------------
# The two-pool memory model
# --------------------------------------------------------------------------


def test_discrete_card_binds_on_vram_not_host_ram():
    """A workstation with plenty of RAM and a small card must still burst.

    This is the case the unified-memory model gets wrong: 96GB of host RAM says
    "fits", but a 24GB model does not go onto an 8GB card.
    """
    device = DeviceProfile(
        id="ws",
        label="Workstation",
        cpu_cores=16,
        gpu_cores=0,
        unified_memory_gb=96.0,
        disk_free_gb=1000.0,
        network_mbps=1000.0,
        vram_gb=8.0,
    )
    estimate = WorkloadEstimate(
        vram_gb=24.0, unified_memory_gb=8.0, vcpus=8, disk_gb=50.0, estimated_minutes=30
    )
    decision = decide_placement(estimate, device)
    assert decision.target == "cloud"
    assert not decision.fits_locally


def test_discrete_card_runs_locally_when_both_pools_fit():
    device = DeviceProfile(
        id="ws",
        label="Workstation",
        cpu_cores=16,
        gpu_cores=0,
        unified_memory_gb=96.0,
        disk_free_gb=1000.0,
        network_mbps=1000.0,
        vram_gb=48.0,
    )
    estimate = WorkloadEstimate(
        vram_gb=24.0, unified_memory_gb=8.0, vcpus=8, disk_gb=50.0, estimated_minutes=30
    )
    decision = decide_placement(estimate, device)
    assert decision.target == "device"
    assert decision.fits_locally


def test_discrete_card_binds_on_host_ram_when_that_is_tighter():
    """Both pools are checked — the host side can bind just as well."""
    device = DeviceProfile(
        id="ws",
        label="Workstation",
        cpu_cores=16,
        gpu_cores=0,
        unified_memory_gb=8.0,
        disk_free_gb=1000.0,
        network_mbps=1000.0,
        vram_gb=80.0,
    )
    estimate = WorkloadEstimate(
        vram_gb=8.0, unified_memory_gb=32.0, vcpus=8, disk_gb=50.0, estimated_minutes=30
    )
    assert decide_placement(estimate, device).target == "cloud"


def test_unified_memory_path_is_unchanged_by_the_discrete_branch():
    """``vram_gb=None`` must behave exactly as before — one shared pool."""
    device = DeviceProfile(
        id="mac",
        label="Mac Studio",
        cpu_cores=24,
        gpu_cores=60,
        unified_memory_gb=64.0,
        disk_free_gb=1000.0,
        network_mbps=1000.0,
    )
    assert device.vram_gb is None
    fits = WorkloadEstimate(
        vram_gb=40.0, unified_memory_gb=16.0, vcpus=8, disk_gb=50.0, estimated_minutes=30
    )
    assert decide_placement(fits, device).target == "device"
    too_big = WorkloadEstimate(
        vram_gb=60.0, unified_memory_gb=16.0, vcpus=8, disk_gb=50.0, estimated_minutes=30
    )
    assert decide_placement(too_big, device).target == "cloud"


def test_the_reason_names_the_quantity_that_actually_bound():
    """A message must not read as a contradiction.

    Found by running the real tool: a job needing 12GB VRAM and 16GB host RAM on
    a 12.1GB machine was refused with "needs ~12GB, offers ~12.1GB usable" —
    naming the accelerator figure when host memory was what blocked it.
    """
    device = DeviceProfile(
        id="small",
        label="Small Laptop",
        cpu_cores=4,
        gpu_cores=0,
        unified_memory_gb=15.0,
        disk_free_gb=200.0,
        network_mbps=100.0,
    )
    estimate = WorkloadEstimate(
        vram_gb=12.0, unified_memory_gb=16.0, vcpus=4, disk_gb=10.0, estimated_minutes=10
    )
    decision = decide_placement(estimate, device)
    assert decision.target == "cloud"
    assert "16GB" in decision.reason
    assert "12GB memory" not in decision.reason


def test_the_reason_still_says_accelerator_when_vram_is_what_bound():
    device = DeviceProfile(
        id="small",
        label="Small Laptop",
        cpu_cores=4,
        gpu_cores=0,
        unified_memory_gb=15.0,
        disk_free_gb=200.0,
        network_mbps=100.0,
    )
    estimate = WorkloadEstimate(
        vram_gb=40.0, unified_memory_gb=4.0, vcpus=4, disk_gb=10.0, estimated_minutes=10
    )
    decision = decide_placement(estimate, device)
    assert "accelerator memory" in decision.reason
    assert "40GB" in decision.reason


# --------------------------------------------------------------------------
# Measurement → profile conversion
# --------------------------------------------------------------------------


def test_profile_uses_available_ram_not_total():
    """Placement asks whether it fits *now*; open browser tabs are not headroom."""
    profile = _measured(ram_total_gb=64.0, ram_available_gb=12.0).to_profile()
    assert profile.unified_memory_gb == 12.0


def test_unified_gpu_leaves_vram_unset():
    gpu = GpuInfo(name="Apple M3 Max", vram_gb=64.0, memory_model="unified", cores=40)
    profile = _measured(gpu=gpu).to_profile()
    assert profile.vram_gb is None
    assert profile.gpu_cores == 40


def test_discrete_gpu_carries_vram_through():
    gpu = GpuInfo(name="RTX 4090", vram_gb=24.0, memory_model="discrete")
    assert _measured(gpu=gpu).to_profile().vram_gb == 24.0


def test_offline_measurement_reaches_the_engine():
    profile = _measured(online=False).to_profile()
    estimate = WorkloadEstimate(
        vram_gb=1.0, unified_memory_gb=1.0, vcpus=1, disk_gb=1.0, estimated_minutes=1
    )
    assert decide_placement(estimate, profile).target == "cloud"


def test_a_machine_that_measures_as_nothing_bursts():
    """Total probe failure must fail toward the cloud, never onto the device."""
    profile = _measured(ram_available_gb=0.0, disk_free_gb=0.0).to_profile()
    estimate = WorkloadEstimate(
        vram_gb=1.0, unified_memory_gb=1.0, vcpus=1, disk_gb=1.0, estimated_minutes=1
    )
    assert decide_placement(estimate, profile).target == "cloud"


# --------------------------------------------------------------------------
# Derived signals
# --------------------------------------------------------------------------


def test_throttled_is_none_when_nothing_could_be_read():
    """Unknown is not healthy — a caller must be able to tell them apart."""
    assert _measured().throttled is None


def test_throttled_true_on_low_clock_or_high_temp():
    assert _measured(cpu_freq_ratio=0.4).throttled is True
    assert _measured(max_temp_c=95.0).throttled is True


def test_throttled_false_when_readings_are_healthy():
    assert _measured(cpu_freq_ratio=0.98, max_temp_c=55.0).throttled is False


def test_on_battery_only_when_positively_known():
    assert _measured(on_ac_power=None).on_battery is False
    assert _measured(on_ac_power=True).on_battery is False
    assert _measured(on_ac_power=False).on_battery is True


# --------------------------------------------------------------------------
# Probe robustness — a missing tool is a normal machine, not an error
# --------------------------------------------------------------------------


def test_run_returns_none_for_a_binary_that_does_not_exist():
    assert _run(["definitely-not-a-real-binary-9c3f"]) is None


def test_run_returns_none_on_empty_argv():
    assert _run([]) is None


def test_run_returns_none_on_timeout(monkeypatch):
    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("hermes_cli.hussh_one_burst.telemetry.shutil.which", lambda _: "/bin/x")
    monkeypatch.setattr("hermes_cli.hussh_one_burst.telemetry.subprocess.run", _boom)
    assert _run(["x"]) is None


def test_run_returns_none_on_nonzero_exit(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = "ignored"

    monkeypatch.setattr("hermes_cli.hussh_one_burst.telemetry.shutil.which", lambda _: "/bin/x")
    monkeypatch.setattr(
        "hermes_cli.hussh_one_burst.telemetry.subprocess.run", lambda *a, **k: _Proc()
    )
    assert _run(["x"]) is None


def test_measure_device_never_raises_and_is_self_consistent():
    device = measure_device()
    assert device.label
    assert device.cpu_cores >= 0
    assert device.ram_total_gb >= 0
    assert device.ram_available_gb <= device.ram_total_gb + 0.01
    assert isinstance(device.online, bool)
    # Whatever it measured must survive conversion into the pure engine.
    decide_placement(
        WorkloadEstimate(
            vram_gb=1.0, unified_memory_gb=1.0, vcpus=1, disk_gb=1.0, estimated_minutes=1
        ),
        device.to_profile(),
    )


@pytest.mark.parametrize("path", ["/", "/definitely/not/a/path/9c3f"])
def test_measure_device_survives_a_bad_disk_path(path):
    assert measure_device(path).disk_free_gb >= 0.0


# --------------------------------------------------------------------------
# CPU load: unknown is not idle
# --------------------------------------------------------------------------


def test_the_first_cpu_sample_reports_unknown_rather_than_zero():
    """`cpu_percent(interval=None)` has no baseline on its first call.

    Verified against psutil 7.2.2: a process pegged at 100% reports `0.0` first
    and `100.0` second. In a long-lived MCP server the first call is the one a
    person is most likely to see, so a fabricated `0.0` would show a busy
    machine as idle. `throttled` already draws this distinction — unknown is
    not healthy — and this now draws it too.
    """
    import hermes_cli.hussh_one_burst.telemetry as tel

    class _Psutil:
        def __init__(self):
            self.calls = 0

        def cpu_percent(self, interval=None):
            self.calls += 1
            return 0.0 if self.calls == 1 else 87.5

    fake = _Psutil()
    tel._cpu_primed = False
    try:
        assert tel._sample_cpu_load(fake) is None, "the baseline call is not a reading"
        assert tel._sample_cpu_load(fake) == 87.5
        assert fake.calls == 2, "the baseline still has to be taken, not skipped"
    finally:
        tel._cpu_primed = False


def test_an_unreadable_cpu_sample_is_none_and_does_not_prime():
    """A probe that raised took no baseline, so the next call is still the first."""
    import hermes_cli.hussh_one_burst.telemetry as tel

    class _Broken:
        def cpu_percent(self, interval=None):
            raise OSError("no /proc")

    tel._cpu_primed = False
    try:
        assert tel._sample_cpu_load(_Broken()) is None
        assert tel._cpu_primed is False
    finally:
        tel._cpu_primed = False


def test_a_real_measurement_never_reports_a_load_it_did_not_take():
    """End to end on this machine: whatever comes back is None or plausible."""
    import hermes_cli.hussh_one_burst.telemetry as tel

    tel._cpu_primed = False
    try:
        first = measure_device().cpu_load_pct
        assert first is None, "the first measurement in a process cannot know the load"
        second = measure_device().cpu_load_pct
        assert second is None or 0.0 <= second <= 100.0
    finally:
        tel._cpu_primed = False
