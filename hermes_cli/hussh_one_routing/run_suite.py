# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Drive one suite across a ladder and write a report that states its own limits.

This is the piece that was missing. ``walk``, ``probe_capabilities``,
``corpus_build`` and the suites all existed and were each tested, but nothing
stitched them together, so every real measurement so far has come from a
throwaway script. Throwaway scripts are where the comparability bugs live: the
one that ran a MoE at 262144 against a dense model at 16384 was a scratch file
that simply never thought about context.

What this refuses to do is as important as what it does:

  * It will not run at a context below the floor, and it pins the same context
    on every rung, read back from the server rather than assumed.
  * It will not let a local model grade a local model.
  * It will not fold indeterminate turns into a correctness rate. Truncation
    means the budget ran out mid-answer, which is a harness fault, and at a
    1600-token budget it accounted for 12 of 12 merge cases.
  * It will not report a single blended score across suites, or add
    ``reference_match`` to anything a judge later confirms.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import host as H
from .ladder import comparability, walk
from .request import Turn, complete
from .suites import merge_conflict as merge_suite

logger = logging.getLogger(__name__)

SUITES = {merge_suite.SUITE_ID: merge_suite}

# Generous on purpose. Reasoning cannot be turned down on this server, so the
# only way not to publish a truncation as a model failure is to leave room for
# it: gemma-4-26b-a4b-qat spent a mean of 5492 reasoning tokens per merge case.
DEFAULT_MAX_TOKENS = 12000

DEFAULT_TIMEOUT_S = 600.0


def _assert_auditor_is_not_local(judge_model: Optional[str]) -> None:
    """A model may not grade its own class of output.

    ``assert_auditor_is_not_local`` has existed in the PKM package for a while
    with no production caller. This is the caller.
    """
    if judge_model is None:
        return
    from hermes_cli.hussh_one_pkm.integrity import assert_auditor_is_not_local

    assert_auditor_is_not_local(judge_model)


