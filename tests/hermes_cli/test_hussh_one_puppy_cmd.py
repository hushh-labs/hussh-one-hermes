# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``hermes puppy`` CLI wiring, previously untested at every layer.

Found 2026-09-01: the real ``hermes puppy replay`` command -- the one a
monthly model refresh actually runs -- hard-coded ``reasoning_effort="low"``
for every model (live and damaging for qwen, inert for gemma) and pinned no
context at all, while every trustworthy number this session produced came
from throwaway scratch scripts that reinvented context pinning by hand each
time. These tests pin the fixed CLI surface down so that gap cannot reopen
silently.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import puppy_cmd as PC
from hermes_cli.hussh_one_pkm import verdict_cli
from hermes_cli.hussh_one_routing.exam import goal_progress as GP


def _parser():
    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="command")
    PC.build_puppy_parser(sub)
    return top


class TestReplayParsing:
    def test_the_new_flags_exist_with_sane_defaults(self):
        args = _parser().parse_args(["puppy", "replay", "some/model"])
        assert args.model == "some/model"
        assert args.artifacts is None
        assert args.context is None
        assert args.no_restart is False
        assert args.assume_loaded is False

    def test_context_and_artifacts_and_no_restart_parse(self):
        args = _parser().parse_args([
            "puppy", "replay", "some/model",
            "--context", "98304", "--artifacts", "out.jsonl", "--no-restart",
        ])
        assert args.context == 98304
        assert args.artifacts == "out.jsonl"
        assert args.no_restart is True

    def test_assume_loaded_parses(self):
        args = _parser().parse_args(
            ["puppy", "replay", "some/model", "--assume-loaded"]
        )
        assert args.assume_loaded is True

    def test_compact_threshold_parses_and_defaults_off(self):
        assert _parser().parse_args(
            ["puppy", "replay", "m"]
        ).compact_threshold is None
        args = _parser().parse_args(
            ["puppy", "replay", "m", "--compact-threshold", "32768"]
        )
        assert args.compact_threshold == 32768


class TestCompactCase:
    """The long-horizon probe: a case run through the real compactor's shape.

    The compressor here is a fake with the production signature, because the
    real one needs a live summary LLM; what these pin is the harness's side
    of the contract -- deep-copied input, recomputed grounding, honest meta.
    """

    class _Compressor:
        def __init__(self, output=None, fallback=False):
            self._output = output
            self._last_summary_fallback_used = fallback
            self._last_summary_error = None
            self.saw = None

        def compress(self, messages, current_tokens=None, force=False):
            self.saw = messages
            assert force is True
            return self._output if self._output is not None else messages[-2:]

    def _case(self):
        from hermes_cli.hussh_one_routing.exam.replay import ReplayCase

        return ReplayCase(
            case_id="c1", session_id="s1",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "read /tmp/old_path.py please"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "now check /tmp/new_path.py"},
            ],
            wire_chars=300_000, catalog=["read_file"],
            expected_tool="read_file", expected_args={"path": "/tmp/new_path.py"},
            known_paths=["/tmp/old_path.py", "/tmp/new_path.py"],
        )

    def test_known_paths_are_recomputed_from_the_compacted_view(self):
        # The summary legitimately drops history; grounding must be judged
        # against what the model can actually see, or paths_grounded punishes
        # the compactor's correct behaviour as a model failure.
        case = self._case()
        new_case, meta = PC.compact_case(case, self._Compressor())
        assert "/tmp/old_path.py" not in new_case.known_paths
        assert "/tmp/new_path.py" in new_case.known_paths
        assert meta["compacted"] is True
        assert meta["messages_after"] == 2

    def test_the_original_case_is_not_mutated(self):
        case = self._case()
        compressor = self._Compressor()
        PC.compact_case(case, compressor)
        assert len(case.messages) == 4
        # And the compressor received a copy, not the case's own list.
        assert compressor.saw is not case.messages

    def test_token_count_reflects_the_compacted_body(self):
        case = self._case()
        new_case, meta = PC.compact_case(case, self._Compressor())
        assert new_case.tokens < case.tokens
        # before/after are measured on the SAME basis (the messages body), so
        # a compactor that drops half the turns shows roughly half the tokens
        # -- and one that touches nothing shows no shrink at all. Comparing a
        # whole-request 'before' against a messages-only 'after' once reported
        # 55k -> 12k on an untouched case.
        assert meta["tokens_before"] > meta["tokens_after"]
        assert meta["tokens_after"] == new_case.tokens

    def test_an_untouched_case_reports_no_shrink(self):
        case = self._case()
        passthrough = self._Compressor(output=list(case.messages))
        _, meta = PC.compact_case(case, passthrough)
        assert meta["tokens_before"] == meta["tokens_after"]

    def test_a_summary_fallback_is_reported_not_hidden(self):
        _, meta = PC.compact_case(
            self._case(), self._Compressor(fallback=True)
        )
        assert meta["summary_fallback_used"] is True


