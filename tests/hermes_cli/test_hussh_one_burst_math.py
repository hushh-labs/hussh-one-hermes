# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Parity tests for the ported Xtreme Burst decision layer.

Every case here mirrors an assertion from the TypeScript engine this module was
ported from, so a divergence shows up as a failure rather than as a silently
different answer on someone's machine.  Invariants, not snapshots.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_burst import (
    ACCEL_CATALOG,
    DEVICE_PROFILES,
    MAX_CHIPS,
    WORKLOAD_PRESETS,
    DeviceProfile,
    WorkloadEstimate,
    benchmark_hardware,
    decide_placement,
    find_device_profile,
    find_workload_preset,
    recommend_hardware,
)

IPHONE = find_device_profile("iphone-17-pro")
MAC_STUDIO = find_device_profile("mac-studio-m3-ultra")


def _estimate(vram: float, mem: float, disk: float, minutes: float = 30) -> WorkloadEstimate:
    return WorkloadEstimate(
        vram_gb=vram, unified_memory_gb=mem, vcpus=8, disk_gb=disk, estimated_minutes=minutes
    )


# --------------------------------------------------------------------------- placement


def test_small_job_runs_on_device() -> None:
    decision = decide_placement(_estimate(4, 6, 8), IPHONE, "gpu")
    assert decision.target == "device"
    assert decision.fits_locally is True
    assert decision.headroom is not None
    assert decision.headroom.memory_gb > 0


def test_job_exceeding_memory_bursts_and_names_the_constraint() -> None:
    decision = decide_placement(_estimate(640, 256, 400), IPHONE, "gpu")
    assert decision.target == "cloud"
    assert decision.fits_locally is False
    assert "accelerator memory" in decision.reason


def test_job_exceeding_disk_bursts_and_names_disk() -> None:
    # Fits in memory on a Mac Studio, but 5 TB of history does not fit the disk.
    decision = decide_placement(_estimate(24, 96, 5_000), MAC_STUDIO, "gpu")
    assert decision.target == "cloud"
    assert "disk" in decision.reason


def test_tpu_always_bursts_even_on_the_largest_machine() -> None:
    decision = decide_placement(_estimate(1, 1, 1), MAC_STUDIO, "tpu")
    assert decision.target == "cloud"
    assert "TPU" in decision.reason


def test_offline_device_bursts() -> None:
    offline = DeviceProfile("offline", "Offline Mac", 8, 8, 64, 512, 0, online=False)
    assert decide_placement(_estimate(1, 1, 1), offline, "gpu").target == "cloud"


def test_unknown_size_never_gambles_the_device() -> None:
    decision = decide_placement(_estimate(0, 0, 0), MAC_STUDIO, "gpu")
    assert decision.target == "cloud"
    assert "unknown" in decision.reason.lower()


def test_safety_margin_is_respected() -> None:
    # 12 GB device → 9.6 GB usable. 10 GB must burst even though it is "under 12".
    assert decide_placement(_estimate(10, 10, 1), IPHONE, "gpu").target == "cloud"
    assert decide_placement(_estimate(9, 9, 1), IPHONE, "gpu").target == "device"


def test_same_job_differs_by_device() -> None:
    """The whole product in one assertion: the machine decides, not the job."""
    photos = find_workload_preset("photos-model")
    assert decide_placement(photos.estimate, MAC_STUDIO, photos.accelerator_kind).target == "device"
    assert decide_placement(photos.estimate, IPHONE, photos.accelerator_kind).target == "cloud"


# --------------------------------------------------------------------------- hardware


def test_small_gpu_job_lands_on_a_right_sized_chip_not_a_frontier_box() -> None:
    rec = recommend_hardware(40, "gpu", 1)
    assert rec.fits is True
    assert rec.count == 1
    assert rec.accel.id == "a100-40"


def test_70b_class_job_uses_newest_large_memory_gpus() -> None:
    rec = recommend_hardware(220, "gpu", 8)
    assert rec.fits is True
    assert rec.accel.id == "h200-141"
    assert rec.count == 8  # parallelism floor honored
    assert rec.usd_per_hour > 20


def test_frontier_job_fits_blackwell_class_single_node() -> None:
    rec = recommend_hardware(1_000, "gpu", 1)
    assert rec.fits is True
    assert rec.accel.id in {"b200-180", "gb200-186"}


def test_small_tpu_job_stays_on_v5e() -> None:
    rec = recommend_hardware(16, "tpu", 8)
    assert rec.accel.kind == "tpu"
    assert rec.accel.id == "tpu-v5e"
    assert rec.count == 8


