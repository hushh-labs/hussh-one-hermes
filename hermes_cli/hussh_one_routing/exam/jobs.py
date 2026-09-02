# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade what the daily cron jobs actually produced, not whether they exited 0.

The founder's bar, 2026-09-02: "see the outputs are always production grade".
A job can complete, deliver, and still be wrong: a wiki maintenance run that
answered "No changes detected" on a day with twenty commits, a usage report
whose model split was empty and whose GCP line leaked a Python import error,
an Auto-Dream brief produced without writing a single memory file. None of
that shows up in the scheduler's status column.

Two layers, kept apart like everywhere else in this harness:

* **Deterministic contract checks** read straight off each job's own prompt:
  the exact branding header, the "[SILENT]" convention, the keys a report must
  carry, the tool calls it must have made, the artifact it must have written,
  the errors it must never leak, the length a phone can read.
* **A blinded judge** reads the job's contract and the evidence of what the run
  did (tool calls, files touched, status) beside the text it delivered, and
  rules on quality with the same discipline as goal progress: planted controls,
  cited verdicts, void on a missed control.

The evidence is the session itself (``state.db``) joined to the scheduler's
execution record (``executions.db``); nothing here re-runs a job.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .model import FAIL, PASS, SKIP, Outcome, Verdict

logger = logging.getLogger(__name__)

SUITE_ID = "cron_quality"

# How a run was asked. Bumped whenever the contracts, the control policy or
# the counting rule changes, so the ledger never compares unlike runs.
PROBE_MODE = "cron_quality/daily-jobs/blinded-judge/v1"

# A WhatsApp message a person reads on a phone. Beyond this the prompt's own
# "extremely concise" rule is being ignored.
MOBILE_MAX_CHARS = 2200

# Text that must never reach the owner's chat: it means a script failed and
# the model pasted the failure instead of saying the data was unavailable.
LEAKED_ERROR_MARKERS = (
    "Traceback",
    "No module named",
    "Error querying",
    "Exception:",
    "errno",
)


@dataclass
class JobContract:
    """What a job's own prompt promises, in checkable form."""

    name_contains: str
    header: tuple = ()  # exact leading lines, or empty when the prompt sets none
    silent_token: str = ""  # a whole-output token the prompt allows instead
    required_substrings: tuple = ()
    forbidden_tools: tuple = ()
    min_tool_calls: int = 0
    required_tools: tuple = ()  # at least one call to each of these
    artifact_glob: str = ""  # strftime-expanded against the previous month
    max_chars: int = MOBILE_MAX_CHARS
    judge_hint: str = ""  # what "production grade" means for this job