class TestGoalProgressParsing:
    def test_queue_requires_artifacts_out_seal_identity(self):
        with pytest.raises(SystemExit):
            _parser().parse_args(["puppy", "goal-progress", "queue"])

    def test_queue_parses_with_everything_supplied(self):
        args = _parser().parse_args([
            "puppy", "goal-progress", "queue",
            "--artifacts", "a.jsonl", "b.jsonl",
            "--out", "run", "--seal", "s.json", "--identity", "i.json",
        ])
        assert args.artifacts == ["a.jsonl", "b.jsonl"]
        assert args.out == "run"

    def test_report_requires_judge(self):
        with pytest.raises(SystemExit):
            _parser().parse_args([
                "puppy", "goal-progress", "report",
                "--out", "run", "--seal", "s.json", "--identity", "i.json",
            ])

    def test_report_ledger_is_optional(self):
        base = ["puppy", "goal-progress", "report", "--out", "run",
                "--seal", "s.json", "--identity", "i.json", "--judge", "j"]
        assert _parser().parse_args(base).ledger is None
        assert _parser().parse_args(base + ["--ledger", "l.jsonl"]).ledger == "l.jsonl"


def _artifact(path, model_slug, records):
    file = path / f"corrected_{model_slug}.jsonl"
    file.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return file


def _record(case_id, chosen_tool, chosen_args, reference_tool, reference_args):
    return {
        "case_id": case_id,
        "user_request_tail": "do the thing",
        "reference_tool": reference_tool,
        "reference_args": reference_args,
        "chosen_tool": chosen_tool,
        "chosen_args": chosen_args,
        "assistant_text": "",
        "indeterminate": "",
        "oracles": [],
        "label_match": chosen_tool == reference_tool,
    }


