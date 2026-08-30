# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Hand small-model output to a stronger model through the filesystem.

The judge does not need an API key, because the strongest model available is
already running: the Claude Code session driving this repo, with filesystem
access. So the harness writes a review queue to disk, that session grades it,
and the verdicts are read back.

This is better than an API judge in three concrete ways, not merely cheaper:

  * It works today, on any machine, with no credential to provision or leak.
  * The judge can READ THE REPO. An API judge sees only the prompt and the
    output; this one can open the agent manifest, the tool schema and the PKM
    code to decide whether a save was actually right.
  * The queue and the verdicts are files, so a run is reviewable and re-gradeable
    long after it happened, and a disagreement between two judges is a diff.

The queue format is deliberately hostile to a lazy grader.

  * Planted controls are mixed in, shuffled by a seeded permutation, and carry
    no marking of any kind. A grader that cannot be told which rows are the
    trap has to actually read all of them.
  * The control answers are NOT written next to the queue. They live in the
    manifest that the ingest step reads, so a grader working only from the
    queue file cannot look them up.
  * Rows carry a content hash. If the queue is edited between issue and ingest,
    the run is void rather than silently scored against altered evidence.

The one thing this cannot defend against is the same session writing and then
grading in one uninterrupted context: it would remember planting the controls.
`assert_fresh_context` exists for exactly that, and the honest posture is that
it is a discipline, not an enforcement -- so it is recorded in the run metadata
and stated in the report rather than quietly assumed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .judge import (
    NEGATIVE_CONTROLS,
    POSITIVE_CONTROLS,
    VERDICT_CORRECT,
    VERDICT_UNSURE,
    VERDICT_WRONG,
    GradedCase,
    JudgeReport,
    parse_verdict,
)

logger = logging.getLogger(__name__)

QUEUE_FILENAME = "review-queue.jsonl"
VERDICTS_FILENAME = "verdicts.jsonl"
MANIFEST_FILENAME = "run-manifest.json"
LEDGER_FILENAME = "evolution-ledger.jsonl"

SCHEMA_VERSION = 1
SEAL_SUFFIX = ".seal.json"


