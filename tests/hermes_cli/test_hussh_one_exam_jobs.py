# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Grading what the daily cron jobs delivered, not whether they exited 0.

A job can complete, deliver, and still be wrong. These pin the join from the
scheduler's execution record to the session that served it, the contract
checks each job's own prompt implies, and the blinded queue that lets a
separate session judge the quality of the delivered text.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli import puppy_cmd as PC
from hermes_cli.hussh_one_pkm import verdict_cli
from hermes_cli.hussh_one_pkm.integrity import rules_for
from hermes_cli.hussh_one_routing.exam import jobs as J
from hermes_cli.hussh_one_routing.exam.model import FAIL, PASS

HEADER = "*🤫 Hussh One* · *Board Sync*\n======================================\n\n"


def _run(name="Hushh Core Board Sync", text=HEADER + "• Synced 3 tickets\n\n• No blockers",
         status="completed", tool_calls=None, error="", job_id="j1"):
    tool_calls = tool_calls if tool_calls is not None else []
    return J.JobRun(
        job_id=job_id, name=name, session_id=f"cron_{job_id}_20260902_031000",
        model="google/gemma-4-26b-a4b-qat", status=status,
        claimed_at="2026-09-02T03:10:00-07:00", finished_at="2026-09-02T03:12:00-07:00",
        error=error, prompt="Start exactly with this 3-line header ...",
        final_text=text, tool_calls=tool_calls,
        files_written=[p for n, a in tool_calls if (p := J._written_path(n, a))],
        deliver="local,whatsapp:owner", duration_s=120.0,
    )


def _fails(verdict):
    return {o.name: o.detail for o in verdict.outcomes if o.outcome == FAIL}


class TestContractChecks:
    def test_a_clean_report_passes_everything(self):
        assert _fails(J.grade(_run())) == {}

    def test_the_branding_header_is_exact(self):
        verdict = J.grade(_run(text="Board Sync\n=====\n• stuff"))
        assert "header_exact" in _fails(verdict)

    def test_a_failed_execution_is_indeterminate_not_a_text_failure(self):
        verdict = J.grade(_run(status="failed", error="RuntimeError: Model unloaded.", text=""))
        assert verdict.indeterminate.startswith("job failed")
        assert _fails(verdict) == {"job_completed": "RuntimeError: Model unloaded."}

    def test_a_completed_run_with_no_text_is_a_failure(self):
        assert "delivered_text" in _fails(J.grade(_run(text="")))

    def test_a_leaked_python_error_fails(self):
        text = HEADER + "• Whole-GCP spend: Error querying BigQuery: No module named 'google.cloud'"
        assert "no_leaked_error" in _fails(J.grade(_run(text=text)))

    def test_send_message_is_forbidden_where_the_prompt_says_so(self):
        verdict = J.grade(_run(tool_calls=[("send_message", {"to": "group"})]))
        assert _fails(verdict)["no_forbidden_tool"] == "called send_message"

    def test_auto_dream_must_patch_not_write(self):
        text = "*🤫 Hussh One* · *Auto-Dream Daemon*\n======================================\n\n• x"
        lazy = J.grade(_run(name="Auto-Dream Consolidated Suite", text=text, tool_calls=[]))
        assert {"did_work", "called:patch"} <= set(_fails(lazy))
        destructive = J.grade(_run(
            name="Auto-Dream Consolidated Suite", text=text,
            tool_calls=[("write_file", {"path": "/x/MEMORY.md"})] * 4,
        ))
        assert "no_forbidden_tool" in _fails(destructive)
        good = J.grade(_run(
            name="Auto-Dream Consolidated Suite", text=text,
            tool_calls=[("read_file", {"path": "/x"})] + [("patch", {"path": "/x/MEMORY.md"})] * 4,
        ))
        assert _fails(good) == {}

    def test_wiki_silent_token_is_allowed_whole(self):
        verdict = J.grade(_run(name="Hushh Wiki Maintenance Follow-on", text="[SILENT]",
                               tool_calls=[("terminal", {"command": "git log"})]))
        assert _fails(verdict) == {}

    def test_wiki_report_needs_its_own_header(self):
        verdict = J.grade(_run(name="Hushh Wiki Maintenance Follow-on", text=HEADER + "• nothing",
                               tool_calls=[("terminal", {"command": "git log"})]))
        assert "header_exact" in _fails(verdict)

    def test_usage_report_requires_every_key(self):
        text = "🤫 Hussh One\nUsage Daemon [S]\n════════════════════\n\n*Today:*\n\n*Cost:*\n$0"
        missing = {n for n in _fails(J.grade(_run(name="Hussh One Token Usage Report", text=text)))
                   if n.startswith("has:")}
        assert "has:*Model split (monthly):*" in missing

    def test_a_novel_full_length_message_fails_brevity(self):
        assert "mobile_brevity" in _fails(J.grade(_run(text=HEADER + "x" * 3000)))

    def test_the_timesheet_artifact_is_checked_for_the_previous_month(self, tmp_path, monkeypatch):
        folder = tmp_path / "Timesheets_and_Reimbursements"
        folder.mkdir()
        (folder / "Reimbursement_Tracker_Kushal_August2026.xlsx").write_bytes(b"x")
        monkeypatch.setattr(J, "CONTRACTS", (
            J.JobContract(name_contains="timesheet",
                          artifact_glob=str(folder / "*{prev_month_name}{prev_year}*.xlsx"),
                          min_tool_calls=1),
        ))
        run = _run(name="monthly-timesheet-generator", text="saved", tool_calls=[("terminal", {})])
        assert _fails(J.grade(run, now=datetime(2026, 9, 2))) == {}
        assert "artifact_exists" in _fails(J.grade(run, now=datetime(2026, 10, 2)))

    def test_the_rule_vocabulary_is_registered(self):
        assert "contradicts-evidence" in rules_for("cron_quality")
        assert "leaked-error" in rules_for("cron_quality")

    def test_a_citation_with_an_emoji_is_still_found(self):
        # Every branded job header carries an emoji and a middle dot. The
        # writer used to compare against ASCII-escaped JSON, so an honest
        # citation of such a line was rejected as unfound.
        row = {"output": {"action": "", "assistant_text": "*🤫 Hussh One* · *Board Sync*\n===="}}
        assert verdict_cli._citation_present("*🤫 Hussh One* · *Board Sync*", row)


