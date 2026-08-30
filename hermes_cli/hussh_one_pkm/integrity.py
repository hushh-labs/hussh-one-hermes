# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Make a judge's edits detectable, because they cannot be prevented.

The judge lane holds Bash. That is not a small caveat, it is the whole threat
model: a judge with a shell can read the manifest naming which rows are planted,
rewrite the queue it is grading, append verdicts without the validating writer,
rewrite verdicts it already gave, and edit the rules it is judged against. Every
control in this harness is enforced by code the judge can edit.

So the honest position is that the judge has god-mode inside the run directory
and prevention is not available at this layer. What IS available is making every
one of those edits **detectable**, and voiding the run when one is found. That
converts "the judge can cheat invisibly" into "the judge can cheat and the
result is discarded", which is the difference between a number nobody should
trust and no number at all.

Four things are sealed at issue time:

  * **Every queue row**, so altering the evidence after it was served is caught.
  * **The control identities**, kept as a salted hash rather than a list, so the
    seal itself does not hand over the answer key it protects.
  * **The harness source**, so a judge that rewrites the rules, the controls, or
    the ingest logic invalidates the run rather than redefining it.
  * **A hash chain over verdicts**, so rewriting an earlier verdict breaks every
    verdict after it.

The seal must live **outside the run directory**. Inside, it is one more file
the judge can regenerate. Outside, tampering requires finding it first, and the
caller is free to keep it somewhere the judge has no path to at all -- another
directory, another machine, the ledger.

Nothing here is cryptographic authentication. A judge that locates the seal and
the salt can forge a consistent one. The real fix is a sandbox where the judge
cannot write to the harness or the seal at all, and that is an infrastructure
change rather than a code change. This raises the cost of an undetected edit
from zero to "find and rewrite a second file you were never told about", and it
makes the ordinary case -- a judge that improvises without meaning to cheat --
reliably visible.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

SEAL_VERSION = 1
SEAL_SUFFIX = ".seal.json"

# The harness files whose contents define what a valid run means. If any of
# these change between issue and ingest, the rules being enforced at scoring are
# not the rules the run was issued under.
SEALED_SOURCES = (
    "judge.py",
    "judge_queue.py",
    "verdict_cli.py",
    "integrity.py",
)

# Grading logic that lives outside this package but decides the same verdicts.
# Sealed relative to the repo root rather than this directory: an oracle a judge
# could edit undetected is a rule it can rewrite mid-run, which is exactly what
# sealing exists to prevent. Paths that do not exist hash as "<missing>" and are
# therefore also detected if one is deleted.
SEALED_REPO_SOURCES = (
    "hermes_cli/hussh_one_write_guard.py",
    "hermes_cli/hussh_one_routing/request.py",
    "hermes_cli/hussh_one_routing/profile.py",
)

# A verdict may only name a rule the contract defines. Without this a judge can
# invent a rule, cite something real against it, and produce a failure that
# looks fully compliant while grading against a standard nobody agreed to.
#
# Keyed by suite, because a single flat set makes every non-PKM run void on its
# first real finding: a merge judge citing `kept-wrong-side` would be recorded
# as an invented rule, `verify` would raise, and `ingest` would discard the
# whole run. The rule vocabulary is a property of what is being graded, not of
# the harness.
SUITE_RULES: dict[str, frozenset[str]] = {
    "pkm": frozenset(
        {
            "right-domain",
            "no-invention",
            "durable-only",
            "no-metadata",
            "minimal-patch",
            "faithful-summary",
        }
    ),
    "code_edit": frozenset(
        {
            "wrong-target",      # edited something other than what was asked
            "incomplete-edit",   # the asked-for change is not fully present
            "collateral-change", # changed code outside the intended region
            "duplicated-region", # emitted context twice instead of replacing it
            "broken-structure",  # indentation or syntax the file cannot carry
            "invented-symbol",   # referenced something that does not exist
        }
    ),
    "merge": frozenset(
        {
            "kept-wrong-side",   # chose ours where theirs was correct, or vice versa
            "dropped-fork-behaviour",  # silently lost a Hussh-One-only change
            "dropped-upstream-change", # silently lost an upstream addition
            "duplicated-region",
            "broken-structure",
            "markers-left",      # conflict markers survived into the result
        }
    ),
}