def _row_hash(row: dict[str, Any]) -> str:
    """Stable hash of the gradable content of one row.

    Covers utterance and output only. The id and position are excluded so that
    reshuffling a queue does not invalidate it, while editing what is actually
    being graded does.
    """
    payload = json.dumps(
        {"utterance": row.get("utterance"), "output": row.get("output")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _seeded_order(count: int, seed: int) -> list[int]:
    """A deterministic permutation.

    Deterministic so a run is reproducible from its manifest, seeded so the
    control positions are not the same every time -- a grader that learned
    "rows 3 and 7 are always traps" would be back to guessing.
    """
    order = list(range(count))
    # Fisher-Yates with a small LCG. Reimplemented rather than using `random`
    # so the permutation is stable across Python versions, which matters when
    # a months-old run is re-graded.
    state = (seed or 1) & 0xFFFFFFFF
    for i in range(count - 1, 0, -1):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        j = state % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


@dataclass
class QueuedRun:
    """A written-out review queue, and the secrets the grader must not see."""

    run_id: str
    queue_path: Path
    manifest_path: Path
    row_count: int
    control_count: int
    # Deliberately outside the run directory. Handing this path to the grader
    # defeats it, so it is returned to the caller and never mentioned in the
    # instructions.
    seal_path: Optional[Path] = None

    def instructions(self) -> str:
        """What to tell the grading session."""
        return (
            f"Grade every row in {self.queue_path}.\n\n"
            f"There are {self.row_count} rows. Some are planted failures; you are "
            "not told which, and the answers are not in that file. Read each one "
            "on its merits.\n\n"
            "You may open the repo to check whether a save was right: the tool "
            "schema is in tools/hussh_one_pkm_tool.py and the grading rules are in "
            "hermes_cli/hussh_one_pkm/judge.py.\n\n"
            "Record each verdict with the sanctioned writer, one row at a time:\n\n"
            "  python3 -m hermes_cli.hussh_one_pkm.verdict_cli \\\n"
            f"    --run-dir {self.queue_path.parent} record \\\n"
            '    --id <row id> --verdict correct|wrong|unsure \\\n'
            '    --rule <rule id> --citation "<exact quote>" --note "<one sentence>"\n\n'
            "It appends, validates, and cannot touch the queue or the manifest. A "
            "rejection exits non-zero and explains itself; do not treat a "
            "rejected row as graded.\n\n"
            "A `wrong` verdict REQUIRES a rule and a citation quoting the offending "
            "value verbatim from that row's output, or the utterance span that went "
            "unrecorded when the failure is an omission. If you cannot quote it, the "
            "verdict is `unsure` -- an uncited failure is discarded, not counted.\n\n"
            "Check you are done with:\n"
            f"  python3 -m hermes_cli.hussh_one_pkm.verdict_cli --run-dir "
            f"{self.queue_path.parent} progress"
        )


def write_queue(
    *,
    out_dir: Path | str,
    cases: Sequence[dict[str, Any]],
    answerer_model: str,
    run_id: Optional[str] = None,
    seed: Optional[int] = None,
    controls: Sequence[dict[str, Any]] = NEGATIVE_CONTROLS,
    positive_controls: Sequence[dict[str, Any]] = POSITIVE_CONTROLS,
    capability_profile: Optional[dict[str, Any]] = None,
    seal_path: Optional[Path | str] = None,
    suite: str = "pkm",
) -> QueuedRun:
    """Write the review queue and the manifest that holds its answers.

    The queue is what the grader sees. The manifest is what `ingest` uses, and
    it is the only place the control positions are recorded.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    run = run_id or f"run-{stamp}"
    # Seeded from the run id so the same run always shuffles the same way, and
    # two runs do not share control positions.
    shuffle_seed = seed if seed is not None else (hash(run) & 0x7FFFFFFF)

    rows: list[dict[str, Any]] = []
    control_ids: dict[str, str] = {}
    for index, case in enumerate(cases):
        rows.append(
            {
                "id": f"c{index:03d}",
                "utterance": str(case.get("utterance") or ""),
                "output": case.get("output"),
            }
        )
    for index, control in enumerate(controls):
        row_id = f"c{len(cases) + index:03d}"
        control_ids[row_id] = str(control.get("must_catch") or "planted failure")
        rows.append(
            {
                "id": row_id,
                "utterance": str(control.get("utterance") or ""),
                "output": control.get("output"),
            }
        )

    # Known-good rows the judge must NOT flag. Without these the design has no
    # false-positive rate: a judge that flags everything passes a
    # negative-only gate perfectly, and its noise reads as diligence.
    clean_ids: dict[str, str] = {}
    offset = len(cases) + len(controls)
    for index, control in enumerate(positive_controls):
        row_id = f"c{offset + index:03d}"
        clean_ids[row_id] = str(control.get("must_not_flag") or "a correct save")
        rows.append(
            {
                "id": row_id,
                "utterance": str(control.get("utterance") or ""),
                "output": control.get("output"),
            }
        )

    order = _seeded_order(len(rows), shuffle_seed)
    shuffled = [rows[i] for i in order]
    for row in shuffled:
        # Versioned per row, not just per manifest. The row shape is the thing
        # most likely to grow -- a multimodal utterance, an agentic trajectory,
        # a second judge's verdict -- and a reader that meets an unknown version
        # should be able to say so instead of misparsing it as v1.
        row["v"] = SCHEMA_VERSION
        row["hash"] = _row_hash(row)

    queue_path = out / QUEUE_FILENAME
    with queue_path.open("w", encoding="utf-8") as handle:
        for row in shuffled:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run,
        "created_at": stamp,
        "answerer_model": answerer_model,
        "capability_profile": capability_profile or {},
        # Which rule vocabulary a verdict in this run may cite. Recorded at
        # issue time so ingest cannot be argued into a more permissive set later.
        "suite": suite,
        "row_count": len(shuffled),
        # The answers. Deliberately NOT in the queue file.
        "controls": control_ids,
        "clean_controls": clean_ids,
        "hashes": {row["id"]: row["hash"] for row in shuffled},
        "queue_file": QUEUE_FILENAME,
        "verdicts_file": VERDICTS_FILENAME,
    }
    manifest_path = out / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # Seal the run OUTSIDE its directory. The judge holds Bash, so everything
    # inside the run directory -- the queue, the manifest, the verdicts, and the
    # harness itself -- is editable. The seal cannot prevent that; it makes it
    # detectable, and only if it lives somewhere the judge was not handed a path
    # to. A seal inside the run directory is one more file to regenerate.
    from .integrity import seal_run, write_seal

    seal = seal_run(
        run_id=run,
        rows=shuffled,
        control_ids=list(control_ids) + list(clean_ids),
    )
    seal_target = Path(seal_path) if seal_path else out.parent / f"{run}{SEAL_SUFFIX}"
    write_seal(seal, seal_target)

    return QueuedRun(
        run_id=run,
        queue_path=queue_path,
        manifest_path=manifest_path,
        row_count=len(shuffled),
        control_count=len(control_ids) + len(clean_ids),
        seal_path=seal_target,
    )


def _citation_is_real(citation: str, output: Any, utterance: str = "") -> bool:
    """A citation must quote something that was actually in front of the judge.

    Checked against the output OR the utterance. Output-only would be a bug that
    silently penalises correct judgement: an OMISSION failure -- the model
    dropped a fact the owner stated -- has nothing to quote in the output by
    definition, since the whole complaint is that it is not there. Forcing those
    to `unsure`, which counts against accuracy, would train a judge away from
    reporting the one failure class that loses the owner's data.

    So an omission cites the utterance span that went unrecorded.
    """
    if not citation:
        return False
    needle = citation.strip().casefold().strip('"')
    if needle in json.dumps(output, sort_keys=True).casefold():
        return True
    return bool(utterance) and needle in utterance.casefold()


def ingest(
    *,
    out_dir: Path | str,
    judge_label: str = "claude-code",
    seal_path: Optional[Path | str] = None,
    require_seal: bool = True,
) -> JudgeReport:
    """Read the verdicts back and score the run.

    Applies exactly the checks the API judge applies -- controls gate the run,
    uncited failures are discarded, unsure counts against accuracy -- plus two
    that only a filesystem handoff needs: the queue must not have changed since
    it was issued, and every row must actually have been graded.
    """
    out = Path(out_dir)
    manifest = json.loads((out / MANIFEST_FILENAME).read_text())
    answerer = str(manifest.get("answerer_model") or "unknown")
    report = JudgeReport(judge_model=judge_label, answerer_model=answerer)

    queue: dict[str, dict[str, Any]] = {}
    for line in (out / QUEUE_FILENAME).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            queue[row["id"]] = row

    # The evidence must be the evidence that was issued.
    expected = manifest.get("hashes") or {}
    tampered = [
        row_id
        for row_id, row in queue.items()
        if expected.get(row_id) and _row_hash(row) != expected[row_id]
    ]
    if tampered:
        report.void = True
        report.void_reason = (
            "queue rows changed between issue and ingest: " + ", ".join(sorted(tampered))
        )
        return report

    verdicts: dict[str, dict[str, Any]] = {}
    verdicts_path = out / VERDICTS_FILENAME
    if verdicts_path.exists():
        for line in verdicts_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            row_id = str(raw.get("id") or "")
            if row_id:
                verdicts[row_id] = parse_verdict(raw)

    ungraded = sorted(set(queue) - set(verdicts))
    if ungraded:
        # Scoring a partial pass would let a grader raise accuracy by skipping
        # the rows it found hard.
        report.void = True
        report.void_reason = (
            f"{len(ungraded)} of {len(queue)} rows were not graded: "
            + ", ".join(ungraded[:8])
            + ("..." if len(ungraded) > 8 else "")
        )
        return report

    controls = manifest.get("controls") or {}
    clean_controls = manifest.get("clean_controls") or {}

    # Integrity check before scoring. The judge holds Bash, so the queue, the
    # manifest, the verdicts and the harness itself are all editable by the
    # party being measured. This cannot prevent that; it detects it, and a run
    # with detectable tampering publishes no accuracy rather than a number
    # produced under rules that may have been rewritten.
    if require_seal:
        from .integrity import describe, read_seal, verify

        resolved_seal = (
            Path(seal_path)
            if seal_path
            else out.parent / f"{manifest.get('run_id')}{SEAL_SUFFIX}"
        )
        violations = verify(
            seal=read_seal(resolved_seal),
            rows=list(queue.values()),
            control_ids=list(controls) + list(clean_controls),
            verdicts=[
                {**parsed, "id": row_id} for row_id, parsed in verdicts.items()
            ],
            run_dir=out,
            seal_path=resolved_seal,
            # The rule vocabulary is a property of what was graded. Without
            # this, a merge judge citing a merge-specific rule is recorded as
            # having invented it, and the whole run voids on its first real
            # finding.
            suite=manifest.get("suite"),
        )
        if violations:
            report.void = True
            report.void_reason = "integrity check failed -- " + describe(violations)
            return report

    for row_id, row in sorted(queue.items()):
        parsed = verdicts[row_id]
        counted = True
        note = parsed["note"]
        if parsed["verdict"] == VERDICT_WRONG and not _citation_is_real(
            parsed["citation"], row.get("output"), str(row.get("utterance") or "")
        ):
            counted = False
            note = (note + " [discarded: citation not found in output]").strip()
        report.cases.append(
            GradedCase(
                case_id=row_id,
                model=answerer,
                verdict=parsed["verdict"],
                rule=parsed["rule"],
                citation=parsed["citation"],
                note=note,
                counted=counted,
                # Both kinds of control are marked, so neither enters the score.
                control=controls.get(row_id) or clean_controls.get(row_id),
            )
        )

    missed = [
        c
        for c in report.cases
        if c.case_id in controls and c.verdict != VERDICT_WRONG
    ]
    if missed:
        report.void = True
        report.void_reason = (
            "grader passed planted failures it was required to catch: "
            + "; ".join(f"{c.case_id} ({c.control})" for c in missed)
        )
        return report

    # The other half of the gate. A judge told to hunt for planted failures can
    # flag every correct row and sail through a negative-only check, with its
    # noise reading as diligence. These rows are unambiguous, so flagging one is
    # not strictness, it is a false positive -- and a judge with an unmeasured
    # false-positive rate produces failures nobody can act on.
    false_positives = [
        c
        for c in report.cases
        if c.case_id in clean_controls and c.verdict == VERDICT_WRONG and c.counted
    ]
    if false_positives:
        report.void = True
        report.void_reason = (
            "grader flagged known-good outputs it was required to pass: "
            + "; ".join(f"{c.case_id} ({c.control})" for c in false_positives)
        )
    return report


def append_to_ledger(
    *,
    ledger_path: Path | str,
    report: JudgeReport,
    capability_profile: Optional[dict[str, Any]] = None,
    benchmark: Optional[dict[str, Any]] = None,
    host: Optional[dict[str, Any]] = None,
    timestamp: Optional[int] = None,
) -> dict[str, Any]:
    """Append one run to the append-only evolution ledger.

    The ledger is the point of the whole exercise. A single score says nothing;
    the same probe across model generations says whether local models are
    actually getting better at this owner's work.

    So it records what makes two runs comparable, not just the number. A score
    without the capability profile, the host and the judge beside it cannot be
    compared to anything -- and a comparison nobody can invalidate is the kind
    that quietly becomes wrong.
    """
    row = {
        "schema_version": SCHEMA_VERSION,
        "at": int(timestamp if timestamp is not None else time.time()),
        "answerer_model": report.answerer_model,
        "judge": report.judge_model,
        "capability_profile": capability_profile or {},
        "host": host or {},
        "benchmark": benchmark or {},
        "scoreboard": report.scoreboard(),
        # Kept even for a void run: "we tried and the grader failed the
        # controls" is a real event, and dropping it would make the ledger look
        # like an unbroken record of successes.
        "void": report.void,
        "void_reason": report.void_reason,
    }
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def read_ledger(ledger_path: Path | str) -> list[dict[str, Any]]:
    """Every recorded run, oldest first. Missing ledger is an empty history."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def compare_runs(
    ledger_path: Path | str, *, model: Optional[str] = None
) -> dict[str, Any]:
    """Compare the two most recent comparable runs.

    Refuses to compare runs whose capability profiles differ, because the probe
    itself adapts to what a model can do: a model tested through tool calling
    and one tested through JSON mode were not asked the same question, and
    putting their accuracies side by side invents a trend.
    """
    rows = [r for r in read_ledger(ledger_path) if not r.get("void")]
    if model:
        rows = [r for r in rows if r.get("answerer_model") == model]
    if len(rows) < 2:
        return {"comparable": False, "reason": "need at least two valid runs"}

    latest, previous = rows[-1], rows[-2]
    latest_probe = (latest.get("capability_profile") or {}).get("probe_mode")
    previous_probe = (previous.get("capability_profile") or {}).get("probe_mode")
    if latest_probe != previous_probe:
        return {
            "comparable": False,
            "reason": (
                f"probe mode changed ({previous_probe!r} -> {latest_probe!r}); "
                "the two runs were not asked the same question"
            ),
        }

    def _accuracy(row: dict[str, Any]) -> Optional[float]:
        return (row.get("scoreboard") or {}).get("accuracy")

    before, after = _accuracy(previous), _accuracy(latest)
    if before is None or after is None:
        return {"comparable": False, "reason": "a run has no measured accuracy"}
    return {
        "comparable": True,
        "from": {"model": previous.get("answerer_model"), "accuracy": before},
        "to": {"model": latest.get("answerer_model"), "accuracy": after},
        "delta": round(after - before, 4),
    }


def assert_fresh_context(manifest_path: Path | str) -> dict[str, Any]:
    """Record whether the grader could have seen the queue being written.

    A session that wrote the queue remembers where it planted the controls, so
    its verdicts prove nothing. This cannot be enforced from inside a script --
    there is no way to ask "are you the same context" -- so it is recorded
    rather than asserted, and the report states it instead of assuming it.

    Returning the fact is the honest option. Pretending to check would be
    worse than not checking.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    return {
        "run_id": manifest.get("run_id"),
        "created_at": manifest.get("created_at"),
        "enforced": False,
        "note": (
            "Context separation between the session that wrote this queue and "
            "the session grading it is a discipline, not an enforced property. "
            "Grade in a session that did not write the queue."
        ),
    }
