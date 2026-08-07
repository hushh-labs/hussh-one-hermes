# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Xtreme Burst Compute — the 🤫 One burst-orchestration capability.

Hermes is the local compute and burst-orchestration layer.  The placement
decision runs here, on the person's own machine, from resource numbers alone —
so nothing about a workload has to leave the device to decide where it should
run.  When a job outgrows the machine, the accelerator is provisioned in the
*person's own* cloud, and the result comes home.

This module is Phase 1: the pure decision layer.  Execution, the consent and
credential broker, and receipt sealing land in later phases and stay behind the
same overlay boundary, per ``docs/hussh-one/architecture``.
"""

from __future__ import annotations

from .devices import (
    DEVICE_PROFILES,
    WORKLOAD_PRESETS,
    find_device_profile,
    find_workload_preset,
)
from .hardware import ACCEL_CATALOG, MAX_CHIPS, benchmark_hardware, recommend_hardware
from .placement import SAFETY, decide_placement
from .types import (
    AcceleratorClass,
    AcceleratorKind,
    BenchmarkRow,
    DeviceProfile,
    Headroom,
    HardwareRecommendation,
    PlacementDecision,
    PlacementTarget,
    WorkloadEstimate,
    WorkloadPreset,
)

__all__ = [
    "ACCEL_CATALOG",
    "DEVICE_PROFILES",
    "MAX_CHIPS",
    "SAFETY",
    "WORKLOAD_PRESETS",
    "AcceleratorClass",
    "AcceleratorKind",
    "BenchmarkRow",
    "DeviceProfile",
    "HardwareRecommendation",
    "Headroom",
    "PlacementDecision",
    "PlacementTarget",
    "WorkloadEstimate",
    "WorkloadPreset",
    "benchmark_hardware",
    "decide_placement",
    "find_device_profile",
    "find_workload_preset",
    "recommend_hardware",
]
