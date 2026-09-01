# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Did the chosen action advance the goal? A judged dimension, not an oracle.

The founder's critique of the exam was exact: structural validity and
agreement-with-a-reference are both proxies, and neither answers whether the
goal the user actually had was advanced. The playbook names the same drift --
"treating structural validity as correctness". This module adds the third
number: **goal progress**, judged on-path or off-path per action, reported
beside the other two and never added to them.

It deliberately builds on the review-queue discipline that already exists
rather than inventing a second judging stack:

  * Rows go through ``judge_queue.write_queue`` with planted, shuffled,
    unmarked controls, a content seal outside the run directory, and the
    sanctioned ``verdict_cli`` writer.
  * The grading session must not be the session that wrote the queue.
  * An off-path verdict must cite evidence and name a rule from the CLOSED
    ``goal_progress`` vocabulary in ``integrity.SUITE_RULES``; an invented rule
    voids the run at ingest.
  * ``unsure`` counts against the model. Hedging is not free.

Two design points carry the fairness burden:

**Model identity is stripped.** All models' rows go into one queue under one
seed, so the judge cannot grade by reputation. This exists because the whole
audit happened when a reputation ("qwen is strong") disagreed with a number,
and a judge who knows which rows are whose is measuring the reputation.

**The reference is labelled what it is.** Each row shows the frontier run's
next action as "one known-good continuation, NOT ground truth". A different
action can be on-path; the judge rules on progress toward the goal, not on
imitation. That is the line the loop already refuses to teach across, applied
to judging.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

SUITE_ID = "goal_progress"

# How much of the user's request each row carries. The earlier 500-char tail
# was enough to identify the task but not always its object; a judge ruling
# wrong-object needs the object.
REQUEST_TAIL_CHARS = 2000

NEGATIVE_CONTROL_COUNT = 4
POSITIVE_CONTROL_COUNT = 2


def render_action(tool: Optional[str], args: Any) -> str:
    """One line a judge can read at a glance, verbatim enough to cite."""
    if not tool:
        return "(no tool call: the model replied with prose or nothing)"
    try:
        rendered = json.dumps(args, sort_keys=True) if args else "{}"
    except Exception:  # noqa: BLE001
        rendered = str(args)
    if len(rendered) > 600:
        rendered = rendered[:600] + "...(truncated for display)"
    return f"{tool} {rendered}"


def _row_id(model: str, case_id: str) -> str:
    digest = hashlib.sha256(f"{model}\x1f{case_id}".encode("utf-8")).hexdigest()
    return f"gp-{digest[:10]}"


def build_rows(
    artifact_files: Sequence[Path | str],
) -> tuple[list, dict]:
    """Turn per-case artifacts into judgeable rows, identities held apart.

    Returns ``(rows, identity)``. The rows carry no model name anywhere; the
    identity map (row id to model and case) is the caller's to store NEXT TO
    THE SEAL, outside the run directory, because handing it to the grader
    defeats the blinding exactly the way handing over the seal defeats the
    tamper check.
    """
    rows: list = []
    identity: dict = {}
    for path in artifact_files:
        source = Path(path)
        model = source.stem.replace("corrected_", "").replace("_", "/", 1)
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("indeterminate"):
                # A timeout or compaction says nothing about goal progress.
                continue
            row_id = _row_id(model, record["case_id"])
            reference = render_action(
                record.get("reference_tool"), record.get("reference_args")
            )
            utterance = (
                "USER REQUEST (tail):\n"
                f"{(record.get('user_request_tail') or '')[-REQUEST_TAIL_CHARS:]}\n\n"
                "One known-good continuation from a frontier run -- NOT ground "
                "truth; a different action can still be on-path:\n"
                f"  {reference}"
            )
            output = {
                "action": render_action(
                    record.get("chosen_tool"), record.get("chosen_args")
                ),
                "assistant_text": (record.get("assistant_text") or "")[:800],
            }
            rows.append({"id": row_id, "utterance": utterance, "output": output})
            identity[row_id] = {"model": model, "case_id": record["case_id"]}
    return rows, identity


def negative_controls(rows: Sequence[dict], *, count: int = NEGATIVE_CONTROL_COUNT) -> list:
    """Real requests wearing another case's action: valid, off-path by build.

    The judging contract requires controls the cheap benchmark would NOT catch,
    and these are exactly that: every swapped action is a real model output
    that passed the structural oracles somewhere else. A judge that waves them
    through is rubber-stamping, and the run voids.
    """
    donors = [r for r in rows if "(no tool call" not in r["output"]["action"]]
    controls: list = []
    for index in range(min(count, max(0, len(donors) - 1))):
        base = donors[index]
        # Two exclusions make "off-path by construction" actually true. The
        # donor must use a different tool than the base's own action, and its
        # action must not appear anywhere in the base's utterance -- which is
        # exactly where the reference continuation is printed. Without the
        # second check, a donor whose action equals the base's REFERENCE builds
        # a control that is on-path by construction while labelled must-catch,
        # and it voids the run of any judge diligent enough to notice. Found
        # when a correct grader was voided by control c006.
        donor = None
        for candidate in donors[index + 1 :]:
            different_tool = candidate["output"]["action"].split(" ", 1)[0] != base[
                "output"
            ]["action"].split(" ", 1)[0]
            not_the_reference = candidate["output"]["action"] not in base["utterance"]
            if different_tool and not_the_reference:
                donor = candidate
                break
        if donor is None:
            continue
        controls.append(
            {
                "id": f"control-swapped-{index}",
                "utterance": base["utterance"],
                "output": {
                    "action": donor["output"]["action"],
                    "assistant_text": "",
                },
                "must_catch": (
                    "an action lifted from an unrelated request; structurally "
                    "valid and off-path by construction"
                ),
            }
        )
    return controls


