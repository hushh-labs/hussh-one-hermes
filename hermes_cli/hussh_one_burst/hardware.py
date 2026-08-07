# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Best-hardware matching (pure, no I/O).

Given a workload's shape, pick the accelerator class with the best
performance-per-dollar that actually *fits* — not the biggest box (overpay), not
the smallest (won't fit, or crawls).  Catalog prices and throughputs are modeled
inputs; see ``AcceleratorClass``.
"""

from __future__ import annotations

from .types import AcceleratorClass, AcceleratorKind, BenchmarkRow, HardwareRecommendation

#: Google Cloud Compute Engine accelerator SKUs, newest generations included.
ACCEL_CATALOG: list[AcceleratorClass] = [
    AcceleratorClass("nvidia-t4", "gpu", "NVIDIA T4", 16, 1.0, 0.35,
                     "light inference, small fine-tunes, IO-bound jobs"),
    AcceleratorClass("nvidia-l4", "gpu", "NVIDIA L4", 24, 2.0, 0.70,
                     "diffusion, batch inference, media"),
    AcceleratorClass("a100-40", "gpu", "NVIDIA A100 40GB", 40, 8.0, 2.90,
                     "mid/large training, fine-tunes"),
    AcceleratorClass("a100-80", "gpu", "NVIDIA A100 80GB", 80, 9.0, 3.67,
                     "large-model training, big batches"),
    AcceleratorClass("h100-80", "gpu", "NVIDIA H100 80GB (A3)", 80, 22.0, 9.80,
                     "frontier training, lowest time-to-result"),
    AcceleratorClass("h200-141", "gpu", "NVIDIA H200 141GB (A3 Ultra)", 141, 26.0, 10.90,
                     "memory-bound frontier training, long-context inference"),
    AcceleratorClass("b200-180", "gpu", "NVIDIA B200 180GB (A4)", 180, 45.0, 21.00,
                     "largest single-node training runs, Blackwell-class throughput"),
    AcceleratorClass("gb200-186", "gpu", "NVIDIA GB200 NVL (A4X)", 186, 55.0, 27.50,
                     "rack-scale frontier workloads, the biggest jobs GCP offers"),
    AcceleratorClass("tpu-v5e", "tpu", "Cloud TPU v5e", 16, 7.0, 1.20,
                     "JAX/XLA, protein folding, large matmul"),
    AcceleratorClass("tpu-v6e", "tpu", "Cloud TPU v6e (Trillium)", 32, 14.0, 2.70,
                     "high-throughput JAX/XLA training and serving"),
    AcceleratorClass("tpu-v5p", "tpu", "Cloud TPU v5p", 95, 18.0, 4.20,
                     "largest TPU training pods, memory-heavy XLA models"),
]

MAX_CHIPS = 8
_OVERHEAD_SEC = 120


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _ceil_div(numerator: float, denominator: float) -> int:
    return int(-(-numerator // denominator))


def recommend_hardware(
    vram_gb: float,
    kind: AcceleratorKind,
    parallel_chips: int = 1,
) -> HardwareRecommendation:
    """Pick the accelerator class with the best performance-per-dollar that fits."""
    candidates = [c for c in ACCEL_CATALOG if c.kind == kind]

    best: tuple[AcceleratorClass, int, float] | None = None
    for c in candidates:
        mem_chips = max(1, _ceil_div(vram_gb, c.mem_gb_per_chip))
        if mem_chips > MAX_CHIPS:
            continue  # this job can't fit one node of this class
        # Lower is better: dollars needed to cover the memory, per unit of throughput.
        proxy = (c.usd_per_hour_per_chip * mem_chips) / c.perf
        if best is None or proxy < best[2]:
            best = (c, mem_chips, proxy)

    if best is None:
        # Nothing in this family fits on a single node — fall back to the biggest.
        big = sorted(candidates, key=lambda c: c.mem_gb_per_chip, reverse=True)[0]
        count = _clamp(parallel_chips, 1, MAX_CHIPS)
        return HardwareRecommendation(
            accel=big,
            count=count,
            usd_per_hour=_round2(big.usd_per_hour_per_chip * count),
            fits=False,
            rationale=(
                f"~{vram_gb:g}GB exceeds a single {big.label} node — "
                f"needs multi-node (sharded) {big.label}."
            ),
        )

    accel, mem_chips, _ = best
    count = _clamp(max(mem_chips, parallel_chips), 1, MAX_CHIPS)
    total_mem = count * accel.mem_gb_per_chip
    parallel_note = f" · {parallel_chips}× parallel" if parallel_chips > 1 else ""
    return HardwareRecommendation(
        accel=accel,
        count=count,
        usd_per_hour=_round2(accel.usd_per_hour_per_chip * count),
        fits=True,
        rationale=(
            f"~{vram_gb:g}GB{parallel_note} → {count}× {accel.label} ({total_mem:g}GB) "
            "— best performance-per-dollar that fits."
        ),
    )


def _row_for(
    c: AcceleratorClass,
    vram_gb: float,
    parallel: int,
    runtime_min_on_matched: float,
    matched_perf: float,
    role: str,
    note: str,
) -> BenchmarkRow:
    mem_chips = max(1, _ceil_div(vram_gb, c.mem_gb_per_chip))
    if mem_chips > MAX_CHIPS:
        return BenchmarkRow(
            role=role,  # type: ignore[arg-type]
            label=c.label,
            count=mem_chips,
            feasible=False,
            note=f"won't fit on one node (needs {mem_chips}× {c.label})",
        )
    count = _clamp(max(mem_chips, parallel), 1, MAX_CHIPS)
    runtime_min = runtime_min_on_matched * (matched_perf / c.perf)
    wall_minutes = _round2(runtime_min + _OVERHEAD_SEC / 60)
    return BenchmarkRow(
        role=role,  # type: ignore[arg-type]
        label=f"{count}× {c.label}",
        count=count,
        feasible=True,
        note=note,
        wall_minutes=wall_minutes,
        cost_usd=_round2(c.usd_per_hour_per_chip * count * (wall_minutes / 60)),
    )


def benchmark_hardware(
    vram_gb: float,
    kind: AcceleratorKind,
    parallel: int,
    runtime_min_on_matched: float,
) -> list[BenchmarkRow]:
    """Compare the matched choice against a naive-cheap and a naive-biggest pick.

    This is what makes the recommendation auditable by the person paying for it:
    the alternative they would have picked by hand, priced.
    """
    fam = [c for c in ACCEL_CATALOG if c.kind == kind]
    matched = recommend_hardware(vram_gb, kind, parallel)
    cheapest = sorted(fam, key=lambda c: c.usd_per_hour_per_chip)[0]
    fastest = sorted(fam, key=lambda c: c.perf, reverse=True)[0]
    return [
        _row_for(cheapest, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "undersized", "naive cheap pick"),
        _row_for(matched.accel, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "matched", "One's choice — perf/$ that fits"),
        _row_for(fastest, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "oversized", "naive 'biggest box' pick"),
    ]