class TestGoalProgressCommandDispatch:
    def test_no_subcommand_is_an_error_not_a_silent_noop(self):
        args = argparse.Namespace(goal_progress_command=None)
        assert PC._cmd_goal_progress(args) == PC.EXIT_ERROR

    def test_queue_refuses_a_missing_artifact_file(self, tmp_path, capsys):
        args = argparse.Namespace(
            goal_progress_command="queue",
            artifacts=[str(tmp_path / "does-not-exist.jsonl")],
            out=str(tmp_path / "run"),
            seal=str(tmp_path / "seal.json"),
            identity=str(tmp_path / "identity.json"),
        )
        assert PC._cmd_goal_progress(args) == PC.EXIT_ERROR
        assert not (tmp_path / "run").exists()
        assert "not found" in capsys.readouterr().err

    def test_queue_then_report_round_trips_through_the_cli(
        self, tmp_path, capsys, monkeypatch
    ):
        # Same fixture shape as the goal_progress module's own tests, driven
        # this time through the CLI layer to prove nothing is lost or
        # renamed crossing that boundary.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        model_a = _artifact(tmp_path, "alpha_model-a", [
            _record("c1", "search_files", {"pattern": "x"},
                    "search_files", {"pattern": "x"}),
            _record("c2", "terminal", {"command": "ls"},
                    "read_file", {"path": "a.py"}),
            # A turn from a cron job that is not active (no jobs file under
            # this HERMES_HOME): graded like any row, excluded from the rate.
            _record("cron_deadbeef0000_20260101#0", "read_file", {"path": "b.py"},
                    "read_file", {"path": "b.py"}),
        ])
        model_b = _artifact(tmp_path, "beta_model-b", [
            _record("c1", "read_file", {"path": "a.py"},
                    "search_files", {"pattern": "x"}),
        ])

        run_dir = tmp_path / "run"
        seal = tmp_path / "secrets" / "seal.json"
        identity = tmp_path / "secrets" / "identity.json"
        queue_args = argparse.Namespace(
            goal_progress_command="queue",
            artifacts=[str(model_a), str(model_b)],
            out=str(run_dir), seal=str(seal), identity=str(identity),
        )
        assert PC._cmd_goal_progress(queue_args) == PC.EXIT_OK
        assert (run_dir / "review-queue.jsonl").exists()
        assert "Grade every row in a DIFFERENT session" in capsys.readouterr().out

        # Grade like a diligent judge: real rows are identifiable only via
        # the identity map (a real grader never sees this); planted swaps are
        # identifiable by content, since a swapped action does not match the
        # reference continuation printed in its own utterance. Proving the
        # CLI's queue/report plumbing here, not goal_progress's own control
        # logic, which has its own dedicated tests.
        identity_map = json.loads(identity.read_text(encoding="utf-8"))
        real_ids = set(identity_map)
        for line in (run_dir / "review-queue.jsonl").read_text().splitlines():
            row = json.loads(line)
            action = row["output"]["action"]
            is_control = row["id"] not in real_ids
            looks_on_path = action in row["utterance"]
            if is_control and not looks_on_path:
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="wrong",
                    rule="dead-end",
                    citation=action.split('"')[0].strip() or action.split(" ", 1)[0],
                    note="planted swap",
                )
            elif not is_control and identity_map[row["id"]]["case_id"] == "c2":
                # One real off-path turn, so the report has something to
                # carry into the ledger and the judged-failures file.
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="wrong",
                    rule="dead-end",
                    citation=action.split(" ", 1)[0],
                    note="listed instead of reading the named file",
                )
            else:
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="correct",
                    rule="", citation="", note="",
                )

        report_args = argparse.Namespace(
            goal_progress_command="report",
            out=str(run_dir), seal=str(seal), identity=str(identity),
            judge="test-judge", ledger=str(tmp_path / "ledger.jsonl"),
        )
        assert PC._cmd_goal_progress(report_args) == PC.EXIT_OK
        printed = json.loads(capsys.readouterr().out)
        assert printed["void"] is False
        assert set(printed["per_model"]) == {"alpha/model-a", "beta/model-b"}

        # The judged off-path turn leaves the report twice: into the evolution
        # ledger (one row per model, comparable by probe mode) and into the
        # file the learning loop reads for exactly this model.
        alpha = printed["per_model"]["alpha/model-a"]
        assert printed["excluded_inactive_cron_cases"] == 1
        assert alpha["graded"] == 2
        assert [f["case_id"] for f in alpha["judged_failures"]] == ["c2"]
        assert alpha["judged_failures"][0]["rule"] == "dead-end"
        rows = [json.loads(line) for line in
                (tmp_path / "ledger.jsonl").read_text().splitlines()]
        assert sorted(r["answerer_model"] for r in rows) == [
            "alpha/model-a", "beta/model-b",
        ]
        assert all(r["capability_profile"]["probe_mode"] == GP.PROBE_MODE
                   for r in rows)
        assert printed["ledger"]["rows"] == 2
        from hermes_cli.hussh_one_routing import loop_replay as LR

        assert LR.load_judged("alpha/model-a")["c2"][0]["rule"] == "dead-end"
        assert LR.load_judged("beta/model-b") == {}


class TestDefaultLadderReflectsTheFinalPick:
    def test_only_the_two_shipping_models_remain(self):
        # gemma-4-12b led only agreement (the weakest of the three signals)
        # and was cut once goal progress existed as a number. A future
        # candidate (e.g. a GGUF build of the same base model) is added
        # here, not scattered across call sites.
        assert PC.DEFAULT_LADDER == (
            "google/gemma-4-26b-a4b-qat", "qwen/qwen3.8-27b",
        )


class TestLoopUsesTheReplaySuiteScorer:
    def test_run_round_is_called_with_the_replay_scorer_and_failure_filter(self):
        # Regression: the CLI once let run_round fall back to its generic
        # scorer, which counts reference-disagreement as failure and reported
        # a 0.952-structural model as 0.357 held-out. Pin the wiring by reading
        # the call site itself -- the round needs a live model to execute.
        import inspect
        from hermes_cli.hussh_one_routing import loop_replay as LR

        src = inspect.getsource(PC._cmd_loop)
        assert "score_fn=LR.score" in src
        # Judged verdicts ride along as the one non-structural signal; the
        # shuffled control wraps the same failures_fn, so both arms see them.
        assert "failures_fn=functools.partial(LR.learnable_failures" in src
        assert "judged=judged" in src
        assert callable(LR.score) and callable(LR.learnable_failures)


