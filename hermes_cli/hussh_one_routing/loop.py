# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The learning loop: generate on-device, verify deterministically, reflect offline.

Puppy One ships with a $0 token budget, so the model that serves has to be the
model on the device. That only works if it gets better over time without anyone
paying per call, and this is how.

    Generate   the on-device model answers the exam        ($0, every round)
    Verify     deterministic oracles score it and, more
               importantly, say WHY it failed
    Reflect    a stronger model reads only the failures
               and proposes tactics                        (offline, occasional)
    Curate     accepted tactics append to a playbook
    Re-run     on a held-out split the reflector never saw
    Ledger     the run is recorded so the next one can be
               compared to it, or refused as incomparable

The shape is GEPA's (arXiv 2507.19457), which beats reinforcement learning by up
to 20% while using 35x fewer rollouts, and it works because the verifier hands
back *text*, not a number. "0.4" cannot be learned from. "line 620: unindent
does not match any outer indentation level" names the fix. GEPA calls that
Actionable Side Information; ``Verdict.asi`` is where ours comes from.

Two rules exist to stop this loop from lying to itself.

**Only the held-out split counts.** A tactic that lifts the training split and
not the held-out one has taught the model this corpus rather than this job.
Pioneer Agent (arXiv 2604.09791) names overfitting-to-the-evaluation as the
characteristic failure of exactly this arrangement.

**The reflector never grades.** It proposes tactics from failures the oracles
already found. It cannot mark anything correct, so it cannot rubber-stamp, and
a run where it also judged would be a model grading its own class of output.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from . import playbook as pb
from .exam.model import HARNESS, Verdict, summarize

logger = logging.getLogger(__name__)

# Fraction of cases withheld from the reflector entirely.
HELD_OUT_FRACTION = 0.3

# A round must beat the previous held-out score by at least this to count as an
# improvement. Anything smaller is indistinguishable from run-to-run noise on a
# corpus this size, and calling it progress is how a flat loop looks like a
# working one.
MIN_MEANINGFUL_GAIN = 0.02


@dataclass
class RoundResult:
    """One pass of the loop."""

    round_number: int
    model: str
    suite: str
    train: dict = field(default_factory=dict)
    held_out: dict = field(default_factory=dict)
    proposed: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    retired: list = field(default_factory=list)
    improved: bool = False
    delta: Optional[float] = None
    void: bool = False
    void_reason: str = ""
    # Which cases sat on which side, and where they came from. Two arms of a
    # comparison must show identical lists here or the comparison is void: the
    # first matched/control pair silently ran on two different case sets
    # because the live sessions directory changed between the two launches.
    train_ids: list = field(default_factory=list)
    held_out_ids: list = field(default_factory=list)
    corpus: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "model": self.model,
            "suite": self.suite,
            "train": self.train,
            "held_out": self.held_out,
            "proposed": len(self.proposed),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "retired": self.retired,
            "improved": self.improved,
            "delta": self.delta,
            "void": self.void,
            "void_reason": self.void_reason,
            "train_ids": list(self.train_ids),
            "held_out_ids": list(self.held_out_ids),
            "corpus": self.corpus,
        }


def split_cases(cases: Sequence[Any], *, held_out: float = HELD_OUT_FRACTION):
    """Partition into train and held-out, stably.

    Hashed on the case id rather than shuffled, so the same case lands on the
    same side on every run and across machines. A split that moves between
    rounds would let a case migrate out of held-out exactly when it started
    failing, which is the most flattering possible bug.
    """
    train, hold = [], []
    for case in cases:
        case_id = getattr(case, "case_id", str(case))
        digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        (hold if bucket < held_out else train).append(case)
    return train, hold


def score(verdicts: Sequence[Verdict]) -> float:
    """Fraction of *offered* cases that came back clean.

    Offered, not graded. A model that answers 4 of 20 and gets 3 right is not a
    75% model, and scoring on graded-only would rank a model that mostly times
    out above one that always answers.
    """
    if not verdicts:
        return 0.0
    return sum(1 for v in verdicts if v.ok) / len(verdicts)


def failures_for_reflection(verdicts: Sequence[Verdict]) -> list:
    """What the reflector is allowed to see.

    Harness faults are excluded deliberately. A truncated turn says the budget
    was too small, and asking a reflector to write a tactic about it produces
    advice like "be more concise" aimed at a model that did nothing wrong.
    """
    payload = []
    for verdict in verdicts:
        if verdict.fault == HARNESS or not verdict.failures:
            continue
        payload.append(
            {
                "case_id": verdict.case_id,
                "suite": verdict.suite,
                "fault": verdict.fault,
                "oracles": [o.name for o in verdict.failures],
                "asi": verdict.asi,
            }
        )
    return payload


