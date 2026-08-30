# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Walk a model ladder so the rungs are actually comparable.

The obvious reading of the memory arithmetic is wrong and worth stating, because
it changes what this module does. The five-model ladder is 68.1 GB of weights
against ~64 GB available, which looks like "eviction is mandatory to fit". It is
not: the walker loads one rung at a time, so the binding constraint is the
largest single model (26.10 GB), which fits with room to spare.

The real requirement is stricter and different. **Drain to empty before every
rung.** Opportunistic eviction -- clearing only enough room for the next model --
leaves each rung running under whatever the previous rung happened to leave
behind, so a latency difference between two models becomes a memory-pressure
artifact and the routing table records it as a property of the model.

So: unload everything, verify empty, record the memory actually available, then
load. A rung whose pre-load `available_gb` differs materially from its
neighbours is not comparable to them, and the report says so rather than
ranking it.

Two other comparability hazards handled here:

  * **Order and thermal state.** A model measured after forty minutes of
    sustained GPU load runs on a hotter machine. Model order is counterbalanced
    across reps rather than fixed.
  * **The resident head start.** Whatever is loaded when the walk begins would
    otherwise get a free warm cold-start. Draining first is what makes its cold
    number mean the same thing as everyone else's.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Sequence

from .request import CircuitBreaker, Turn

logger = logging.getLogger(__name__)

# How far apart two rungs' pre-load available memory may be before the run is
# flagged as not comparable. Generous: normal background drift on a desktop is
# a couple of GB, and a false alarm here would discard a good three-hour run.
COMPARABILITY_MEMORY_TOLERANCE_GB = 6.0


@dataclass
class RungResult:
    """One model's pass over one suite, plus the conditions it ran under."""

    model: str
    suite: str
    turns: list = field(default_factory=list)
    available_gb_before_load: Optional[float] = None
    resident_before_load: list = field(default_factory=list)
    abandoned: bool = False
    abandoned_reason: str = ""
    load_error: str = ""
    wall_clock_offset_s: Optional[float] = None

    @property
    def usable(self) -> bool:
        """False when this rung produced nothing worth scoring."""
        return not self.abandoned and not self.load_error and bool(self.turns)


def drain(
    *,
    unload: Callable[[str], bool],
    resident: Callable[[], list],
    protect: Sequence[str] = (),
    attempts: int = 3,
) -> dict[str, Any]:
    """Unload everything, then verify it actually happened.

    Verified rather than assumed: `unload_model` returning True means the
    request was accepted, not that the weights are gone, and a rung measured on
    a machine still holding the previous model is the exact artifact this
    exists to prevent.
    """
    protected = {str(p) for p in protect}
    unloaded: list[str] = []
    for _attempt in range(max(1, attempts)):
        current = [
            str(entry.get("identifier") or "")
            for entry in resident()
            if str(entry.get("identifier") or "") not in protected
        ]
        if not current:
            return {"empty": True, "unloaded": unloaded, "still_resident": []}
        for identifier in current:
            try:
                if unload(identifier):
                    unloaded.append(identifier)
            except Exception:  # noqa: BLE001
                logger.debug("unload failed for %s", identifier, exc_info=True)

    remaining = [
        str(entry.get("identifier") or "")
        for entry in resident()
        if str(entry.get("identifier") or "") not in protected
    ]
    return {
        "empty": not remaining,
        "unloaded": unloaded,
        "still_resident": remaining,
    }


def counterbalanced_order(models: Sequence[str], rep: int) -> list[str]:
    """Rotate model order per rep so position and thermal state decorrelate.

    A fixed order means one model is always measured on a cold machine and
    another always on a hot one, and that difference is indistinguishable from
    a difference between the models.
    """
    ordered = list(models)
    if not ordered:
        return ordered
    offset = rep % len(ordered)
    return ordered[offset:] + ordered[:offset]


def comparability(rungs: Sequence[RungResult]) -> dict[str, Any]:
    """Whether these rungs ran under conditions close enough to compare.

    Reported, never silently corrected. If the answer is no, the honest output
    is "this run is not comparable" rather than a ranking with a caveat that
    gets dropped the first time someone quotes the number.
    """
    readings = [
        r.available_gb_before_load
        for r in rungs
        if isinstance(r.available_gb_before_load, (int, float))
    ]
    if len(readings) < 2:
        return {
            "comparable": False,
            "reason": "fewer than two rungs recorded a memory reading",
            "spread_gb": None,
        }
    spread = round(max(readings) - min(readings), 2)
    within = spread <= COMPARABILITY_MEMORY_TOLERANCE_GB
    return {
        "comparable": within,
        "reason": (
            ""
            if within
            else (
                f"pre-load available memory varied by {spread} GB across rungs "
                f"(tolerance {COMPARABILITY_MEMORY_TOLERANCE_GB}); latency "
                "differences may be memory artifacts rather than model "
                "differences"
            )
        ),
        "spread_gb": spread,
        "min_gb": round(min(readings), 2),
        "max_gb": round(max(readings), 2),
    }


def walk(
    *,
    models: Sequence[str],
    suite_id: str,
    run_case: Callable[[str, Any], Turn],
    cases: Sequence[Any],
    reps: int = 1,
    unload: Optional[Callable[[str], bool]] = None,
    resident: Optional[Callable[[], list]] = None,
    available_gb: Optional[Callable[[], Optional[float]]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run every model over every case, one rung at a time, drained between.

    The I/O is injected so the walk is testable without a server and without
    evicting anything on a real machine.
    """
    announce = on_progress or (lambda _m: None)
    started = clock()
    rungs: list[RungResult] = []

    for rep in range(max(1, reps)):
        for model in counterbalanced_order(models, rep):
            rung = RungResult(model=model, suite=suite_id)
            rung.wall_clock_offset_s = round(clock() - started, 2)

            if unload is not None and resident is not None:
                drained = drain(unload=unload, resident=resident)
                if not drained["empty"]:
                    # Do not measure on a dirty machine and call it a result.
                    rung.load_error = (
                        "could not drain before load; still resident: "
                        + ", ".join(drained["still_resident"])
                    )
                    announce(f"{model}: {rung.load_error}")
                    rungs.append(rung)
                    continue

            if available_gb is not None:
                try:
                    rung.available_gb_before_load = available_gb()
                except Exception:  # noqa: BLE001
                    logger.debug("memory probe failed", exc_info=True)

            breaker = CircuitBreaker()
            for case in cases:
                announce(f"{model} rep{rep} {getattr(case, 'case_id', '')}")
                turn = run_case(model, case)
                rung.turns.append(turn)
                breaker.record(turn)
                if breaker.abandoned:
                    rung.abandoned = True
                    rung.abandoned_reason = breaker.reason
                    announce(f"{model}: {breaker.reason}")
                    break
            rungs.append(rung)

    return {
        "suite": suite_id,
        "reps": reps,
        "rungs": [asdict(r) for r in rungs],
        "comparability": comparability(rungs),
        "wall_clock_s": round(clock() - started, 2),
    }