class TestReplayCanServeThePlaybook:
    """`hermes puppy replay --playbook` answers with what the device serves.

    Held-out structural validity cannot see a tactic about stalling or dead
    ends; a judged with/without pair on one blinded queue can. This is the
    with-playbook half of that pair.
    """

    def test_the_flag_parses_and_defaults_off(self):
        assert _parser().parse_args(["puppy", "replay", "m"]).playbook is False
        assert _parser().parse_args(
            ["puppy", "replay", "m", "--playbook"]
        ).playbook is True

    def test_the_section_lands_in_front_of_the_system_message(self):
        messages = [{"role": "system", "content": "You are Hermes."},
                    {"role": "user", "content": "list files"}]
        out = PC._with_playbook(messages, "# Learned\n- quote paths")
        assert out[0]["content"].startswith("# Learned\n- quote paths")
        assert out[0]["content"].endswith("You are Hermes.")
        assert messages[0]["content"] == "You are Hermes."  # not mutated

    def test_a_case_without_a_system_message_gets_one(self):
        out = PC._with_playbook([{"role": "user", "content": "hi"}], "- tactic")
        assert out[0] == {"role": "system", "content": "- tactic"}
        assert out[1]["role"] == "user"

    def test_no_playbook_means_no_change(self):
        messages = [{"role": "user", "content": "hi"}]
        assert PC._with_playbook(messages, "") == messages


class TestTheCorpusCanBeFrozenAndPinned:
    """The exam reads the live sessions directory, which changes under a run.

    The first matched/control learning pair ran on two different case sets
    because a dump vanished and the cron case-id fix renumbered cases between
    the two launches. `hermes puppy freeze` plus `--dumps` is the answer.
    """

    def test_loop_and_replay_take_a_dumps_directory(self):
        assert _parser().parse_args(["puppy", "loop", "m"]).dumps is None
        assert _parser().parse_args(
            ["puppy", "loop", "m", "--dumps", "/frozen"]
        ).dumps == "/frozen"
        assert _parser().parse_args(
            ["puppy", "replay", "m", "--dumps", "/frozen"]
        ).dumps == "/frozen"

    def test_freeze_copies_active_dumps_and_writes_a_manifest(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "cron").mkdir()
        (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "29a6e1247b31", "enabled": True},
            {"id": "8a9375e7bf7c", "enabled": False},
        ]}), encoding="utf-8")
        source = tmp_path / "sessions"
        source.mkdir()
        for name in (
            "request_dump_a.json",
            "request_dump_b.json",
            "request_dump_cron_29a6e1247b31_20260630_051615_x.json",
            "request_dump_cron_8a9375e7bf7c_20260618_045000_x.json",
        ):
            (source / name).write_text("{}", encoding="utf-8")
        (source / "sessions.json").write_text("{}", encoding="utf-8")  # not a dump
        target = tmp_path / "frozen"
        args = argparse.Namespace(
            puppy_command="freeze", source=str(source), to=str(target)
        )
        assert PC._cmd_freeze(args) == PC.EXIT_OK
        assert sorted(p.name for p in target.glob("request_dump_*.json")) == [
            "request_dump_a.json", "request_dump_b.json",
            "request_dump_cron_29a6e1247b31_20260630_051615_x.json",
        ]
        assert not (target / "sessions.json").exists()
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["count"] == 3 and manifest["source"] == str(source)
        # The disabled job's dump is left out, and the set used is recorded so
        # the frozen corpus reads the same way whatever the jobs file says later.
        assert manifest["active_cron_jobs"] == ["29a6e1247b31"]
        assert manifest["excluded_inactive_cron"] == [
            "request_dump_cron_8a9375e7bf7c_20260618_045000_x.json",
        ]
        assert "--dumps" in capsys.readouterr().out
        # Freezing again is idempotent: nothing is re-copied or clobbered.
        assert PC._cmd_freeze(args) == PC.EXIT_OK
        assert "0 newly copied" in capsys.readouterr().out

    def test_freeze_refuses_an_empty_source(self, tmp_path):
        args = argparse.Namespace(
            puppy_command="freeze", source=str(tmp_path), to=str(tmp_path / "out")
        )
        assert PC._cmd_freeze(args) == PC.EXIT_ERROR
        assert not (tmp_path / "out").exists()
