# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared types for Xtreme Burst Compute (pure, no I/O).

The decision layer runs entirely on the person's own machine.  Nothing here
touches the network, a credential, or a clock — which is what lets placement be
decided without any information leaving the device.

``WorkloadEstimate``, ``DeviceProfile``, ``Headroom`` and ``PlacementDecision``
correspond to the interfaces in ``src/lib/burst/types.ts`` in
``hushh-labs/husshone``, in snake_case.  The remaining types
(``AcceleratorClass``, ``HardwareRecommendation``, ``BenchmarkRow``,
``WorkloadPreset``) have no counterpart there and originate in Hermes.

See ``docs/hussh-one/architecture/xtreme-burst.md`` for the migration record,
including the one deliberate incompatibility: ``PlacementTarget`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Accelerator family a workload asks for.  TPUs exist only in-cloud.
AcceleratorKind = Literal["gpu", "tpu"]

#: Where a job actually runs: the person's own device, or a cloud burst.
#:
#: DELIBERATELY NOT WIRE-COMPATIBLE with the husshone control plane, which uses
#: ``"puppy" | "gcp"`` (``src/lib/burst/types.ts``, the ``[puppy]`` enums in
#: ``docs/specs/burst-control-plane.openapi.yaml``, and ``BurstClient.swift``).
#: Those names bake in one device tier and one provider; Hermes is the burst
#: home now and has to describe a Windows workstation bursting to a non-GCP
#: cloud.  The mapping is exactly ``puppy → device`` and ``gcp → cloud``, so an
#: adapter is a two-line translation — but it must be written deliberately at
#: the boundary, not assumed.  See
#: ``docs/hussh-one/architecture/xtreme-burst.md`` § *Vocabulary*.
PlacementTarget = Literal["device", "cloud"]


@dataclass(frozen=True)
class WorkloadEstimate:
    """A coarse resource estimate for a workload — drives the placement decision."""

    vram_gb: float
    """Accelerator (GPU/unified) memory the job needs, in GB."""

    unified_memory_gb: float
    """Host RAM the job needs, in GB.  On Apple Silicon this is unified memory."""

    vcpus: float
    disk_gb: float
    estimated_minutes: float


@dataclass(frozen=True)
class DeviceProfile:
    """A snapshot of the person's machine — phone, tablet, laptop, or workstation."""

    id: str
    label: str
    cpu_cores: int
    gpu_cores: int
    """Apple GPU core count (informational; capacity is gated on unified memory)."""

    unified_memory_gb: float
    disk_free_gb: float
    network_mbps: float
    online: bool = True


@dataclass(frozen=True)
class Headroom:
    """Spare capacity when a job fits locally (negative = the binding deficit)."""

    memory_gb: float
    disk_gb: float


@dataclass(frozen=True)
class PlacementDecision:
    target: PlacementTarget
    reason: str
    fits_locally: bool
    headroom: Headroom | None = None


@dataclass(frozen=True)
class AcceleratorClass:
    """One purchasable accelerator SKU.

    ``perf`` and ``usd_per_hour_per_chip`` are MODELED inputs (approximate
    on-demand, us-central1) — edit to committed-use or spot rates.  They are
    deliberately data, not logic, so a price change never becomes a code change.
    """

    id: str
    kind: AcceleratorKind
    label: str
    mem_gb_per_chip: float
    perf: float
    """Relative throughput against a T4 baseline (= 1.0).  Modeled."""

    usd_per_hour_per_chip: float
    best_for: str


@dataclass(frozen=True)
class HardwareRecommendation:
    accel: AcceleratorClass
    count: int
    usd_per_hour: float
    fits: bool
    rationale: str


@dataclass(frozen=True)
class BenchmarkRow:
    """One row of the matched-vs-naive comparison shown before anything runs."""

    role: Literal["undersized", "matched", "oversized"]
    label: str
    count: int
    feasible: bool
    note: str
    wall_minutes: float | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class WorkloadPreset:
    """A ready-made ask, phrased the way a person would say it."""

    id: str
    emoji: str
    title: str
    subtitle: str
    accelerator_kind: AcceleratorKind
    matched_runtime_min: float
    """Wall-clock minutes on the matched hardware — drives cost and benchmarks."""

    parallel_chips: int
    estimate: WorkloadEstimate = field(repr=False)