def _databases(tmp_path):
    ex = tmp_path / "executions.db"
    st = tmp_path / "state.db"
    con = sqlite3.connect(ex)
    con.execute("create table executions (id text primary key, job_id text, source text, "
                "process_id text, pid integer, process_started_at integer, status text, "
                "claimed_at text, started_at text, finished_at text, error text)")
    con.execute("insert into executions values ('e1','j1','builtin','p',1,0,'completed',"
                "'2026-09-02T03:10:00-07:00','2026-09-02T03:10:00-07:00',"
                "'2026-09-02T03:12:00-07:00',null)")
    con.execute("insert into executions values ('e2','j2','builtin','p',1,0,'failed',"
                "'2026-09-02T04:00:00-07:00','2026-09-02T04:00:00-07:00',"
                "'2026-09-02T04:00:05-07:00','RuntimeError: Model unloaded.')")
    con.execute("insert into executions values ('e3','j3','builtin','p',1,0,'completed',"
                "'2026-09-02T05:00:00-07:00',null,'2026-09-02T05:00:05-07:00',null)")
    con.commit(); con.close()
    claimed = datetime.fromisoformat("2026-09-02T03:10:00-07:00").timestamp()
    con = sqlite3.connect(st)
    con.execute("create table sessions (id text primary key, source text, model text, "
                "started_at real, ended_at real, message_count integer, tool_call_count integer, end_reason text)")
    con.execute("create table messages (id integer primary key, session_id text, role text, "
                "content text, tool_call_id text, tool_calls text, tool_name text, timestamp real)")
    con.execute("insert into sessions values ('cron_j1_20260902_031002','cron','google/gemma-4-26b-a4b-qat',?,?,4,1,'cron_complete')",
                (claimed + 2, claimed + 100))
    calls = json.dumps([{"function": {"name": "patch", "arguments": json.dumps({"path": "/tmp/MEMORY.md"})}}])
    con.executemany("insert into messages (session_id, role, content, tool_calls, timestamp) values (?,?,?,?,?)", [
        ("cron_j1_20260902_031002", "user", "prompt...", "", claimed + 2),
        ("cron_j1_20260902_031002", "assistant", "", calls, claimed + 10),
        ("cron_j1_20260902_031002", "tool", '{"bytes_written": 5}', "", claimed + 11),
        ("cron_j1_20260902_031002", "assistant", HEADER + "• Synced 3 tickets", "", claimed + 90),
    ])
    con.commit(); con.close()
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        {"id": "j1", "name": "Hushh Core Board Sync", "prompt": "Start exactly with this 3-line header", "deliver": "local"},
        {"id": "j2", "name": "Hushh Wiki Maintenance Follow-on", "prompt": "wiki", "deliver": "local"},
        {"id": "j3", "name": "Hussh One Self-Healing Doctor", "prompt": "", "no_agent": True},
    ]}), encoding="utf-8")
    return jobs, ex, st


