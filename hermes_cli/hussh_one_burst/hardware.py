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
                     "light inference, small fine-tunes, IO-bound jobs", (1, 2, 4)),
    AcceleratorClass("nvidia-l4", "gpu", "NVIDIA L4", 24, 2.0, 0.70,
                     "diffusion, batch inference, media"),
    AcceleratorClass("a100-40", "gpu", "NVIDIA A100 40GB", 40, 8.0, 2.90,
                     "mid/large training, fine-tunes"),
    AcceleratorClass("a100-80", "gpu", "NVIDIA A100 80GB", 80, 9.0, 3.67,
                     "large-model training, big batches"),
    AcceleratorClass("h100-80", "gpu", "NVIDIA H100 80GB (A3)", 80, 22.0, 9.80,
                     "frontier training, lowest time-to-result", (8,)),
    AcceleratorClass("h200-141", "gpu", "NVIDIA H200 141GB (A3 Ultra)", 141, 26.0, 10.90,
                     "memory-bound frontier training, long-context inference", (8,)),
    AcceleratorClass("b200-180", "gpu", "NVIDIA B200 180GB (A4)", 180, 45.0, 21.00,
                     "largest single-node training runs, Blackwell-class throughput", (8,)),
    AcceleratorClass("gb200-186", "gpu", "NVIDIA GB200 NVL (A4X)", 186, 55.0, 27.50,
                     "rack-scale frontier workloads, the biggest jobs GCP offers", (4,)),
    AcceleratorClass("tpu-v5e", "tpu", "Cloud TPU v5e", 16, 7.0, 1.20,
                     "JAX/XLA, protein folding, large matmul", (1, 4, 8)),
    AcceleratorClass("tpu-v6e", "tpu", "Cloud TPU v6e (Trillium)", 32, 14.0, 2.70,
                     "high-throughput JAX/XLA training and serving", (1, 4, 8)),
    AcceleratorClass("tpu-v5p", "tpu", "Cloud TPU v5p", 95, 18.0, 4.20,
                     "largest TPU training pods, memory-heavy XLA models", (4, 8)),
]

MAX_CHIPS = 8
_OVERHEAD_SEC = 120


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _ceil_div(numerator: float, denominator: float) -> int:
    return int(-(-numerator // denominator))


def _round_up_to_sellable(chips: int, options: tuple[int, ...]) -> int | None:
    """Smallest purchasable count that covers ``chips``, or None if none does."""
    for option in sorted(options):
        if option >= chips:
            return option
    return None


def recommend_hardware(
    vram_gb: float,
    kind: AcceleratorKind,
    parallel_chips: int = 1,
) -> HardwareRecommendation:
    """Pick the accelerator class with the best performance-per-dollar that fits."""
    # ``parallel_chips`` is a FLOOR the caller asked for, so a class that cannot
    # be bought in that quantity is not a cheaper answer — it is a different,
    # smaller one. Filtering here keeps the recommendation and the comparison
    # table arguing about the same set of machines.
    #
    # Known modelling limit: ``perf`` is per-chip and runtime does not scale with
    # count, so extra chips buy memory and parallel capacity, never wall-clock.
    # Making them buy time would mean inventing a scaling curve the catalog has
    # no ground truth for.
    candidates = [
        c
        for c in ACCEL_CATALOG
        if c.kind == kind and max(c.sellable_chips) >= max(1, parallel_chips)
    ]
    if not candidates:
        candidates = [c for c in ACCEL_CATALOG if c.kind == kind]

    best: tuple[AcceleratorClass, int, float] | None = None
    for c in candidates:
        needed = max(1, _ceil_div(vram_gb, c.mem_gb_per_chip))
        mem_chips = _round_up_to_sellable(needed, c.sellable_chips)
        if mem_chips is None or mem_chips > MAX_CHIPS:
            continue  # this job can't fit one node of this class
        # Score the configuration this candidate would actually be SOLD as,
        # parallelism floor included. Scoring `mem_chips` while returning
        # `max(mem_chips, parallel)` ranked one machine and delivered another:
        # at parallel=8 an A100-80 was scored on 4 chips and billed for 8, and
        # since `perf` is per-chip those extra chips bought memory, not speed.
        # The result beat a GB200 node that finished six times sooner for less
        # money in total.
        billed = _clamp(max(mem_chips, parallel_chips), 1, MAX_CHIPS)
        billed = _round_up_to_sellable(billed, c.sellable_chips) or max(c.sellable_chips)
        # Total job cost is rate over throughput: time scales as 1/perf, so
        # minimising this minimises what the person actually pays.
        proxy = (c.usd_per_hour_per_chip * billed) / c.perf
        if best is None or proxy < best[2]:
            best = (c, mem_chips, proxy)

    if best is None:
        # Nothing in this family fits on a single node — fall back to the biggest.
        big = sorted(candidates, key=lambda c: c.mem_gb_per_chip, reverse=True)[0]
        count = _clamp(parallel_chips, 1, MAX_CHIPS)
        count = _round_up_to_sellable(count, big.sellable_chips) or max(big.sellable_chips)
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
    # parallel_chips can push the count back off a sellable boundary, and can
    # exceed what this class ships at all (8 T4s is not a machine you can buy).
    # Clamp down to the largest purchasable node rather than quoting a shape
    # that does not exist; memory stays covered because mem_chips is itself a
    # sellable count and never exceeds the maximum.
    count = _round_up_to_sellable(count, accel.sellable_chips) or max(accel.sellable_chips)
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
    if max(c.sellable_chips) < max(1, parallel):
        return BenchmarkRow(
            role=role,  # type: ignore[arg-type]
            label=c.label,
            count=max(c.sellable_chips),
            feasible=False,
            note=f"sold in at most {max(c.sellable_chips)}× — cannot meet {parallel}× parallel",
        )
    needed = max(1, _ceil_div(vram_gb, c.mem_gb_per_chip))
    # Same rule as recommend_hardware: this table is the artifact a person reads
    # before approving spend, so every row has to price a machine that can
    # actually be bought. Rounding here and not there would put two different
    # numbers in front of the same decision.
    mem_chips = _round_up_to_sellable(needed, c.sellable_chips)
    if mem_chips is None or mem_chips > MAX_CHIPS:
        return BenchmarkRow(
            role=role,  # type: ignore[arg-type]
            label=c.label,
            count=needed,
            feasible=False,
            note=f"won't fit on one node (needs {needed}× {c.label})",
        )
    count = _clamp(max(mem_chips, parallel), 1, MAX_CHIPS)
    count = _round_up_to_sellable(count, c.sellable_chips) or max(c.sellable_chips)
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
    # Once ranking accounts for whole-node pricing, the best value genuinely IS
    # the biggest box for some jobs. Say so, rather than printing the same
    # machine twice and leaving the reader to notice.
    cheap_note = (
        "same as One's choice — the cheapest chip is also the best value here"
        if cheapest.id == matched.accel.id
        else "naive cheap pick"
    )
    big_note = (
        "same as One's choice — the biggest box is also the best value here"
        if fastest.id == matched.accel.id
        else "naive 'biggest box' pick"
    )
    return [
        _row_for(cheapest, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "undersized", cheap_note),
        _row_for(matched.accel, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "matched", "One's choice — perf/$ that fits"),
        _row_for(fastest, vram_gb, parallel, runtime_min_on_matched, matched.accel.perf,
                 "oversized", big_note),
    ]