def run_round(
    *,
    model: str,
    suite: str,
    cases: Sequence[Any],
    answer: Callable[[Any, str], Verdict],
    reflect: Callable[[list, list], list],
    book: Optional[pb.Playbook] = None,
    score_fn: Optional[Callable[[Sequence[Verdict]], float]] = None,
    failures_fn: Optional[Callable[[Sequence[Verdict]], list]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple:
    """One full round. Returns ``(RoundResult, Playbook)``.

    ``answer(case, playbook_text) -> Verdict`` runs the on-device model and
    grades it. ``reflect(failures, playbook_text) -> [Bullet]`` is the stronger
    model. Both are injected so the loop is testable without a server and
    without spending anything.

    ``score_fn`` and ``failures_fn`` are injectable because the defaults are
    wrong for the replay suite, and the first real round proved it: the generic
    ``score`` counts a disagreement with the reference as a failure, so a model
    with 0.952 structural validity reported a held-out score of 0.357. The
    signal the loop is measured on must be the signal it is allowed to learn
    from, and only the suite knows which oracles those are.
    """
    announce = on_progress or (lambda _m: None)
    scorer = score_fn or score
    collect = failures_fn or failures_for_reflection
    book = book or pb.load(model, suite)
    train, hold = split_cases(cases)
    result = RoundResult(
        round_number=book.round_number + 1, model=model, suite=suite
    )
    result.train_ids = sorted(getattr(c, "case_id", str(c)) for c in train)
    result.held_out_ids = sorted(getattr(c, "case_id", str(c)) for c in hold)

    if not hold:
        result.void = True
        result.void_reason = (
            "no held-out cases; a gain measured only on what the reflector saw "
            "is indistinguishable from memorisation"
        )
        return result, book

    text = book.render()
    announce(f"round {result.round_number}: {len(train)} train, {len(hold)} held out")

    train_verdicts = [answer(case, text) for case in train]
    hold_before = [answer(case, text) for case in hold]
    baseline = scorer(hold_before)
    result.train = summarize(train_verdicts)
    result.held_out = summarize(hold_before)
    result.held_out["score"] = baseline

    failures = collect(train_verdicts)
    announce(f"  {len(failures)} training failures offered to the reflector")
    if not failures:
        # Nothing to learn from is a real outcome, not an error.
        book.record_round(held_out_score=baseline, improved=False)
        pb.save(book)
        return result, book

    proposed = reflect(failures, text) or []
    result.proposed = [b.text for b in proposed]
    for bullet in proposed:
        if book.add(bullet):
            result.accepted.append(bullet.text)
        else:
            result.rejected.append(bullet.text)
    announce(f"  {len(result.accepted)} accepted, {len(result.rejected)} rejected")

    if not result.accepted:
        book.record_round(held_out_score=baseline, improved=False)
        pb.save(book)
        return result, book

    # Re-measure on the held-out split with the new playbook in place. This is
    # the only number that decides anything, and it must use the same scorer as
    # the baseline or the delta compares two different questions.
    after = scorer([answer(case, book.render()) for case in hold])
    result.delta = round(after - baseline, 4)
    result.improved = result.delta >= MIN_MEANINGFUL_GAIN
    result.held_out["score_after"] = after

    retired = book.record_round(held_out_score=after, improved=result.improved)
    result.retired = [b.text for b in retired]
    pb.save(book)
    announce(
        f"  held-out {baseline:.3f} -> {after:.3f} "
        f"({'improved' if result.improved else 'no meaningful gain'})"
    )
    return result, book


def shuffled_control(failures: list, *, seed: int = 0) -> list:
    """Detach every diagnosis from the case that produced it.

    The negative control for the loop itself. If the playbook still "improves"
    when the reflector is fed mismatched evidence, the loop is fitting noise and
    the gains from the real run mean nothing either. A learning loop with no way
    to fail this check is not measured, it is believed.

    Check ``control_is_degenerate`` before trusting a verdict from this: when
    every failure carries the same diagnosis, rotating them changes nothing and
    the two arms are the same experiment run twice.

    A subtler requirement, learned from the first live run: **the reflector must
    read the evidence content, not just the oracle names.** Rotation moves each
    diagnosis to a different case but preserves the multiset of oracle names, so
    a deterministic oracle-to-tactic lookup proposes the identical tactic set in
    both arms no matter how varied the evidence is. With such a reflector this
    control is degenerate by construction. It only measures anything when the
    reflector is a model that writes tactics from the pairing of case and
    diagnosis, which is the arrangement the loop ships with.
    """
    if len(failures) < 2:
        return [dict(f) for f in failures]
    rotated = failures[1:] + failures[:1]
    out = []
    for original, other in zip(failures, rotated):
        row = dict(original)
        row["asi"] = other.get("asi", "")
        row["oracles"] = other.get("oracles", [])
        out.append(row)
    return out


def control_is_degenerate(failures: list) -> tuple:
    """Can shuffling this evidence produce a different experiment at all?

    Learned from a run that reported "LOOP FAILS ITS OWN CONTROL" on two arms
    with byte-identical results. All eleven training failures were the same
    oracle, so rotating the diagnoses among them was a no-op: both arms received
    the same evidence, proposed the same single tactic, and moved the held-out
    score by the same amount. The verdict was not a finding, it was an artifact.

    A control that cannot distinguish its arms must refuse to return a verdict
    rather than return a confident one, so this reports the reason.
    """
    if len(failures) < 2:
        return True, "fewer than two failures; there is nothing to shuffle"
    signatures = {
        (tuple(f.get("oracles") or ()), (f.get("asi") or "").strip())
        for f in failures
    }
    if len(signatures) < 2:
        return True, (
            f"all {len(failures)} failures carry the same diagnosis "
            f"({', '.join(failures[0].get('oracles') or ['?'])}), so rotating "
            "them changes nothing and both arms run the same experiment"
        )
    shuffled = shuffled_control(failures)
    moved = sum(
        1
        for before, after in zip(failures, shuffled)
        if (before.get("asi") or "") != (after.get("asi") or "")
    )
    if moved == 0:
        return True, "the rotation left every diagnosis on its original case"
    return False, ""


def write_report(result: RoundResult, destination) -> None:
    from pathlib import Path

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