def positive_controls(
    rows: Sequence[dict], identity: dict, artifact_files: Sequence[Path | str],
    *, count: int = POSITIVE_CONTROL_COUNT,
) -> list:
    """Rows whose action byte-equals the reference: on-path by construction.

    Flagging one voids the run, which is what keeps an over-zealous judge from
    farming off-path verdicts out of nothing.
    """
    matches: dict = {}
    for path in artifact_files:
        source = Path(path)
        model = source.stem.replace("corrected_", "").replace("_", "/", 1)
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("indeterminate"):
                continue
            chosen = render_action(record.get("chosen_tool"), record.get("chosen_args"))
            reference = render_action(
                record.get("reference_tool"), record.get("reference_args")
            )
            if chosen == reference:
                matches[_row_id(model, record["case_id"])] = True

    by_id = {r["id"]: r for r in rows}
    controls: list = []
    for row_id in matches:
        if len(controls) >= count:
            break
        row = by_id.get(row_id)
        if not row:
            continue
        controls.append(
            {
                "id": f"control-onpath-{len(controls)}",
                "utterance": row["utterance"],
                "output": row["output"],
                "must_not_flag": (
                    "the action equals the known-good continuation byte for "
                    "byte; calling it off-path is a false positive"
                ),
            }
        )
    return controls


def write_goal_queue(
    *,
    artifact_files: Sequence[Path | str],
    out_dir: Path | str,
    seal_path: Path | str,
    identity_path: Path | str,
    run_id: Optional[str] = None,
    capability_profile: Optional[dict] = None,
):
    """Write one blinded queue covering every model's rows.

    The identity map lands at ``identity_path`` -- put it beside the seal,
    outside the run directory. ``report`` needs it; the grader must not see it.
    """
    from hermes_cli.hussh_one_pkm.judge_queue import write_queue

    rows, raw_identity = build_rows(artifact_files)
    if not rows:
        raise ValueError("no gradeable rows in the artifacts")
    negatives = negative_controls(rows)
    positives = positive_controls(rows, raw_identity, artifact_files)

    # ``write_queue`` blinds row ids to positional ``c{i:03d}`` in input order
    # and drops the ids we authored, so the identity map is keyed by what the
    # verdicts will actually carry. Controls take the indices after the cases
    # and are absent here on purpose: ``report`` treats an unknown id as
    # not-a-model-row.
    identity = {
        f"c{index:03d}": raw_identity[row["id"]]
        for index, row in enumerate(rows)
    }

    queued = write_queue(
        out_dir=Path(out_dir),
        cases=rows,
        answerer_model="blinded:multiple-models",
        run_id=run_id,
        controls=negatives,
        positive_controls=positives,
        capability_profile=capability_profile
        or {"probe_mode": f"{SUITE_ID}/replay/fair-conditions"},
        seal_path=Path(seal_path),
        suite=SUITE_ID,
    )
    Path(identity_path).write_text(
        json.dumps(identity, indent=2, sort_keys=True), encoding="utf-8"
    )
    return queued


def report(
    *,
    out_dir: Path | str,
    seal_path: Path | str,
    identity_path: Path | str,
    judge_label: str,
) -> dict:
    """Ingest the graded queue and report goal progress per model.

    A void run reports void and nothing else: no rate survives a failed
    control. ``unsure`` counts against the rate, per the contract, and the
    denominator is every graded row for that model, so hedging and being wrong
    cost the same.
    """
    from hermes_cli.hussh_one_pkm.judge_queue import ingest
    from hermes_cli.hussh_one_routing import stats

    graded = ingest(
        out_dir=Path(out_dir), judge_label=judge_label, seal_path=Path(seal_path)
    )
    if graded.void:
        return {"void": True, "void_reason": graded.void_reason}

    identity = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    per_model: dict = {}
    for case in graded.cases:
        who = identity.get(case.case_id)
        if not who or case.control is not None or not case.counted:
            continue
        bucket = per_model.setdefault(
            who["model"], {"on_path": 0, "graded": 0, "off_path_rules": {}}
        )
        bucket["graded"] += 1
        if case.verdict == "correct":
            bucket["on_path"] += 1
        elif case.verdict == "wrong" and case.rule:
            bucket["off_path_rules"][case.rule] = (
                bucket["off_path_rules"].get(case.rule, 0) + 1
            )

    for model, bucket in per_model.items():
        bucket["goal_progress"] = stats.describe(bucket["on_path"], bucket["graded"])

    return {
        "void": False,
        "suite": SUITE_ID,
        "per_model": per_model,
        "caveat": (
            "Goal progress is a judged number over blinded rows. It is the "
            "third number beside structural and agreement and is never added "
            "to either; a different action than the reference can be on-path."
        ),
    }
