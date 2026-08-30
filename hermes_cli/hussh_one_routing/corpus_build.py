# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Freeze real merge conflicts, with the resolution that actually shipped.

The live upstream backlog gives conflicts but no answers: that merge is
unresolved, so every case would go to the judge and the suite could never report
a deterministic number. Past merges are better. Replaying one reconstructs the
conflict exactly as git presented it, and the merge commit is the resolution a
human actually shipped. That is ground truth, recovered rather than invented.

Replay is read-only. ``git merge-tree --write-tree`` reports conflicts without
touching the working tree or the index, and blobs are read with ``git show``.
Nothing here checks anything out, which matters because this runs on a machine
whose working tree is serving live WhatsApp traffic.

**Only single-hunk files carry a reference.** In a file with three hunks the
merge commit shows the resolved result but not which hunk each line came from,
and attributing them requires exactly the guesswork this corpus exists to avoid.
Multi-hunk files are still emitted, with ``reference_side`` empty, and the suite
routes them to the judge. The split is recorded in the manifest so a report
cannot quietly present judge-only cases as deterministic ones.

**What this corpus is not.** It is one fork's history, heavily Python, and the
shipped resolution is one correct answer rather than the only one. A model that
resolves differently and correctly scores as a miss until the judge rescues it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .suites.merge_conflict import (
    SIDE_OURS,
    SIDE_THEIRS,
    MergeCase,
    classify_side,
    extract_cases,
    find_conflicts,
)

logger = logging.getLogger(__name__)

# Text files only. A conflicted PNG teaches nothing about merge reasoning.
CORPUS_EXTENSIONS = (".py", ".js", ".mjs", ".ts", ".tsx", ".json", ".md", ".sh")

# A hunk larger than this is a whole-file rewrite wearing conflict markers, and
# grading it measures context length rather than merge reasoning.
MAX_HUNK_LINES = 120

# Below this, a match rate is a small-sample artifact rather than a measurement.
MIN_REFERENCES_FOR_A_RATE = 30


@dataclass
class CorpusEntry:
    """One frozen case: the conflict, and what shipped."""

    case_id: str
    path: str
    merge_sha: str
    conflicted_text: str
    reference_text: str
    pre: str
    ours: str
    theirs: str
    post: str
    reference_side: str = ""
    hunks_in_file: int = 1

    @property
    def has_reference(self) -> bool:
        return bool(self.reference_side)


@dataclass
class BuildReport:
    """What was collected, and what was passed over and why."""

    entries: list = field(default_factory=list)
    merges_scanned: int = 0
    merges_with_conflicts: int = 0
    skipped: dict = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _blob(repo: Path, sha: str, path: str) -> Optional[str]:
    """File content at a commit, or None when the file is absent there."""
    result = _git(repo, "show", f"{sha}:{path}")
    return result.stdout if result.returncode == 0 else None


def conflicted_merges(repo: Path, *, limit: int = 200) -> list[tuple[str, list[str]]]:
    """Past merges that actually conflicted, with the paths that conflicted.

    A clean merge is skipped: git resolved it, so it says nothing about whether
    a model could have.
    """
    listing = _git(repo, "log", "--merges", "--format=%H", f"-{limit}")
    out: list[tuple[str, list[str]]] = []
    for sha in listing.stdout.split():
        parents = _git(repo, "rev-parse", f"{sha}^@").stdout.split()
        if len(parents) != 2:
            continue  # An octopus merge is not a two-sided conflict.
        replay = _git(repo, "merge-tree", "--write-tree", "--name-only", *parents)
        if replay.returncode == 0:
            continue
        paths = _conflicted_paths(replay.stdout)
        if paths:
            out.append((sha, paths))
    return out


def _conflicted_paths(stdout: str) -> list[str]:
    """Read only the path section of ``merge-tree --write-tree`` output.

    The output is three sections separated by a blank line: the tree OID, the
    conflicted paths, then informational messages. Reading past the blank line
    turns "Auto-merging mod.py" and "CONFLICT (content): ..." into paths, which
    then fail to replay and are counted as skips. That silently understates the
    corpus while looking like git could not reproduce the conflicts.
    """
    lines = stdout.splitlines()
    paths: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            break  # End of the path section; the rest is prose.
        paths.append(line.strip())
    return paths


