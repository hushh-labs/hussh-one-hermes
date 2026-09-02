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
import random
import re
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


# Corpus-generic tokens that appear in half of all requests and actions; an
# overlap on one of these says nothing about the donor fitting the base.
# The second group is shell/tool syntax: words that describe HOW a command
# scans rather than WHAT it is looking for, so they never count as the
# entity that makes an action specific to one request.
_GENERIC_TOKENS = frozenset(
    {
        "http", "https", "file", "files", "path", "list", "view", "true",
        "false", "null", "name", "command", "pattern", "target", "limit",
        "query", "user", "users", "content", "action", "add", "head", "echo",
        "find", "grep", "type", "maxdepth", "mindepth", "sort", "tail",
        "terminal", "search", "read", "write", "json", "yaml", "timeout",
    }
)


def _tool_family(tool: str) -> str:
    """skills_list and skill_view are one family; terminal is another."""
    return tool.rstrip("s").split("_", 1)[0].rstrip("s")


def _entity_tokens(text: str) -> set:
    """Identifier-ish tokens, with compound names split into their parts.

    ``board-sync.py`` must collide with ``hushh-engineering-board-sync``: the
    judge reads them as the same entity, so the builder has to as well.
    """
    tokens: set = set()
    for raw in re.split(r"[^a-z0-9_-]+", text.casefold()):
        for part in {raw, *re.split(r"[-_]+", raw)}:
            # A shell flag is its word, not its dashes: without this strip,
            # "-maxdepth" dodges the stoplist entry for "maxdepth" and an
            # entity-free find command reads as if it named something.
            part = part.strip("-_")
            if len(part) >= 4 and part not in _GENERIC_TOKENS:
                tokens.add(part)
    return tokens


def _corpus_frequent(rows: Sequence[dict]) -> set:
    """Tokens too common in this corpus to say anything about domain fit.

    A token in a third of all request tails (or donor actions -- repo paths,
    the org name) cannot discriminate one request's domain from another's, and
    counting it as an entity collision would empty the donor pool. Computed
    from the corpus rather than hand-curated so the list tracks whatever the
    corpus is actually about.
    """
    floor = max(3, len(rows) // 5)
    from collections import Counter

    tail_counts: Counter = Counter()
    action_counts: Counter = Counter()
    for row in rows:
        for token in _entity_tokens(_request_tail(row["utterance"])):
            tail_counts[token] += 1
        for token in _entity_tokens(row["output"]["action"]):
            action_counts[token] += 1
    return {t for t, c in tail_counts.items() if c >= floor} | {
        t for t, c in action_counts.items() if c >= floor
    }


def _request_tail(utterance: str) -> str:
    return utterance.split("One known-good continuation", 1)[0]


def negative_controls(rows: Sequence[dict], *, count: int = NEGATIVE_CONTROL_COUNT) -> list:
    """Real requests wearing another case's action: valid, off-path by build.

    The judging contract requires controls the cheap benchmark would NOT catch,
    and these are exactly that: every swapped action is a real model output
    that passed the structural oracles somewhere else. A judge that waves them
    through is rubber-stamping, and the run voids.

    "Off-path by construction" has now failed three times, and each failure
    is a donor exclusion here:

      * A donor whose action equals the base's REFERENCE (printed in the
        utterance) is on-path by byte equality. Found when a correct grader
        was voided by control c006.
      * A donor that stays inside the base request's own domain is on-path by
        semantics: a ``skill_view`` of a skill the request itself lists,
        planted on a skills-curation request, advances that request no matter
        where it was lifted from. Found when control c128 voided a grader who
        had correctly passed the same action shape on the real curation rows.
        Hence two more exclusions: the donor's tool family must differ from
        both the base's own action and the base's reference, and the donor's
        action must share no entity token with the base's request tail.
      * A donor whose action is GENERIC BOOTSTRAP RECON (locate the repo,
        list a directory, check auth) is on-path for most requests in this
        corpus, whatever request it is planted on. Found when a
        locate-the-repo ``find`` -- the real, correct opening move on the
        components-catalog request -- was planted on the action-items
        request and voided a grader whose reasoned, precedent-consistent
        verdict was on-path. Two signatures catch it, both required because
        the first alone missed a donor that appeared for only one request:
        the same byte-identical action produced as the real output for more
        than one distinct request, and an action carrying NO informative
        entity at all once corpus-frequent and shell-syntax tokens are
        stripped -- an action that names nothing specific advances almost
        anything, so it can never anchor an "off-path by construction"
        claim.

    Base rows are picked by a content-seeded shuffle rather than in queue
    order, so not even the session that authored the queue can predict which
    requests carry the swaps.
    """
    donors = [r for r in rows if "(no tool call" not in r["output"]["action"]]
    seed = hashlib.sha256(
        "\x1f".join(sorted(r["id"] for r in rows)).encode("utf-8")
    ).hexdigest()
    rng = random.Random(int(seed[:12], 16))
    bases = rng.sample(donors, k=min(count, len(donors)))
    frequent = _corpus_frequent(rows)

    # Actions that multiple DIFFERENT requests each produced for real are
    # request-agnostic recon; see the third exclusion above.
    action_requests: dict = {}
    for row in rows:
        action_requests.setdefault(row["output"]["action"], set()).add(
            _request_tail(row["utterance"])
        )
    generic_actions = {a for a, reqs in action_requests.items() if len(reqs) > 1}

    controls: list = []
    for index, base in enumerate(bases):
        base_tool = base["output"]["action"].split(" ", 1)[0]
        base_families = {_tool_family(base_tool)}
        reference = base["utterance"].rsplit("on-path:", 1)[-1].strip()
        if reference:
            base_families.add(_tool_family(reference.split(" ", 1)[0]))
        request_tokens = (
            _entity_tokens(_request_tail(base["utterance"])) - frequent
        )

        candidates = [d for d in donors if d is not base]
        rng.shuffle(candidates)
        donor = None
        for candidate in candidates:
            action = candidate["output"]["action"]
            cross_family = _tool_family(action.split(" ", 1)[0]) not in base_families
            not_the_reference = action not in base["utterance"]
            no_shared_entity = not (
                (_entity_tokens(action) - frequent) & request_tokens
            )
            not_generic_recon = action not in generic_actions
            # The donor must name something specific: with frequent and
            # syntax tokens stripped, an entity-free action is generic recon
            # no matter how many requests produced it.
            names_an_entity = bool(_entity_tokens(action) - frequent)
            if (cross_family and not_the_reference and no_shared_entity
                    and not_generic_recon and names_an_entity):
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
