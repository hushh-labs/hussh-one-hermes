# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade a model's merge-conflict resolution, deterministically where possible.

This suite exists because the upstream sync is 894 commits behind with 16
conflicted files, and the updater correctly refuses to touch main until they are
resolved. The question is whether a local model can do that work.

Almost all of the grading is deterministic, which is the point. When
``gemma-4-26b-a4b-qat`` was given one real conflict it chose the right side
semantically and still produced an unusable result -- first line at indent 0
against a body at indent 8, plus the surrounding context re-emitted so a block
appeared twice. None of that needs a judge. Four cheap checks catch it:

  b1  no conflict markers survived
  b2  the region splices back into the file and the file still parses
  b3  the side chosen matches the reference resolution's side
  b4  no context line appears more often than it did before

Only b3 disagreements reach the judge, and only to answer one question: is a
resolution that differs from the shipped one nonetheless correct? That is the
honest boundary of what determinism cannot reach.

**What the reference cannot tell you**, and the report must say so: the fork's
shipped resolution is *a* correct answer, not *the* correct answer. A model that
resolves differently and correctly scores as a miss until the judge rescues it,
and a model that reproduces a resolution the fork later regretted scores as a
hit forever. ``reference_match`` and ``judge_pass`` are therefore reported as two
numbers and never added together.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

SUITE_ID = "merge"
OUTPUT_PROTOCOL = "region"

CONFLICT_START = "<<<<<<<"
CONFLICT_MID = "======="
CONFLICT_END = ">>>>>>>"

# Lines of surrounding context handed to the model and checked for duplication.
CONTEXT_LINES = 20

SIDE_OURS = "ours"
SIDE_THEIRS = "theirs"
SIDE_UNION = "union"
SIDE_SYNTHESIS = "synthesis"
SIDE_NEITHER = "neither"


@dataclass
class MergeCase:
    """One conflict hunk, with everything needed to grade a resolution."""

    case_id: str
    path: str
    pre: str
    ours: str
    theirs: str
    post: str
    # The resolution the fork actually shipped, when one is recoverable.
    reference: Optional[str] = None
    reference_side: str = ""

    @property
    def language_path(self) -> str:
        """A filename the write guard can dispatch on."""
        return self.path


@dataclass
class MergeVerdict:
    """Which stage a resolution died at, and why."""

    case_id: str
    markers_gone: bool = False
    splices_and_parses: bool = False
    no_duplication: bool = False
    side: str = ""
    reference_match: Optional[bool] = None
    needs_judge: bool = False
    failed_check: str = ""
    detail: str = ""
    rules: list = field(default_factory=list)

    @property
    def deterministically_ok(self) -> bool:
        return self.markers_gone and self.splices_and_parses and self.no_duplication


def find_conflicts(text: str) -> list[tuple[int, int, int]]:
    """Locate every complete conflict block as (start, mid, end) line indices."""
    lines = text.splitlines()
    blocks: list[tuple[int, int, int]] = []
    start = mid = None
    for index, line in enumerate(lines):
        if line.startswith(CONFLICT_START):
            start, mid = index, None
        elif line.startswith(CONFLICT_MID) and start is not None and mid is None:
            mid = index
        elif line.startswith(CONFLICT_END) and start is not None and mid is not None:
            blocks.append((start, mid, index))
            start = mid = None
    return blocks


def extract_cases(
    path: Path | str, *, context: int = CONTEXT_LINES
) -> list[MergeCase]:
    """Turn a conflicted file into one case per hunk.

    Line-based, never a regex over the whole file. A regex with ``re.DOTALL``
    and a bounded-repetition context group backtracks catastrophically on a
    100KB source file -- it burned 18 minutes of CPU here without ever reaching
    the model, and was very nearly reported as the model being slow.
    """
    source = Path(path)
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )
    cases: list[MergeCase] = []
    for ordinal, (start, mid, end) in enumerate(
        find_conflicts("".join(lines))
    ):
        cases.append(
            MergeCase(
                case_id=f"{source.name}#{ordinal}",
                path=str(source),
                pre="".join(lines[max(0, start - context) : start]),
                ours="".join(lines[start + 1 : mid]),
                theirs="".join(lines[mid + 1 : end]),
                post="".join(lines[end + 1 : end + 1 + context]),
            )
        )
    return cases


def _line_set(text: str) -> set[str]:
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def classify_side(resolved: str, case: MergeCase) -> str:
    """Which side the resolution took, by line-set comparison.

    Compares against the lines UNIQUE to each side. A line both sides share
    carries no information about the choice, and counting it would classify
    every resolution as a union.
    """
    resolved_lines = _line_set(resolved)
    ours = _line_set(case.ours)
    theirs = _line_set(case.theirs)
    ours_only = ours - theirs
    theirs_only = theirs - ours

    took_ours = bool(ours_only) and ours_only <= resolved_lines
    took_theirs = bool(theirs_only) and theirs_only <= resolved_lines

    if took_ours and took_theirs:
        return SIDE_UNION
    if took_ours:
        return SIDE_OURS
    if took_theirs:
        return SIDE_THEIRS
    # Nothing unique from either side survived intact. That is either a rewrite
    # or a drop, and the two are worth distinguishing.
    if resolved_lines & (ours | theirs):
        return SIDE_SYNTHESIS
    return SIDE_NEITHER