def run(
    *,
    models: Sequence[str],
    cases: Sequence[Any],
    originals: dict,
    suite_id: str = merge_suite.SUITE_ID,
    reps: int = 1,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning_effort: str = "low",
    context_length: Optional[int] = None,
    judge_model: Optional[str] = None,
    destination: Optional[Path] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Walk the ladder over one suite and grade every turn deterministically."""
    suite = SUITES[suite_id]
    announce = on_progress or (lambda _m: None)
    _assert_auditor_is_not_local(judge_model)

    # The context is a property of the ladder, not a constant. Adding a model
    # with a smaller window silently narrows the whole comparison, so it is
    # resolved from the actual membership and checked against the floor.
    pinned = context_length or H.common_max_context(list(models))
    if not pinned:
        raise RuntimeError("could not determine a common context length")
    if pinned < H.MINIMUM_LADDER_CONTEXT:
        raise RuntimeError(
            f"ladder context {pinned} is below the {H.MINIMUM_LADDER_CONTEXT} "
            "floor; one model's window is dragging the comparison down to "
            "something that does not test long context at all"
        )
    announce(f"context pinned at {pinned} across {len(models)} models")

    graded: dict[str, list] = {}
    timings: dict[str, list] = {}
    reasoning_spend: dict[str, list] = {}

    # A ladder pass is hours long, and writing the report only at the end means
    # an interruption at hour two destroys hour one. This already happened: a
    # five-model run was killed during the fourth rung and three complete models
    # of results existed nowhere but the progress log. Every completed case is
    # checkpointed instead, so a resumed run costs the rung it was in and
    # nothing before it.
    checkpoint = Path(str(destination) + ".partial") if destination else None

    def save_checkpoint() -> None:
        if not checkpoint:
            return
        try:
            checkpoint.write_text(
                json.dumps(
                    {
                        "context_length": pinned,
                        "max_tokens": max_tokens,
                        "completed": {
                            model: [asdict(v) for v in verdicts]
                            for model, verdicts in graded.items()
                        },
                        "timings": timings,
                        "reasoning_spend": reasoning_spend,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            # A failed checkpoint must never take down the run it exists to
            # protect.
            logger.debug("checkpoint write failed", exc_info=True)

    def run_case(model: str, case: Any) -> Turn:
        started = time.time()
        turn = complete(
            model=model,
            messages=suite.prompt_for(case),
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            timeout=DEFAULT_TIMEOUT_S,
        )
        elapsed = time.time() - started
        timings.setdefault(model, []).append(round(elapsed, 1))
        reasoning_spend.setdefault(model, []).append(turn.reasoning_tokens or 0)

        # An indeterminate turn is never graded. It carries no information about
        # whether the model can do the job, and averaging it in as a failure is
        # how a harness under-budget gets published as a model result.
        if turn.indeterminate:
            announce(
                f"{model} {getattr(case, 'case_id', '')}: INDETERMINATE "
                f"({'timeout' if turn.timed_out else 'truncated' if turn.truncated else turn.error})"
            )
            save_checkpoint()
            return turn

        answer = suite.strip_fences(turn.content)
        verdict = suite.grade(answer, case, originals[case.case_id])
        graded.setdefault(model, []).append(verdict)
        announce(
            f"{model} {getattr(case, 'case_id', '')}: "
            f"{verdict.failed_check or ('match' if verdict.reference_match else verdict.side)}"
            f" ({elapsed:.0f}s)"
        )
        save_checkpoint()
        return turn

    result = walk(
        models=list(models),
        suite_id=suite_id,
        cases=list(cases),
        run_case=run_case,
        reps=reps,
        on_progress=announce,
        **H.ladder_io(pinned),
    )

    report = {
        "suite": suite_id,
        "context_length": pinned,
        "max_tokens": max_tokens,
        "reasoning_effort_sent": reasoning_effort,
        "reasoning_effort_is_inert_on_this_server": True,
        "cases_offered": len(cases),
        "reps": reps,
        "comparability": comparability(
            [_rung(r) for r in result["rungs"]]
        ),
        "per_model": {},
        "caveats": list(REPORT_CAVEATS),
    }

    for model in models:
        verdicts = graded.get(model, [])
        offered = len(cases) * max(1, reps)
        summary = suite.summarize(verdicts)
        summary["graded"] = len(verdicts)
        summary["indeterminate"] = offered - len(verdicts)
        summary["offered"] = offered
        latencies = timings.get(model, [])
        if latencies:
            ordered = sorted(latencies)
            summary["latency_s"] = {
                "median": ordered[len(ordered) // 2],
                "max": ordered[-1],
            }
        spend = reasoning_spend.get(model, [])
        if spend:
            summary["reasoning_tokens"] = {
                "mean": round(sum(spend) / len(spend)),
                "max": max(spend),
            }
        report["per_model"][model] = summary

    report["ranking"] = rank(report["per_model"])
    if destination:
        Path(destination).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _rung(row: Any):
    """Rehydrate just enough of a rung for the comparability check."""
    from .ladder import RungResult

    if isinstance(row, RungResult):
        return row
    return RungResult(
        model=row.get("model", ""),
        suite=row.get("suite", ""),
        available_gb_before_load=row.get("available_gb_before_load"),
        context_length=row.get("context_length"),
    )


REPORT_CAVEATS = (
    "Truncated and timed-out turns are indeterminate, not wrong. They are "
    "excluded from every rate and counted separately.",
    "reference_match measures agreement with the resolution this fork shipped, "
    "which is a correct answer and not the only one. It is never added to a "
    "judge result.",
    "The merge corpus contains no keep-ours case, so a model that silently "
    "discards fork behaviour scores clean on it.",
    "reasoning_effort is sent but inert on this LM Studio build; the budget "
    "absorbs reasoning rather than limiting it.",
)


def rank(per_model: dict) -> list:
    """Validity first, latency second, and never the other way round.

    Latency never buys a ranking: gemma-4-e2b was the fastest model on the PKM
    ladder and produced zero usable saves.
    """
    rows = []
    for model, summary in per_model.items():
        graded_n = summary.get("graded", 0)
        ok = summary.get("deterministically_ok", 0)
        rows.append(
            {
                "model": model,
                "valid_rate": round(ok / graded_n, 3) if graded_n else None,
                "graded": graded_n,
                "indeterminate": summary.get("indeterminate", 0),
                "reference_match": summary.get("reference_match", 0),
                "median_s": (summary.get("latency_s") or {}).get("median"),
            }
        )
    rows.sort(
        key=lambda r: (
            -(r["valid_rate"] if r["valid_rate"] is not None else -1),
            r["median_s"] if r["median_s"] is not None else 1e9,
        )
    )
    return rows