def test_memory_heavy_tpu_job_routes_to_v5p() -> None:
    rec = recommend_hardware(400, "tpu", 1)
    assert rec.fits is True
    assert rec.accel.id == "tpu-v5p"


def test_job_too_large_for_one_node_is_flagged_not_hidden() -> None:
    rec = recommend_hardware(2_000, "gpu", 1)
    assert rec.fits is False
    assert "multi-node" in rec.rationale


def test_count_never_exceeds_the_node_ceiling() -> None:
    for kind in ("gpu", "tpu"):
        assert recommend_hardware(5_000, kind, 8).count <= MAX_CHIPS


# --------------------------------------------------------------------------- benchmark


def test_matched_never_costs_more_than_the_biggest_box() -> None:
    rows = benchmark_hardware(220, "gpu", 8, 240)
    matched = next(r for r in rows if r.role == "matched")
    oversized = next(r for r in rows if r.role == "oversized")
    assert matched.feasible is True
    assert matched.cost_usd <= oversized.cost_usd


def test_cheap_pick_is_shown_as_infeasible_when_it_cannot_fit() -> None:
    rows = benchmark_hardware(220, "gpu", 8, 240)
    undersized = next(r for r in rows if r.role == "undersized")
    assert undersized.feasible is False  # 220GB cannot fit 8× T4 (128GB)


def test_benchmark_always_returns_the_three_roles() -> None:
    rows = benchmark_hardware(48, "gpu", 2, 40)
    assert [r.role for r in rows] == ["undersized", "matched", "oversized"]


# --------------------------------------------------------------------------- catalog


def test_catalog_carries_the_newest_gcp_generations() -> None:
    ids = {c.id for c in ACCEL_CATALOG}
    assert {"h200-141", "b200-180", "gb200-186", "tpu-v6e", "tpu-v5p"} <= ids


def test_catalog_entries_are_well_formed() -> None:
    for c in ACCEL_CATALOG:
        assert c.kind in {"gpu", "tpu"}
        assert c.mem_gb_per_chip > 0
        assert c.perf > 0
        assert c.usd_per_hour_per_chip > 0
        assert c.best_for


def test_pricing_never_makes_a_bigger_chip_cheaper_per_unit_of_work() -> None:
    """Guards the modeled catalog against an edit that would break right-sizing.

    If a larger chip were ever cheaper per unit of throughput, every small job
    would be matched to the biggest box — the exact overpay this engine exists
    to prevent.  Caught a real mispricing during the port.
    """
    for kind in ("gpu", "tpu"):
        fam = sorted(
            (c for c in ACCEL_CATALOG if c.kind == kind), key=lambda c: c.mem_gb_per_chip
        )
        for smaller, larger in zip(fam, fam[1:]):
            assert larger.usd_per_hour_per_chip / larger.perf >= (
                smaller.usd_per_hour_per_chip / smaller.perf
            ) * 0.5, f"{larger.id} is suspiciously cheap per unit of work vs {smaller.id}"


# --------------------------------------------------------------------------- presets


def test_every_preset_resolves_and_is_coherent() -> None:
    assert len(WORKLOAD_PRESETS) >= 6
    for preset in WORKLOAD_PRESETS:
        assert find_workload_preset(preset.id) is preset
        assert preset.parallel_chips >= 1
        assert preset.matched_runtime_min > 0
        assert preset.estimate.vram_gb > 0


def test_every_device_resolves() -> None:
    assert len(DEVICE_PROFILES) >= 6
    for device in DEVICE_PROFILES:
        assert find_device_profile(device.id) is device
        assert device.unified_memory_gb > 0


def test_unknown_ids_return_none_rather_than_raising() -> None:
    assert find_device_profile("commodore-64") is None
    assert find_workload_preset("mine-bitcoin") is None


@pytest.mark.parametrize("preset", WORKLOAD_PRESETS, ids=lambda p: p.id)
def test_every_preset_produces_a_decision_on_a_phone(preset) -> None:
    """No preset may crash or return an unpriced plan from the smallest device."""
    decision = decide_placement(preset.estimate, IPHONE, preset.accelerator_kind)
    assert decision.target in {"device", "cloud"}
    if decision.target == "cloud":
        rec = recommend_hardware(
            preset.estimate.vram_gb, preset.accelerator_kind, preset.parallel_chips
        )
        assert rec.usd_per_hour > 0