def duplication_check(resolved: str, case: MergeCase) -> tuple[bool, str]:
    """No context line may appear more often than it did before.

    This is the check that catches the observed failure directly. Asked for the
    conflict region only, the model re-emitted the surrounding context as well;
    spliced in, the file then carried the same block twice. A parser accepts
    that happily -- it is valid Python, just wrong.
    """
    resolved_counts: dict[str, int] = {}
    for line in resolved.splitlines():
        stripped = line.strip()
        if stripped:
            resolved_counts[stripped] = resolved_counts.get(stripped, 0) + 1

    for line in (case.pre + case.post).splitlines():
        stripped = line.strip()
        # Single-token lines like ")" or "else:" legitimately recur; only
        # substantial lines are evidence of a copied block.
        if len(stripped) < 12:
            continue
        if resolved_counts.get(stripped, 0) > 0:
            return False, f"context line re-emitted into the region: {stripped[:60]!r}"
    return True, ""


def splice(resolved: str, case: MergeCase, original: str) -> str:
    """Put the model's region back where the conflict was.

    Grading a fragment in isolation is unfair: a correct region fails to parse
    on its own for reasons that are the harness's fault, not the model's.
    """
    lines = original.splitlines(keepends=True)
    blocks = find_conflicts(original)
    if not blocks:
        return original
    ordinal = int(case.case_id.rsplit("#", 1)[-1]) if "#" in case.case_id else 0
    start, _mid, end = blocks[min(ordinal, len(blocks) - 1)]
    body = resolved if resolved.endswith("\n") else resolved + "\n"
    return "".join(lines[:start]) + body + "".join(lines[end + 1 :])


def grade(
    resolved: str, case: MergeCase, original: str
) -> MergeVerdict:
    """Run the four deterministic checks, then decide if a judge is needed."""
    verdict = MergeVerdict(case_id=case.case_id)

    # b1: markers gone.
    if any(
        marker in resolved
        for marker in (CONFLICT_START, CONFLICT_MID, CONFLICT_END)
    ):
        verdict.failed_check = "markers-left"
        verdict.detail = "conflict markers survived into the resolution"
        verdict.rules = ["markers-left"]
        return verdict
    verdict.markers_gone = True

    # b2: splices back and still parses.
    from hermes_cli.hussh_one_write_guard import validate

    spliced = splice(resolved, case, original)
    parse = validate(case.language_path, spliced)
    if not parse.ok:
        verdict.failed_check = "broken-structure"
        verdict.detail = parse.error
        verdict.rules = ["broken-structure"]
        return verdict
    # `checked=False` means no validator for this extension -- unknown, not
    # verified. Recorded as such rather than counted as a pass.
    verdict.splices_and_parses = parse.checked

    # b4: no duplicated context.
    ok, detail = duplication_check(resolved, case)
    if not ok:
        verdict.failed_check = "duplicated-region"
        verdict.detail = detail
        verdict.rules = ["duplicated-region"]
        return verdict
    verdict.no_duplication = True

    # b3: which side, and does it agree with the reference.
    verdict.side = classify_side(resolved, case)
    if case.reference_side:
        verdict.reference_match = verdict.side == case.reference_side
        # A disagreement is not a failure. It is the one question determinism
        # cannot answer: a different resolution may still be correct.
        verdict.needs_judge = not verdict.reference_match
    else:
        verdict.needs_judge = True
    return verdict


def summarize(verdicts: Sequence[MergeVerdict]) -> dict[str, Any]:
    """Per-stage counts. Deliberately no single blended score.

    `reference_match` and anything the judge later confirms are kept apart,
    because the shipped resolution is one correct answer rather than the only
    one, and adding the two numbers would assert otherwise.
    """
    total = len(verdicts)
    deterministic = [v for v in verdicts if v.deterministically_ok]
    matched = [v for v in deterministic if v.reference_match is True]
    return {
        "suite": SUITE_ID,
        "cases": total,
        "markers_gone": sum(1 for v in verdicts if v.markers_gone),
        "splices_and_parses": sum(1 for v in verdicts if v.splices_and_parses),
        "no_duplication": sum(1 for v in verdicts if v.no_duplication),
        "deterministically_ok": len(deterministic),
        "reference_match": len(matched),
        "needs_judge": sum(1 for v in verdicts if v.needs_judge),
        "failed_checks": sorted(
            {v.failed_check for v in verdicts if v.failed_check}
        ),
        "sides": {
            side: sum(1 for v in verdicts if v.side == side)
            for side in (SIDE_OURS, SIDE_THEIRS, SIDE_UNION, SIDE_SYNTHESIS)
            if any(v.side == side for v in verdicts)
        },
    }