CONTRACTS: tuple = (
    JobContract(
        name_contains="Auto-Dream",
        header=("*🤫 Hussh One* · *Auto-Dream Daemon*",
                "======================================"),
        min_tool_calls=4,
        required_tools=("patch",),
        forbidden_tools=("send_message", "write_file"),
        judge_hint=(
            "Phase 1 must have actually consolidated: patches to MEMORY.md, "
            "procedures.md, index.json and the dream journal, never write_file "
            "on an existing file. The brief must reflect the conversations in "
            "the dump (specific, dated), carry ONE dream narrative teaser and "
            "ONE honest vision line, and stay short."
        ),
    ),
    JobContract(
        name_contains="Board Sync",
        header=("*🤫 Hussh One* · *Board Sync*",
                "======================================"),
        forbidden_tools=("send_message",),
        judge_hint=(
            "The report must summarise what the sync script actually did "
            "(tickets updated, sprint state, blockers), from the injected "
            "script output, with brief spaced bullets; no dense paragraphs, no "
            "invented tickets, no claims the evidence does not support."
        ),
    ),
    JobContract(
        name_contains="Wiki Maintenance",
        header=("*🤫 Hussh One* · *Wiki Maintenance*",
                "======================================"),
        silent_token="[SILENT]",
        # The judge's first pass failed a plausible-looking report for skipping
        # the mandatory discovery; the prompt now makes the evidence visible
        # in the text itself, so the contract check can see it too.
        required_substrings=("Commits in the last 36h:", "Wiki scan:"),
        forbidden_tools=("send_message",),
        min_tool_calls=2,
        judge_hint=(
            "The run must have discovered what changed (git log / diff on the "
            "repository, a broad wiki search) before deciding. '[SILENT]' is "
            "only honest when nothing shipped; on a day with commits, a report "
            "that says nothing changed contradicts the evidence. Updated pages "
            "must be named with live wiki URLs, private ones marked 🔒."
        ),
    ),
    JobContract(
        name_contains="Token Usage",
        header=("🤫 Hussh One", "Usage Daemon [S]", "════════════════════"),
        required_substrings=(
            "*Today:*", "*Weekly:*", "*Monthly:*", "*Cost:*", "*I/O tokens:*",
            "*Cache read:*", "*Sessions:*", "*Model split (monthly):*",
            "*AI budget (Gemini project only):*", "*Basis:*",
        ),
        forbidden_tools=("send_message",),
        judge_hint=(
            "Every number must come from the injected JSON, humanised as the "
            "prompt says; the model split must list models (by tokens when "
            "cost is $0), and an unavailable data source must be stated plainly, "
            "never as a pasted Python error."
        ),
    ),
    JobContract(
        name_contains="timesheet",
        artifact_glob="~/Desktop/Timesheets_and_Reimbursements/*{prev_month_name}{prev_year}*.xlsx",
        min_tool_calls=1,
        judge_hint=(
            "The previous month's reimbursement workbook must exist at the "
            "stated path and the reply must name it; no fabricated totals."
        ),
    ),
)


@dataclass
class JobRun:
    """One scheduled execution joined to the session that served it."""

    job_id: str
    name: str
    session_id: str
    model: str
    status: str
    claimed_at: str
    finished_at: str
    error: str
    prompt: str
    final_text: str
    tool_calls: list = field(default_factory=list)  # (name, args dict)
    files_written: list = field(default_factory=list)
    deliver: str = ""
    duration_s: Optional[float] = None

    @property
    def tool_names(self) -> list:
        return [name for name, _ in self.tool_calls]