class TestCollection:
    def test_executions_join_their_sessions_and_scripts_are_skipped(self, tmp_path):
        jobs, ex, st = _databases(tmp_path)
        runs = J.collect_runs(0, jobs_path=jobs, executions_db=ex, state_db=st)
        assert [r.job_id for r in runs] == ["j1", "j2"]  # j3 is no_agent
        board = runs[0]
        assert board.session_id == "cron_j1_20260902_031002"
        assert board.tool_names == ["patch"]
        assert board.files_written == ["/tmp/MEMORY.md"]
        assert board.final_text.startswith(HEADER)
        assert board.duration_s == 120.0
        failed = runs[1]
        assert failed.status == "failed" and failed.session_id == ""

    def test_the_window_is_honoured(self, tmp_path):
        jobs, ex, st = _databases(tmp_path)
        late = datetime.fromisoformat("2026-09-02T03:30:00-07:00").timestamp()
        runs = J.collect_runs(late, jobs_path=jobs, executions_db=ex, state_db=st)
        assert [r.job_id for r in runs] == ["j2"]


class TestQueueAndReport:
    def test_a_blinded_queue_round_trips_through_the_report(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runs = [
            _run(job_id="j1"),
            _run(job_id="j2", name="Hushh Wiki Maintenance Follow-on",
                 text="*🤫 Hussh One* · *Wiki Maintenance*\n======================================\n\n• No changes detected.",
                 tool_calls=[("terminal", {"command": "ls"})]),
        ]
        run_dir = tmp_path / "jobs-run"
        seal = tmp_path / "secrets" / "seal.json"
        identity = tmp_path / "secrets" / "identity.json"
        queued = J.write_jobs_queue(runs, out_dir=run_dir / "run", seal_path=seal, identity_path=identity)
        assert queued.row_count == 2 + queued.control_count
        assert queued.control_count >= 1  # the two jobs swap outputs
        real = json.loads(identity.read_text(encoding="utf-8"))
        for line in (run_dir / "run" / "review-queue.jsonl").read_text().splitlines():
            row = json.loads(line)
            header_line = row["output"]["assistant_text"].splitlines()[0]
            job_line = row["utterance"].splitlines()[0]
            is_control = row["id"] not in real
            wiki_under_wiki = "Wiki" in header_line and "Wiki" in job_line
            board_under_board = "Board" in header_line and "Board" in job_line
            if is_control and not (wiki_under_wiki or board_under_board):
                verdict_cli.record(run_dir=run_dir / "run", row_id=row["id"], verdict="wrong",
                                   rule="format-contract", citation=header_line, note="another job's output")
            elif not is_control and real[row["id"]]["name"].startswith("Hushh Wiki"):
                verdict_cli.record(run_dir=run_dir / "run", row_id=row["id"], verdict="wrong",
                                   rule="contradicts-evidence", citation="No changes detected",
                                   note="only ran ls; never looked at git")
            else:
                verdict_cli.record(run_dir=run_dir / "run", row_id=row["id"], verdict="correct")
        args = argparse.Namespace(
            jobs_command="report", out=str(run_dir), seal=str(seal), identity=str(identity),
            judge="test-judge", ledger=str(tmp_path / "ledger.jsonl"),
        )
        assert PC._cmd_jobs(args) == PC.EXIT_OK
        printed = json.loads(capsys.readouterr().out)
        assert printed["void"] is False
        assert printed["per_job"]["Hushh Core Board Sync"]["quality"]["rate"] == 1.0
        wiki = printed["per_job"]["Hushh Wiki Maintenance Follow-on"]
        assert wiki["quality"]["rate"] == 0.0
        assert wiki["failures"][0]["rule"] == "contradicts-evidence"
        assert printed["ledger"]["rows"] == 1
        rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
        assert rows[0]["capability_profile"]["probe_mode"] == J.PROBE_MODE
        judged = tmp_path / "puppy-playbooks" / "google_gemma-4-26b-a4b-qat" / "judged_failures.jsonl"
        assert judged.exists() and "contradicts-evidence" in judged.read_text()

    def test_parsing_and_since_forms(self):
        top = argparse.ArgumentParser()
        sub = top.add_subparsers(dest="command")
        PC.build_puppy_parser(sub)
        args = top.parse_args(["puppy", "jobs", "collect", "--out", "d", "--seal", "s", "--identity", "i"])
        assert args.since == "24h"
        with pytest.raises(SystemExit):
            top.parse_args(["puppy", "jobs", "report", "--out", "d", "--seal", "s", "--identity", "i"])
        assert PC._parse_since("2026-09-02T03:00:00-07:00") == datetime.fromisoformat("2026-09-02T03:00:00-07:00").timestamp()
        assert PC._parse_since("24h") < PC._parse_since("1h")