def rebuild_conflict(repo: Path, merge_sha: str, path: str) -> Optional[str]:
    """Recreate the conflicted file exactly as git would have shown it.

    ``git merge-file`` is given the three real blobs, so the markers, the
    ordering, and the hunk boundaries are git's own rather than a reconstruction
    that might drift from what a developer would actually have seen.
    """
    parents = _git(repo, "rev-parse", f"{merge_sha}^@").stdout.split()
    if len(parents) != 2:
        return None
    ours_sha, theirs_sha = parents
    base_sha = _git(repo, "merge-base", ours_sha, theirs_sha).stdout.strip()
    if not base_sha:
        return None

    ours = _blob(repo, ours_sha, path)
    theirs = _blob(repo, theirs_sha, path)
    if ours is None or theirs is None:
        # Added on one side or deleted on the other. That is a tree conflict,
        # not a content conflict, and this suite grades content.
        return None
    base = _blob(repo, base_sha, path) or ""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = {}
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            handle = root / name
            handle.write_text(text, encoding="utf-8")
            paths[name] = str(handle)
        merged = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                paths["ours"],
                paths["base"],
                paths["theirs"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    # Return code is the conflict count; negative means git failed outright.
    if merged.returncode < 0:
        return None
    return merged.stdout or None


def build(
    repo: Path | str,
    *,
    limit: int = 200,
    max_entries: int = 60,
) -> BuildReport:
    """Collect frozen cases from past merges, newest first."""
    root = Path(repo)
    report = BuildReport()

    merges = conflicted_merges(root, limit=limit)
    report.merges_scanned = limit
    report.merges_with_conflicts = len(merges)

    for merge_sha, paths in merges:
        for path in paths:
            if len(report.entries) >= max_entries:
                return report
            if not path.endswith(CORPUS_EXTENSIONS):
                report.skip("not-a-text-source")
                continue

            conflicted = rebuild_conflict(root, merge_sha, path)
            if not conflicted:
                report.skip("could-not-replay")
                continue
            blocks = find_conflicts(conflicted)
            if not blocks:
                # merge-file resolved it even though merge-tree flagged the
                # path; the conflict was in a rename or mode, not the content.
                report.skip("no-content-hunk")
                continue

            reference = _blob(root, merge_sha, path)
            if reference is None:
                report.skip("resolution-deleted-the-file")
                continue

            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / Path(path).name
                staged.write_text(conflicted, encoding="utf-8")
                cases = extract_cases(staged)

            for case in cases:
                if len(report.entries) >= max_entries:
                    return report
                hunk_lines = len(case.ours.splitlines()) + len(
                    case.theirs.splitlines()
                )
                if hunk_lines > MAX_HUNK_LINES:
                    report.skip("hunk-too-large")
                    continue

                entry = CorpusEntry(
                    case_id=f"{merge_sha[:10]}:{path}#{case.case_id.rsplit('#', 1)[-1]}",
                    path=path,
                    merge_sha=merge_sha,
                    conflicted_text=conflicted,
                    reference_text=reference,
                    pre=case.pre,
                    ours=case.ours,
                    theirs=case.theirs,
                    post=case.post,
                    hunks_in_file=len(blocks),
                )
                # Only a single-hunk file lets the shipped resolution be
                # attributed to this hunk without guessing.
                if len(blocks) == 1:
                    entry.reference_side = classify_side(reference, case)
                report.entries.append(entry)

    return report


def to_cases(entries: list) -> list:
    """Turn frozen entries back into gradeable cases."""
    cases = []
    for entry in entries:
        case = MergeCase(
            case_id=entry.case_id,
            path=entry.path,
            pre=entry.pre,
            ours=entry.ours,
            theirs=entry.theirs,
            post=entry.post,
            reference=entry.reference_text,
            reference_side=entry.reference_side,
        )
        cases.append(case)
    return cases


def coverage_gaps(with_reference: list) -> list:
    """Which resolution behaviours this corpus cannot measure at all.

    Sampled from one fork's history, the references skew hard. Merges here are
    mostly a feature branch landing on main, where the resolution takes the
    branch, so ``ours`` can be entirely absent. A corpus with no keep-ours case
    cannot detect a model that silently discards the fork's own changes, and for
    an upstream sync that is the expensive failure rather than a rare one.

    Reported rather than corrected. Manufacturing the missing cases would put
    invented conflicts in a corpus whose only claim is that it is real.
    """
    present = {e.reference_side for e in with_reference}
    gaps = []
    for side, consequence in (
        (
            SIDE_OURS,
            "no case requires keeping the fork's side, so a model that "
            "silently discards fork behaviour scores clean here",
        ),
        (
            SIDE_THEIRS,
            "no case requires taking upstream, so a model that ignores "
            "upstream changes scores clean here",
        ),
    ):
        if side not in present:
            gaps.append({"missing_reference_side": side, "consequence": consequence})
    if len(with_reference) < MIN_REFERENCES_FOR_A_RATE:
        gaps.append(
            {
                "missing_reference_side": "",
                "consequence": (
                    f"only {len(with_reference)} reference cases; too few to "
                    "quote a match rate as anything but indicative"
                ),
            }
        )
    return gaps


def freeze(report: BuildReport, destination: Path | str) -> dict[str, Any]:
    """Write the corpus to disk with a manifest that states its limits."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with_reference = [e for e in report.entries if e.has_reference]
    manifest = {
        "coverage_gaps": coverage_gaps(with_reference),
        "cases": len(report.entries),
        "with_reference": len(with_reference),
        "judge_only": len(report.entries) - len(with_reference),
        "merges_scanned": report.merges_scanned,
        "merges_with_conflicts": report.merges_with_conflicts,
        "skipped": report.skipped,
        "reference_sides": {
            side: sum(1 for e in with_reference if e.reference_side == side)
            for side in {e.reference_side for e in with_reference}
        },
        "caveat": (
            "The shipped resolution is a correct answer, not the only one. "
            "Cases without a reference are judge-only and must never be "
            "reported inside a deterministic rate."
        ),
    }
    target.write_text(
        json.dumps(
            {"manifest": manifest, "entries": [asdict(e) for e in report.entries]},
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def load(source: Path | str) -> tuple[list, dict[str, Any]]:
    """Read a frozen corpus back."""
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    entries = [CorpusEntry(**row) for row in payload["entries"]]
    return entries, payload["manifest"]