def contract_for(name: str) -> Optional[JobContract]:
    lowered = (name or "").lower()
    for contract in CONTRACTS:
        if contract.name_contains.lower() in lowered:
            return contract
    return None


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _parse_iso(value: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _load_jobs(jobs_path: Path) -> dict:
    try:
        payload = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {str(j.get("id")): j for j in payload.get("jobs") or [] if j.get("id")}


def _tool_calls(raw: Any) -> list:
    """``[(name, args)]`` from a stored ``tool_calls`` column."""
    if not raw:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return []
    out = []
    for call in calls or []:
        function = (call or {}).get("function") or {}
        name = function.get("name")
        if not name:
            continue
        args = function.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001
                args = {"__raw__": args}
        out.append((name, args if isinstance(args, dict) else {}))
    return out


def _written_path(name: str, args: dict) -> Optional[str]:
    if name not in ("write_file", "patch"):
        return None
    for key in ("path", "file_path", "target", "file"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def collect_runs(
    since_epoch: float,
    *,
    jobs_path: Optional[Path] = None,
    executions_db: Optional[Path] = None,
    state_db: Optional[Path] = None,
    until_epoch: Optional[float] = None,
) -> list:
    """Every agent-driven cron execution since ``since_epoch``, with evidence.

    Script-only jobs (``no_agent``) have no model output to grade and are
    skipped. An execution whose session cannot be found is kept with an empty
    transcript: a run that delivered nothing is a finding, not a gap.
    """
    home = hermes_home()
    jobs = _load_jobs(jobs_path or home / "cron" / "jobs.json")
    ex_path = executions_db or home / "cron" / "executions.db"
    st_path = state_db or home / "state.db"
    if not Path(ex_path).exists() or not Path(st_path).exists():
        return []

    runs: list = []
    ex = sqlite3.connect(f"file:{ex_path}?mode=ro", uri=True)
    st = sqlite3.connect(f"file:{st_path}?mode=ro", uri=True)
    try:
        rows = ex.execute(
            "select job_id, status, claimed_at, coalesce(finished_at,''), "
            "coalesce(error,'') from executions order by claimed_at"
        ).fetchall()
        for job_id, status, claimed_at, finished_at, error in rows:
            claimed = _parse_iso(claimed_at)
            if claimed is None or claimed < since_epoch:
                continue
            if until_epoch is not None and claimed > until_epoch:
                continue
            job = jobs.get(str(job_id))
            if not job or job.get("no_agent"):
                continue
            finished = _parse_iso(finished_at) if finished_at else None
            session = st.execute(
                "select id, coalesce(model,''), started_at from sessions "
                "where id like ? and started_at >= ? and started_at <= ? "
                "order by started_at limit 1",
                (f"cron_{job_id}_%", claimed - 5, (finished or claimed) + 120),
            ).fetchone()
            session_id, model, tool_calls, final_text = "", "", [], ""
            if session:
                session_id, model, _started = session
                messages = st.execute(
                    "select role, coalesce(content,''), coalesce(tool_calls,'') "
                    "from messages where session_id = ? order by timestamp, id",
                    (session_id,),
                ).fetchall()
                for role, content, raw_calls in messages:
                    if role != "assistant":
                        continue
                    tool_calls.extend(_tool_calls(raw_calls))
                    if content.strip():
                        final_text = content
            files = [
                path for name, args in tool_calls
                if (path := _written_path(name, args))
            ]
            runs.append(
                JobRun(
                    job_id=str(job_id),
                    name=str(job.get("name") or job_id),
                    session_id=session_id,
                    model=model or str(job.get("model") or ""),
                    status=str(status),
                    claimed_at=str(claimed_at),
                    finished_at=str(finished_at),
                    error=str(error),
                    prompt=str(job.get("prompt") or ""),
                    final_text=final_text,
                    tool_calls=tool_calls,
                    files_written=files,
                    deliver=str(job.get("deliver") or ""),
                    duration_s=(round(finished - claimed, 1)
                                if finished is not None else None),
                )
            )
    finally:
        ex.close()
        st.close()
    return runs


# --------------------------------------------------------------------------
# Deterministic contract checks
# --------------------------------------------------------------------------


def _previous_month(now: Optional[datetime] = None) -> datetime:
    today = (now or datetime.now()).replace(day=1)
    return today - timedelta(days=1)


def _artifact_exists(pattern: str, now: Optional[datetime] = None) -> tuple:
    previous = _previous_month(now)
    expanded = pattern.format(
        prev_month_name=previous.strftime("%B"),
        prev_month=previous.strftime("%m"),
        prev_year=previous.strftime("%Y"),
    )
    expanded = str(Path(expanded).expanduser())
    parent = Path(expanded).parent
    if not parent.exists():
        return False, expanded
    for candidate in parent.iterdir():
        if fnmatch.fnmatch(str(candidate), expanded):
            return True, str(candidate)
    return False, expanded


def grade(run: JobRun, *, now: Optional[datetime] = None) -> Verdict:
    """Deterministic contract outcomes for one run."""
    verdict = Verdict(case_id=run.session_id or f"{run.job_id}@{run.claimed_at}", suite=SUITE_ID)
    contract = contract_for(run.name)
    text = (run.final_text or "").strip()
    lines = text.splitlines()

    verdict.outcomes.append(
        Outcome("job_completed", PASS if run.status == "completed" else FAIL,
                run.error[:200] if run.status != "completed" else "")
    )
    if run.status != "completed":
        verdict.indeterminate = f"job {run.status}: {run.error[:80]}"
        return verdict
    if not text:
        verdict.outcomes.append(Outcome("delivered_text", FAIL, "the run produced no final text"))
        return verdict
    verdict.outcomes.append(Outcome("delivered_text", PASS))

    if contract is None:
        verdict.outcomes.append(Outcome("contract_known", SKIP, "no contract for this job"))
        return verdict

    silent = bool(contract.silent_token) and text == contract.silent_token
    if contract.header:
        if silent:
            verdict.outcomes.append(Outcome("header_exact", SKIP, "silent run"))
        else:
            head = [line.strip() for line in lines[: len(contract.header)]]
            ok = head == [line.strip() for line in contract.header]
            verdict.outcomes.append(Outcome(
                "header_exact", PASS if ok else FAIL,
                "" if ok else f"starts with {head[:2]!r}, contract wants {list(contract.header)[:2]!r}",
            ))
    for needle in contract.required_substrings:
        if silent:
            break
        present = needle in text
        verdict.outcomes.append(Outcome(
            f"has:{needle}", PASS if present else FAIL,
            "" if present else f"missing {needle!r}",
        ))
    leaked = [m for m in LEAKED_ERROR_MARKERS if m.lower() in text.lower()]
    verdict.outcomes.append(Outcome(
        "no_leaked_error", FAIL if leaked else PASS,
        f"delivered text contains {leaked[0]!r}" if leaked else "",
    ))
    used_forbidden = [t for t in run.tool_names if t in contract.forbidden_tools]
    verdict.outcomes.append(Outcome(
        "no_forbidden_tool", FAIL if used_forbidden else PASS,
        f"called {used_forbidden[0]}" if used_forbidden else "",
    ))
    if contract.min_tool_calls and not silent:
        enough = len(run.tool_calls) >= contract.min_tool_calls
        verdict.outcomes.append(Outcome(
            "did_work", PASS if enough else FAIL,
            "" if enough else f"{len(run.tool_calls)} tool call(s), contract needs {contract.min_tool_calls}",
        ))
    for tool in contract.required_tools:
        if silent:
            break
        called = tool in run.tool_names
        verdict.outcomes.append(Outcome(
            f"called:{tool}", PASS if called else FAIL,
            "" if called else f"never called {tool}",
        ))
    if contract.artifact_glob:
        exists, where = _artifact_exists(contract.artifact_glob, now)
        verdict.outcomes.append(Outcome(
            "artifact_exists", PASS if exists else FAIL,
            where if exists else f"nothing matches {where}",
        ))
    within = len(text) <= contract.max_chars
    verdict.outcomes.append(Outcome(
        "mobile_brevity", PASS if within else FAIL,
        "" if within else f"{len(text):,} chars, limit {contract.max_chars:,}",
    ))
    return verdict


def summarize(runs: Sequence[JobRun], verdicts: Sequence[Verdict]) -> dict:
    """Per-job deterministic outcome table."""
    per_job: dict = {}
    for run, verdict in zip(runs, verdicts):
        failures = [o.name for o in verdict.outcomes if o.outcome == FAIL]
        per_job.setdefault(run.name, []).append({
            "session_id": run.session_id,
            "claimed_at": run.claimed_at,
            "status": run.status,
            "duration_s": run.duration_s,
            "tool_calls": len(run.tool_calls),
            "files_written": run.files_written,
            "contract_failures": failures,
            "indeterminate": verdict.indeterminate,
        })
    return {"suite": SUITE_ID, "runs": len(runs), "per_job": per_job}


# --------------------------------------------------------------------------
# The blinded queue and its report
# --------------------------------------------------------------------------


CONTRACT_EXCERPT_CHARS = 2600
OUTPUT_CHARS = 4000


def _evidence(run: JobRun) -> str:
    calls = ", ".join(
        f"{name}({', '.join(f'{k}={str(v)[:60]!r}' for k, v in list(args.items())[:2])})"
        for name, args in run.tool_calls[:12]
    ) or "none"
    files = ", ".join(run.files_written[:8]) or "none"
    return (
        f"status={run.status}; duration={run.duration_s}s; "
        f"tool calls ({len(run.tool_calls)}): {calls}; files written: {files}"
    )


def _utterance(run: JobRun) -> str:
    contract = contract_for(run.name)
    hint = contract.judge_hint if contract else ""
    return (
        f"JOB: {run.name}\n\n"
        f"WHAT PRODUCTION GRADE MEANS HERE: {hint}\n\n"
        f"THE JOB'S OWN PROMPT (contract, excerpt):\n"
        f"{run.prompt[:CONTRACT_EXCERPT_CHARS]}\n\n"
        f"EVIDENCE OF WHAT THE RUN DID: {_evidence(run)}"
    )


def build_rows(runs: Sequence[JobRun]) -> tuple:
    rows, identity = [], {}
    for run in runs:
        if run.status != "completed" or not run.final_text.strip():
            continue  # a failed or empty run is a deterministic finding already
        row_id = f"{run.job_id}@{run.session_id}"
        rows.append({
            "id": row_id,
            "utterance": _utterance(run),
            "output": {"action": "", "assistant_text": run.final_text[:OUTPUT_CHARS]},
        })
        identity[row_id] = {
            "job_id": run.job_id, "name": run.name,
            "session_id": run.session_id, "model": run.model,
        }
    return rows, identity


def negative_controls(rows: Sequence[dict], identity: dict, *, count: int = 3) -> list:
    """A row whose delivered text belongs to a DIFFERENT job.

    A board-sync report under the wiki job's contract, or a dream brief under
    the usage report's, cannot be production grade for that job; a judge who
    passes one is not reading the contract.
    """
    controls: list = []
    by_job: dict = {}
    for row in rows:
        by_job.setdefault(identity[row["id"]]["name"], []).append(row)
    names = sorted(by_job)
    if len(names) < 2:
        return controls
    for index, name in enumerate(names):
        if len(controls) >= count:
            break
        donor_name = names[(index + 1) % len(names)]
        base, donor = by_job[name][0], by_job[donor_name][0]
        controls.append({
            "id": f"control-swap-{len(controls)}",
            "utterance": base["utterance"],
            "output": donor["output"],
            "must_catch": f"the delivered text is {donor_name}'s output under {name}'s contract",
        })
    return controls


def write_jobs_queue(
    runs: Sequence[JobRun],
    *,
    out_dir: Path | str,
    seal_path: Path | str,
    identity_path: Path | str,
    run_id: Optional[str] = None,
):
    from hermes_cli.hussh_one_pkm.judge_queue import write_queue

    rows, raw_identity = build_rows(runs)
    if not rows:
        raise ValueError("no completed runs with output to judge")
    controls = negative_controls(rows, raw_identity)
    identity = {f"c{index:03d}": raw_identity[row["id"]] for index, row in enumerate(rows)}
    queued = write_queue(
        out_dir=Path(out_dir),
        cases=rows,
        answerer_model="blinded:daily-jobs",
        run_id=run_id,
        controls=controls,
        positive_controls=[],
        capability_profile={"probe_mode": PROBE_MODE},
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
    """Ingest the graded queue: per-job judged quality, and what was wrong."""
    from hermes_cli.hussh_one_pkm.judge_queue import ingest
    from hermes_cli.hussh_one_routing import stats

    identity = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    graded = ingest(
        out_dir=Path(out_dir), judge_label=judge_label, seal_path=Path(seal_path)
    )
    if graded.void:
        return {"void": True, "void_reason": graded.void_reason, "judge": judge_label}

    per_job: dict = {}
    per_model: dict = {}
    for case in graded.cases:
        who = identity.get(case.case_id)
        if not who or case.control is not None or not case.counted:
            continue
        bucket = per_job.setdefault(
            who["name"], {"graded": 0, "production_grade": 0, "failures": []}
        )
        bucket["graded"] += 1
        model_bucket = per_model.setdefault(
            who.get("model") or "unknown",
            {"graded": 0, "production_grade": 0, "judged_failures": []},
        )
        model_bucket["graded"] += 1
        if case.verdict == "correct":
            bucket["production_grade"] += 1
            model_bucket["production_grade"] += 1
        else:
            failure = {
                "session_id": who["session_id"], "job": who["name"],
                "rule": case.rule, "citation": case.citation, "note": case.note,
                "verdict": case.verdict,
            }
            bucket["failures"].append(failure)
            model_bucket["judged_failures"].append({
                "case_id": who["session_id"], "row_id": case.case_id,
                "rule": case.rule or case.verdict, "citation": case.citation,
                "note": f"[{who['name']}] {case.note}",
            })
    for bucket in list(per_job.values()) + list(per_model.values()):
        bucket["quality"] = stats.describe(bucket["production_grade"], bucket["graded"])
    return {
        "void": False,
        "suite": SUITE_ID,
        "judge": judge_label,
        "per_job": per_job,
        "per_model": per_model,
        "caveat": (
            "Judged against each job's own prompt and the evidence of what the "
            "run did. Deterministic contract checks are reported separately by "
            "`hermes puppy jobs collect`; neither number is the other."
        ),
    }


def append_to_ledger(result: dict, *, ledger_path: Optional[Path | str] = None,
                     timestamp: Optional[int] = None) -> dict:
    """One evolution-ledger row per model, in the judge_queue row shape."""
    from hermes_cli.hussh_one_pkm import judge_queue as JQ

    at = int(timestamp if timestamp is not None else time.time())
    path = Path(ledger_path) if ledger_path is not None else JQ.default_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with path.open("a", encoding="utf-8") as handle:
        for model, bucket in sorted((result.get("per_model") or {}).items()):
            row = {
                "schema_version": JQ.SCHEMA_VERSION,
                "at": at,
                "answerer_model": model,
                "judge": result.get("judge"),
                "capability_profile": {"probe_mode": PROBE_MODE, "suite": SUITE_ID},
                "host": {},
                "benchmark": {},
                "scoreboard": {
                    "metric": "cron_quality",
                    "accuracy": bucket["quality"]["rate"],
                    "n": bucket["quality"]["n"],
                    "ci95": bucket["quality"]["ci95"],
                },
                "void": bool(result.get("void")),
                "void_reason": result.get("void_reason", ""),
            }
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            rows.append(row)
    return {"path": str(path), "rows": rows}


def write_judged_failures(result: dict, *, directory: Optional[Path | str] = None,
                          timestamp: Optional[int] = None) -> dict:
    """Judged job failures beside the model's playbook, suite ``cron_quality``."""
    from hermes_cli.hussh_one_routing.exam import goal_progress as GP

    if result.get("void"):
        return {}
    shaped = {
        "void": False,
        "judge": result.get("judge"),
        "per_model": {
            model: {"judged_failures": bucket.get("judged_failures") or []}
            for model, bucket in (result.get("per_model") or {}).items()
        },
    }
    at = int(timestamp if timestamp is not None else time.time())
    written: dict = {}
    for model, bucket in shaped["per_model"].items():
        rows = bucket["judged_failures"]
        if not rows:
            continue
        path = GP.judged_failures_path(model, directory=directory)
        seen = {(r.get("case_id"), r.get("rule")) for r in GP._read_jsonl(path)}
        path.parent.mkdir(parents=True, exist_ok=True)
        new_rows = 0
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                key = (row["case_id"], row["rule"])
                if key in seen:
                    continue
                seen.add(key)
                handle.write(json.dumps(
                    {**row, "suite": SUITE_ID, "judge": result.get("judge"), "at": at},
                    sort_keys=True,
                ) + "\n")
                new_rows += 1
        written[model] = {"path": str(path), "new_rows": new_rows}
    return written
