# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The shared vocabulary every suite speaks.

Two decisions here carry most of the weight.

**Three outcomes, not two.** A check that has no validator for this input has
not passed; it has not run. Folding SKIP into PASS is how a green board comes to
mean "we did not look". 7 of 72 real file edits are extensions no parser covers.

**A verdict carries its diagnosis, not just its score.** This is the input the
reflection stage consumes: GEPA calls it Actionable Side Information, and it is
the difference between "0.4" and "line 620: unindent does not match any outer
indentation level". The first cannot be learned from. The second names the fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

# Why a case failed, which decides who fixes it. The split matters because the
# three demand different responses and lumping them together sends every failure
# to the same place.
COMPREHENSION = "comprehension"  # wrong tool, wrong file, ignored constraint
EXECUTION = "execution"  # right intent, malformed output
HARNESS = "harness"  # our clock or our plumbing ran out, not the model
# A turn that hit max_tokens. Deliberately NOT the same as HARNESS: in a real
# agent loop this is a normal event, because Hermes compacts and continues. A
# single-shot probe cannot see the continuation, so the turn is ungraded here,
# but calling it a harness fault implies a setting to fix when often there is
# none, and calling it a model failure is worse. It is its own category, and
# what actually matters is the quality of the answer after compaction, which
# only a multi-turn probe can measure.
COMPACTED = "compacted"


@dataclass
class Case:
    """One exam item, recovered from a real session."""

    case_id: str
    suite: str
    prompt: list  # chat messages, ready for `complete()`
    expected: Any = None  # label, where one exists
    context: dict = field(default_factory=dict)  # what the oracles need
    tokens: Optional[int] = None  # measured, not estimated
    provenance: dict = field(default_factory=dict)

    @property
    def has_label(self) -> bool:
        return self.expected is not None


@dataclass
class Outcome:
    """One oracle's answer about one case."""

    name: str
    outcome: str  # PASS / FAIL / SKIP
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome == FAIL


@dataclass
class Verdict:
    """Every oracle's answer about one case, plus the diagnosis."""

    case_id: str
    suite: str
    outcomes: list = field(default_factory=list)
    label_match: Optional[bool] = None
    indeterminate: str = ""  # non-empty means the turn could not be graded
    elapsed_s: Optional[float] = None
    reasoning_tokens: Optional[int] = None

    @property
    def ok(self) -> bool:
        """No oracle failed. SKIP does not block, but it is never a pass."""
        return not self.indeterminate and not any(o.failed for o in self.outcomes)

    @property
    def checked(self) -> bool:
        """At least one oracle actually ran."""
        return any(o.outcome != SKIP for o in self.outcomes)

    @property
    def failures(self) -> list:
        return [o for o in self.outcomes if o.failed]

    @property
    def asi(self) -> str:
        """The failure text a reflector reads. Empty when nothing failed.

        Names the oracle beside its detail, because "broken-structure" alone
        says a class and "line 620: unindent does not match any outer
        indentation level" says the instance, and a fix needs both.
        """
        return "\n".join(f"{o.name}: {o.detail}" for o in self.failures if o.detail)

    @property
    def fault(self) -> str:
        """Whose problem this is.

        Three distinct outcomes that a single ``indeterminate`` flag used to
        collapse into one, and they call for different responses.

        A **timeout** is our clock. It was the binding constraint on 7 of 50
        real turns while being reported as a budget problem, so it gets named.

        A **truncation** is a turn Hermes would compact and continue. Not a
        model failure and not really ours either; the answer after compaction is
        what matters, and a one-shot probe cannot see it.

        Anything else is plumbing.
        """
        if self.indeterminate:
            reason = self.indeterminate.lower()
            if "truncat" in reason or "length" in reason:
                return COMPACTED
            return HARNESS
        if self.label_match is False and not self.failures:
            # Well-formed and wrong: it did the job it thought it was given.
            return COMPREHENSION
        for outcome in self.failures:
            if outcome.name in _COMPREHENSION_ORACLES:
                return COMPREHENSION
        return EXECUTION if self.failures else ""


# Oracles whose failure means the model misread the goal rather than fumbled the
# output. Kept as an explicit set so the classification is auditable instead of
# inferred from a name pattern.
_COMPREHENSION_ORACLES = frozenset(
    {
        "tool_name_correct",
        "tool_in_catalog",
        "paths_grounded",
        "no_unrequested_destructive_verb",
        "abstains_when_no_tool_fits",
        "needle_recalled",
    }
)


def summarize(verdicts: list) -> dict:
    """Per-suite counts. Deliberately no single blended number.

    ``graded`` and ``offered`` are kept apart because a model that answered 4 of
    20 cases and got 3 right is not a 75% model, and reporting it as one is how
    a model that mostly times out looks better than one that always answers.
    """
    offered = len(verdicts)
    gradeable = [v for v in verdicts if not v.indeterminate]
    labelled = [v for v in gradeable if v.label_match is not None]
    faults: dict[str, int] = {}
    for verdict in verdicts:
        fault = verdict.fault
        if fault:
            faults[fault] = faults.get(fault, 0) + 1

    per_oracle: dict[str, dict] = {}
    for verdict in gradeable:
        for outcome in verdict.outcomes:
            row = per_oracle.setdefault(
                outcome.name, {PASS: 0, FAIL: 0, SKIP: 0}
            )
            row[outcome.outcome] += 1

    return {
        "offered": offered,
        "graded": len(gradeable),
        "indeterminate": offered - len(gradeable),
        # Broken out because they mean different things and only one of them is
        # a number to act on. Timeouts say raise the clock; compacted says the
        # turn would have continued in a real loop and this probe stopped early.
        "timed_out": sum(1 for v in verdicts if v.fault == HARNESS),
        "compacted": sum(1 for v in verdicts if v.fault == COMPACTED),
        "ok": sum(1 for v in gradeable if v.ok),
        "label_match": sum(1 for v in labelled if v.label_match),
        "labelled": len(labelled),
        "faults": faults,
        "per_oracle": per_oracle,
    }
