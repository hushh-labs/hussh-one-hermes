# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Run the learning loop on real session turns, not on a chore.

The loop was previously exercised on merge conflicts, which is one upstream task
and close to none of the work Hermes does. Whether a playbook can move a model
on *the owner's actual turns* is the question that decides whether any of this
is worth shipping, and it is the only place the loop's negative control means
anything.

The generator is the on-device model answering a replayed moment. The verifier
is the replay suite, which already returns a diagnosis rather than a score. The
reflector is a strong model reading only the failures.

**Two grading signals, and only one of them may drive learning.** Structural
failures are unambiguous: a shell command that does not parse is wrong however
the reference behaved, and a tactic that fixes it is a real tactic. Agreement
failures are not: the label is one frontier trajectory, so a model that picks a
different tool may be right. Teaching to agreement would train imitation of a
recorded trace, and the model would learn to copy a run rather than do a job.
So ``learnable_failures`` withholds agreement-only misses from the reflector and
they are reported without being taught to.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Sequence

from .exam import replay as RP
from .exam.model import COMPACTED, HARNESS

logger = logging.getLogger(__name__)

SUITE_ID = "replay"

# Oracles whose failure is true regardless of what the reference model did.
STRUCTURAL_ORACLES = frozenset(
    {
        "shell_parses",
        "parses",
        "no_escaped_delimiter",
        "no_truncation",
        "no_interactive_command",
        "background_flag_consistency",
        "no_unrequested_destructive_verb",
        "bounded_recursive_scan",
        "paths_grounded",
        "arguments_valid",
        "no_invented_arguments",
        "tool_in_catalog",
    }
)


def learnable_failures(verdicts: Sequence[Any]) -> list:
    """The failures a reflector may write tactics about.

    Excludes three things, each for its own reason.

    **Timeouts and compaction**: neither is the model getting the job wrong. A
    tactic written about a truncated turn comes out as "be more concise" aimed
    at a model that was mid-sentence.

    **Agreement-only misses**: the reference is one frontier trajectory, not
    ground truth. Teaching to it produces imitation of a recorded run.

    What survives is a structural failure with its exact diagnostic, which is
    the only evidence here that is true independent of the reference.
    """
    out = []
    for verdict in verdicts:
        if verdict.indeterminate:
            continue
        structural = [
            outcome
            for outcome in verdict.failures
            if outcome.name in STRUCTURAL_ORACLES
        ]
        if not structural:
            continue
        out.append(
            {
                "case_id": verdict.case_id,
                "suite": SUITE_ID,
                "fault": verdict.fault,
                "oracles": [o.name for o in structural],
                "asi": "\n".join(
                    f"{o.name}: {o.detail}" for o in structural if o.detail
                ),
            }
        )
    return out


def score(verdicts: Sequence[Any]) -> float:
    """Structural validity over offered cases.

    Structural rather than agreement, for the reason in the module docstring:
    it is the signal the loop is allowed to learn from, so it must also be the
    signal the loop is measured on. Optimising one number while reporting
    another is how a loop appears to work.

    Over *offered*, so a model that times out on half the cases cannot score
    well on the half it answered.
    """
    if not verdicts:
        return 0.0
    ok = 0
    for verdict in verdicts:
        if verdict.indeterminate:
            continue
        if not any(o.name in STRUCTURAL_ORACLES for o in verdict.failures):
            ok += 1
    return ok / len(verdicts)


def agreement(verdicts: Sequence[Any]) -> Optional[float]:
    """Imitation of the reference. Reported, never optimised."""
    labelled = [
        v for v in verdicts if not v.indeterminate and v.label_match is not None
    ]
    if not labelled:
        return None
    return sum(1 for v in labelled if v.label_match) / len(labelled)


def make_answerer(
    *,
    model: str,
    max_tokens: int,
    timeout: float,
    reasoning_prefix: str = "",
    reasoning_effort: str = "low",
    complete_fn: Optional[Callable] = None,
) -> Callable:
    """An ``answer(case, playbook_text) -> Verdict`` for the on-device model.

    ``reasoning_effort`` defaults to ``"low"`` for backward compatibility, but
    a real caller should pass ``reasoning.effort_for(model, mode)``: "low" is
    inert for gemma and LIVE for qwen3.8 through LM Studio's chat template,
    where it injects a think-less instruction. Sending it unconditionally is
    how a MAX-thinking learning round silently ran qwen3.8 think-less.
    """
    import json

    def answer(case, playbook_text: str):
        messages = [dict(m) for m in case.messages]
        preamble = "\n\n".join(p for p in (reasoning_prefix, playbook_text) if p.strip())
        if preamble:
            for message in messages:
                if message.get("role") == "system":
                    message["content"] = f"{preamble}\n\n{message.get('content', '')}"
                    break
            else:
                messages.insert(0, {"role": "system", "content": preamble})

        caller = complete_fn
        if caller is None:
            from .request import complete as caller  # noqa: PLC0415

        turn = caller(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            tools=RP.tools_payload(case) or None,
            timeout=timeout,
        )
        if turn.indeterminate:
            verdict = RP.grade(case, chosen=None, arguments=None)
            verdict.indeterminate = (
                "timeout"
                if turn.timed_out
                else "truncated"
                if turn.truncated
                else (turn.error or "error")
            )
            return verdict

        calls = getattr(turn, "tool_calls", None) or []
        chosen = args = None
        if calls:
            function = (calls[0] or {}).get("function") or {}
            chosen = function.get("name")
            raw = function.get("arguments")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:  # noqa: BLE001
                    raw = {}
            args = raw if isinstance(raw, dict) else {}
        return RP.grade(case, chosen=chosen, arguments=args)

    return answer


def summarize_round(before: Sequence[Any], after: Sequence[Any]) -> dict:
    """What one round changed, with both signals kept apart."""
    return {
        "structural_before": round(score(before), 4),
        "structural_after": round(score(after), 4),
        "structural_delta": round(score(after) - score(before), 4),
        "agreement_before": agreement(before),
        "agreement_after": agreement(after),
        "timed_out_before": sum(1 for v in before if v.fault == HARNESS),
        "compacted_before": sum(1 for v in before if v.fault == COMPACTED),
        "learnable_failures": len(learnable_failures(before)),
        "caveat": (
            "Only structural failures are taught to. Agreement is reported "
            "because the label is one frontier trajectory rather than ground "
            "truth, and teaching to it would train imitation of a recorded run."
        ),
    }