# Every rule any suite defines. `verify` accepts a rule when it belongs to the
# run's own suite; this union exists only so an older run whose suite is
# unrecorded still verifies rather than voiding retroactively.
KNOWN_RULES = frozenset().union(*SUITE_RULES.values())


def rules_for(suite: Optional[str]) -> frozenset[str]:
    """The rule vocabulary a verdict in this suite may cite.

    An unknown or absent suite falls back to the union rather than to the PKM
    set. Being permissive here is right: the alternative is voiding a run for
    citing a rule that is real but belongs to a suite this code did not know
    about, which punishes the run for the harness being out of date.
    """
    if suite and suite in SUITE_RULES:
        return SUITE_RULES[suite]
    return KNOWN_RULES


@dataclass
class Violation:
    kind: str
    detail: str


@dataclass
class Seal:
    """What the run looked like when it was issued."""

    version: int
    run_id: str
    salt: str
    row_hashes: dict[str, str]
    control_commitment: str
    source_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "salt": self.salt,
            "row_hashes": self.row_hashes,
            "control_commitment": self.control_commitment,
            "source_hashes": self.source_hashes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Seal":
        return cls(
            version=int(data.get("version") or 0),
            run_id=str(data.get("run_id") or ""),
            salt=str(data.get("salt") or ""),
            row_hashes=dict(data.get("row_hashes") or {}),
            control_commitment=str(data.get("control_commitment") or ""),
            source_hashes=dict(data.get("source_hashes") or {}),
        )


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def row_fingerprint(row: dict[str, Any], salt: str) -> str:
    """Hash of what a row actually asks the judge to grade.

    Salted so a judge cannot recompute a matching fingerprint for altered
    evidence without also holding the seal.
    """
    payload = json.dumps(
        {"utterance": row.get("utterance"), "output": row.get("output")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(salt, payload)


def control_commitment(control_ids: Iterable[str], salt: str) -> str:
    """A commitment to WHICH rows are planted, without naming them.

    A plain list would make the seal an answer key: anyone who found it would
    know exactly which rows to catch. The commitment proves the set did not
    change, and reveals nothing about its members.
    """
    joined = ",".join(sorted(str(i) for i in control_ids))
    return _digest(salt, joined)


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        # A missing harness file is itself a finding: the run cannot be scored
        # by rules that are not there.
        return "<missing>"


def source_hashes(package_dir: Optional[Path | str] = None) -> dict[str, str]:
    """Hash the harness files that define what a valid run means."""
    root = Path(package_dir) if package_dir else Path(__file__).parent
    hashes: dict[str, str] = {name: _hash_file(root / name) for name in SEALED_SOURCES}

    # Grading logic outside this package. Skipped entirely when `package_dir` is
    # supplied, because that argument means "hash this directory" -- a test
    # pointing at a temp dir must not have real repo files silently folded in,
    # which would make its seal depend on files it never wrote.
    if package_dir is None:
        repo_root = Path(__file__).resolve().parents[2]
        for rel in SEALED_REPO_SOURCES:
            hashes[rel] = _hash_file(repo_root / rel)
    return hashes


def seal_run(
    *,
    run_id: str,
    rows: Sequence[dict[str, Any]],
    control_ids: Iterable[str],
    package_dir: Optional[Path | str] = None,
    salt: Optional[str] = None,
) -> Seal:
    """Capture what the run looks like at issue time."""
    run_salt = salt or os.urandom(16).hex()
    return Seal(
        version=SEAL_VERSION,
        run_id=run_id,
        salt=run_salt,
        row_hashes={
            str(row.get("id")): row_fingerprint(row, run_salt) for row in rows
        },
        control_commitment=control_commitment(control_ids, run_salt),
        source_hashes=source_hashes(package_dir),
    )


def write_seal(seal: Seal, path: Path | str) -> Path:
    """Persist a seal. The caller chooses where, and it must not be the run dir.

    Not enforced here, because this function cannot know the run directory. The
    caller that does know is responsible, and `verify` reports a seal found
    inside the run directory as a violation in its own right.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(seal.to_dict(), indent=2, sort_keys=True) + "\n")
    return target


def read_seal(path: Path | str) -> Optional[Seal]:
    try:
        return Seal.from_dict(json.loads(Path(path).read_text()))
    except Exception:
        return None


def verdict_chain(entries: Sequence[dict[str, Any]], salt: str) -> str:
    """Fold verdicts into one hash, in order.

    Rewriting or reordering any earlier verdict changes every hash after it, so
    a judge cannot revise a verdict it already gave and leave the file looking
    untouched.
    """
    running = _digest(salt, "genesis")
    for entry in entries:
        payload = json.dumps(
            {
                "id": entry.get("id"),
                "verdict": entry.get("verdict"),
                "rule": entry.get("rule", ""),
                "citation": entry.get("citation", ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        running = _digest(running, payload)
    return running


def verify(
    *,
    seal: Optional[Seal],
    rows: Sequence[dict[str, Any]],
    control_ids: Iterable[str],
    verdicts: Sequence[dict[str, Any]],
    run_dir: Optional[Path | str] = None,
    seal_path: Optional[Path | str] = None,
    package_dir: Optional[Path | str] = None,
    suite: Optional[str] = None,
) -> list[Violation]:
    """Everything that changed between issue and ingest.

    An empty list means nothing detectable was altered. It does not mean nothing
    was altered: a judge that found and rewrote the seal defeats this. It means
    the ordinary failure -- editing the run and not the seal -- is caught.
    """
    violations: list[Violation] = []

    if seal is None:
        # No seal at all is the most important finding, not the least. An
        # unsealed run is one where tampering is undetectable by construction,
        # and it must not be scored as if it were verified.
        return [
            Violation(
                "no-seal",
                "run was not sealed at issue time; tampering is undetectable",
            )
        ]

    if seal_path is not None and run_dir is not None:
        try:
            Path(seal_path).resolve().relative_to(Path(run_dir).resolve())
            violations.append(
                Violation(
                    "seal-inside-run",
                    f"seal lives inside the run directory ({seal_path}); the judge "
                    "can regenerate it, so it protects nothing",
                )
            )
        except ValueError:
            pass  # outside the run dir, which is what we want

    seen_ids = set()
    for row in rows:
        row_id = str(row.get("id"))
        seen_ids.add(row_id)
        expected = seal.row_hashes.get(row_id)
        if expected is None:
            violations.append(
                Violation("row-added", f"row {row_id} was not in the sealed queue")
            )
        elif row_fingerprint(row, seal.salt) != expected:
            violations.append(
                Violation(
                    "row-altered",
                    f"row {row_id} was edited after it was issued",
                )
            )
    for row_id in sorted(set(seal.row_hashes) - seen_ids):
        violations.append(
            Violation("row-removed", f"row {row_id} was removed from the queue")
        )

    if control_commitment(control_ids, seal.salt) != seal.control_commitment:
        violations.append(
            Violation(
                "controls-altered",
                "the set of planted rows changed after the run was issued",
            )
        )

    current_sources = source_hashes(package_dir)
    for name, expected in seal.source_hashes.items():
        actual = current_sources.get(name, "<missing>")
        if actual != expected:
            violations.append(
                Violation(
                    "harness-altered",
                    f"{name} changed between issue and ingest; the rules enforced "
                    "at scoring are not the rules the run was issued under",
                )
            )

    permitted = rules_for(suite)
    for entry in verdicts:
        rule = str(entry.get("rule") or "")
        if str(entry.get("verdict")) == "wrong" and rule and rule not in permitted:
            # Improvising a rule produces a failure that looks compliant while
            # grading against a standard nobody agreed to.
            violations.append(
                Violation(
                    "invented-rule",
                    f"verdict for {entry.get('id')} cites rule {rule!r}, which the "
                    f"{suite or 'default'} contract does not define",
                )
            )

    return violations


def describe(violations: Sequence[Violation]) -> str:
    if not violations:
        return "no detectable tampering"
    return "; ".join(f"{v.kind}: {v.detail}" for v in violations)
