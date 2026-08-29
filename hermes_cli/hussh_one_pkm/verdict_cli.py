# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The sanctioned way for a judge to record a verdict.

The judge lane is declared read-only and its generated toolset has no Write. The
contract nonetheless asked it to produce verdicts.jsonl, so the only path
available was a shell redirect through Bash: a write laundered past a read-only
declaration, with no validation, able to clobber the file or the queue it was
grading.

This replaces that with a purpose-built writer. It is still invoked through
Bash, but the difference is not cosmetic:

  * it appends one verdict and cannot truncate or rewrite the file;
  * it refuses to write anywhere except the run's verdicts file, so it cannot
    touch the queue it is grading or the manifest holding the answers;
  * it validates against the contract at write time, so a malformed verdict is
    rejected where the judge can still fix it rather than silently discarded at
    ingest;
  * it refuses a duplicate id, because a second verdict for one row is either a
    mistake or an overwrite, and both should be seen rather than resolved
    silently;
  * it verifies the row exists in the queue, which catches a hallucinated id
    before it becomes a score.

"Read-only" remains a label rather than an enforced capability while Bash is in
the toolset. This does not fix that. It makes the one write the judge legitimately
needs into a narrow, auditable, append-only operation instead of arbitrary
filesystem access, which is the difference between a sanctioned path and a
tolerated one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .judge import VERDICT_CORRECT, VERDICT_UNSURE, VERDICT_WRONG
from .judge_queue import QUEUE_FILENAME, VERDICTS_FILENAME

_VERDICTS = (VERDICT_CORRECT, VERDICT_WRONG, VERDICT_UNSURE)


class VerdictRejected(ValueError):
    """The verdict does not satisfy the contract and was not written."""


def _load_queue(run_dir: Path) -> dict[str, dict]:
    path = run_dir / QUEUE_FILENAME
    if not path.exists():
        raise VerdictRejected(f"no queue at {path}")
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            rows[str(row.get("id"))] = row
    return rows


def _existing_ids(run_dir: Path) -> set[str]:
    path = run_dir / VERDICTS_FILENAME
    if not path.exists():
        return set()
    seen = set()
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                seen.add(str(json.loads(line).get("id")))
            except ValueError:
                continue
    return seen


def _citation_present(citation: str, row: dict) -> bool:
    """The citation must quote the output or the utterance.

    Checked here as well as at ingest so the judge learns immediately, while it
    still has the row in front of it, rather than discovering at scoring time
    that a verdict was thrown away.
    """
    needle = citation.strip().casefold().strip('"')
    if not needle:
        return False
    if needle in json.dumps(row.get("output"), sort_keys=True).casefold():
        return True
    return needle in str(row.get("utterance") or "").casefold()


def record(
    *,
    run_dir: Path | str,
    row_id: str,
    verdict: str,
    rule: str = "",
    citation: str = "",
    note: str = "",
) -> dict:
    """Validate one verdict and append it. Raises rather than writing junk."""
    directory = Path(run_dir)
    queue = _load_queue(directory)

    if row_id not in queue:
        # A hallucinated id would otherwise become an ungraded row at ingest,
        # voiding the run for a reason that points at the wrong thing.
        raise VerdictRejected(
            f"row {row_id!r} is not in the queue; graded ids are "
            f"{', '.join(sorted(queue)[:6])}..."
        )

    if verdict not in _VERDICTS:
        raise VerdictRejected(
            f"verdict must be one of {', '.join(_VERDICTS)}, got {verdict!r}"
        )

    if row_id in _existing_ids(directory):
        raise VerdictRejected(
            f"row {row_id!r} already has a verdict; a second one is either a "
            "mistake or an overwrite, and both should be seen"
        )

    if verdict == VERDICT_WRONG:
        if not rule:
            raise VerdictRejected("a `wrong` verdict must name the rule it broke")
        if not _citation_present(citation, queue[row_id]):
            raise VerdictRejected(
                "a `wrong` verdict must quote the offending value verbatim from "
                "this row's output, or the utterance span that went unrecorded. "
                "If you cannot quote it, the verdict is `unsure`."
            )

    entry = {
        "id": row_id,
        "verdict": verdict,
        "rule": rule,
        "citation": citation,
        "note": note,
    }
    # Append only. The judge can add a verdict and can never remove or rewrite
    # one, so a run's grading history is complete by construction.
    with (directory / VERDICTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def progress(run_dir: Path | str) -> dict:
    """How much of the queue is graded. Every row must be, or the run voids."""
    directory = Path(run_dir)
    queue = _load_queue(directory)
    done = _existing_ids(directory)
    remaining = sorted(set(queue) - done)
    return {
        "total": len(queue),
        "graded": len(done & set(queue)),
        "remaining": remaining,
        "complete": not remaining,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    record_cmd = sub.add_parser("record", help="append one validated verdict")
    record_cmd.add_argument("--id", required=True)
    record_cmd.add_argument("--verdict", required=True, choices=list(_VERDICTS))
    record_cmd.add_argument("--rule", default="")
    record_cmd.add_argument("--citation", default="")
    record_cmd.add_argument("--note", default="")

    sub.add_parser("progress", help="how many rows remain ungraded")

    args = parser.parse_args(argv)

    try:
        if args.command == "record":
            entry = record(
                run_dir=args.run_dir,
                row_id=args.id,
                verdict=args.verdict,
                rule=args.rule,
                citation=args.citation,
                note=args.note,
            )
            print(json.dumps(entry, sort_keys=True))
            return 0
        state = progress(args.run_dir)
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0 if state["complete"] else 1
    except VerdictRejected as exc:
        # Rejected loudly and on stderr: a judge that cannot tell a rejection
        # from a success will move on believing the row is graded.
        print(f"rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
