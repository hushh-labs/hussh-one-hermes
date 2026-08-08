# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Xtreme Burst Compute — the 🤫 One burst-orchestration capability.

Hermes is the local compute and burst-orchestration layer.  The placement
decision runs here, on the person's own machine, from resource numbers alone —
so nothing about a workload has to leave the device to decide where it should
run.  When a job outgrows the machine, the accelerator is provisioned in the
*person's own* cloud, and the result comes home.

Layering, and the one rule that holds it together:

* ``types`` / ``placement`` / ``hardware`` / ``devices`` — **pure**.  No network,
  no credential, no clock.  This is what lets the decision run locally.
* ``telemetry`` — the *only* module that reads the real machine.
* ``credentials`` / ``providers`` / ``execution`` — the cloud side, reached only
  after a person approves.
* ``mcp_server`` — how the capability becomes reachable.  Not imported here, so
  the package stays usable without the MCP extra.

Per ``docs/hussh-one/architecture/xtreme-burst.md``, payload transfer to a burst
instance is deliberately not implemented: shipping a workload off the device is
the step that actually moves someone's information, and it needs its own consent
design rather than arriving as a side effect of provisioning.
"""

from __future__ import annotations

from .credentials import CredentialError, CredentialRef, resolve_credentials
from .devices import (
    DEVICE_PROFILES,
    WORKLOAD_PRESETS,
    find_device_profile,
    find_workload_preset,
)
from .execution import BurstReceipt, BurstRequest, run_burst
from .hardware import ACCEL_CATALOG, MAX_CHIPS, benchmark_hardware, recommend_hardware
from .placement import SAFETY, decide_placement
from .providers import (
    BurstProvider,
    GcpBurstProvider,
    InstanceHandle,
    InstanceSpec,
    MockBurstProvider,
    resolve_provider,
)
from .telemetry import GpuInfo, MeasuredDevice, measure_device
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
    "BurstProvider",
    "BurstReceipt",
    "BurstRequest",
    "CredentialError",
    "CredentialRef",
    "DeviceProfile",
    "GcpBurstProvider",
    "GpuInfo",
    "HardwareRecommendation",
    "Headroom",
    "InstanceHandle",
    "InstanceSpec",
    "MeasuredDevice",
    "MockBurstProvider",
    "PlacementDecision",
    "PlacementTarget",
    "WorkloadEstimate",
    "WorkloadPreset",
    "benchmark_hardware",
    "decide_placement",
    "find_device_profile",
    "find_workload_preset",
    "measure_device",
    "recommend_hardware",
    "resolve_credentials",
    "resolve_provider",
    "run_burst",
]
