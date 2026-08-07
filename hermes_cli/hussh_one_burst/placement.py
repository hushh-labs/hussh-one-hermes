# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The placement engine (pure, no I/O).

Decides whether a workload runs on the person's own machine or bursts to their
own cloud.  This is the whole privacy argument in one function: the decision is
made from resource *numbers*, never from the workload's contents, so it can run
entirely locally and nothing about the job needs to leave the device.
"""

from __future__ import annotations

from .types import AcceleratorKind, DeviceProfile, Headroom, PlacementDecision, WorkloadEstimate

#: Never pack the device to 100% — leave headroom for the OS, the agent, and
#: whatever the person is doing in the foreground.
SAFETY = 0.8


def _round1(value: float) -> float:
    return round(value * 10) / 10


def decide_placement(
    estimate: WorkloadEstimate,
    device: DeviceProfile,
    accelerator_kind: AcceleratorKind = "gpu",
) -> PlacementDecision:
    """Route a workload to the device when it comfortably fits, else to the cloud.

    Rules, first match wins:

    * device offline → cloud;
    * TPU workloads → cloud (no consumer silicon has a TPU);
    * unknown size → cloud (never gamble the person's machine on a guess);
    * GPU job fitting under the safety fraction of memory *and* disk → device;
    * otherwise → cloud, naming the binding constraint.
    """
    if not device.online:
        return PlacementDecision(
            target="cloud",
            reason="Your device is offline — bursting to the cloud.",
            fits_locally=False,
        )

    if accelerator_kind == "tpu":
        return PlacementDecision(
            target="cloud",
            reason="TPU workloads run only in the cloud — bursting.",
            fits_locally=False,
        )

    if estimate.vram_gb <= 0 and estimate.unified_memory_gb <= 0:
        return PlacementDecision(
            target="cloud",
            reason="Workload size is unknown — bursting to the cloud.",
            fits_locally=False,
        )

    mem_budget_gb = device.unified_memory_gb * SAFETY
    disk_budget_gb = device.disk_free_gb * SAFETY
    # On Apple Silicon the GPU and the host share one unified pool, so the
    # binding requirement is the larger of the two against the same budget.
    mem_need_gb = max(estimate.vram_gb, estimate.unified_memory_gb)

    memory_headroom_gb = mem_budget_gb - mem_need_gb
    disk_headroom_gb = disk_budget_gb - estimate.disk_gb
    headroom = Headroom(memory_gb=_round1(memory_headroom_gb), disk_gb=_round1(disk_headroom_gb))

    if memory_headroom_gb < 0:
        return PlacementDecision(
            target="cloud",
            reason=(
                f"Needs ~{estimate.vram_gb:g}GB accelerator memory; {device.label} offers "
                f"~{_round1(mem_budget_gb):g}GB usable — bursting to the cloud."
            ),
            fits_locally=False,
            headroom=headroom,
        )

    if disk_headroom_gb < 0:
        return PlacementDecision(
            target="cloud",
            reason=(
                f"Needs ~{estimate.disk_gb:g}GB disk; {device.label} has "
                f"~{_round1(disk_budget_gb):g}GB usable — bursting to the cloud."
            ),
            fits_locally=False,
            headroom=headroom,
        )

    return PlacementDecision(
        target="device",
        reason=(
            f"Fits on {device.label} with ~{headroom.memory_gb:g}GB memory headroom "
            "— running on-device."
        ),
        fits_locally=True,
        headroom=headroom,
    )
