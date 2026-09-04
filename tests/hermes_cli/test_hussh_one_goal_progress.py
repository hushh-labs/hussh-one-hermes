# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The goal-progress dimension: judged on-path or off-path, blinded, sealed.

These run the real queue machinery end to end in a temp directory: write the
blinded queue, grade it through the sanctioned writer, ingest, and report per
model. The founder's critique this answers: structural validity was standing in
for goal achievement, and nothing measured whether the action advanced the
goal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.hussh_one_pkm import verdict_cli
from hermes_cli.hussh_one_routing.exam import goal_progress as GP


def artifact(path: Path, model_slug: str, records: list) -> Path:
    file = path / f"corrected_{model_slug}.jsonl"
    file.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return file


def record(case_id, chosen_tool, chosen_args, reference_tool, reference_args,
           *, indeterminate="", tail="find the config file for the gateway"):
    return {
        "case_id": case_id,
        "user_request_tail": tail,
        "reference_tool": reference_tool,
        "reference_args": reference_args,
        "chosen_tool": chosen_tool,
        "chosen_args": chosen_args,
        "assistant_text": "working on it",
        "indeterminate": indeterminate,
        "oracles": [],
        "label_match": chosen_tool == reference_tool,
    }


@pytest.fixture
def artifacts(tmp_path):
    model_a = artifact(tmp_path, "alpha_model-a", [
        record("c1", "search_files", {"pattern": "config"},
               "search_files", {"pattern": "config"}),          # byte-equal
        record("c2", "terminal", {"command": "ls /tmp"},
               "read_file", {"path": "gateway.yaml"}),
        record("c3", None, None, "read_file", {"path": "gateway.yaml"}),
        record("c4", "read_file", {"path": "a.py"},
               "read_file", {"path": "a.py"}, indeterminate="timeout"),
    ])
    model_b = artifact(tmp_path, "beta_model-b", [
        record("c1", "read_file", {"path": "gateway.yaml"},
               "search_files", {"pattern": "config"}),
        record("c2", "web_search", {"query": "gateway config"},
               "read_file", {"path": "gateway.yaml"}),
    ])
    return [model_a, model_b]


class TestRowsAreBlindAndLabelled:
    def test_identity_is_held_apart_from_the_rows(self, artifacts):
        rows, identity = GP.build_rows(artifacts)
        blob = json.dumps(rows)
        assert "alpha/model-a" not in blob and "beta/model-b" not in blob
        assert {v["model"] for v in identity.values()} == {
            "alpha/model-a", "beta/model-b"
        }

    def test_indeterminate_rows_are_excluded(self, artifacts):
        # A timeout says nothing about goal progress.
        rows, identity = GP.build_rows(artifacts)
        assert len(rows) == 5
        assert not any(v["case_id"] == "c4" for v in identity.values())

    def test_the_reference_is_labelled_not_ground_truth(self, artifacts):
        rows, _ = GP.build_rows(artifacts)
        assert all("NOT ground truth" in r["utterance"] for r in rows)

    def test_a_no_tool_turn_is_still_a_row(self, artifacts):
        # Calling nothing is judgeable: the stalls rule exists for it.
        rows, _ = GP.build_rows(artifacts)
        assert any("(no tool call" in r["output"]["action"] for r in rows)


class TestControls:
    def test_negative_controls_swap_in_a_different_tool(self, artifacts):
        rows, _ = GP.build_rows(artifacts)
        negatives = GP.negative_controls(rows, count=2)
        assert negatives, "no negative controls built"
        for control in negatives:
            base = next(r for r in rows if r["utterance"] == control["utterance"])
            assert control["output"]["action"].split(" ", 1)[0] != base["output"][
                "action"
            ].split(" ", 1)[0]
            assert "must_catch" in control

    def test_a_donor_equal_to_the_base_reference_is_never_used(self, tmp_path):
        # Found live: a swapped-in action that happens to equal the base row's
        # REFERENCE builds a control that is on-path by construction while
        # labelled must-catch, and it voids the run of any judge diligent
        # enough to notice. The builder must skip such donors even when they
        # are the only different-tool candidates.
        poisoned = artifact(tmp_path, "solo_model", [
            record("c1", "read_file", {"path": "g.yaml"},
                   "search_files", {"pattern": "config"}),
            record("c2", "search_files", {"pattern": "config"},
                   "read_file", {"path": "g.yaml"}),
        ])
        rows, _ = GP.build_rows([poisoned])
        for control in GP.negative_controls(rows, count=4):
            base = next(
                r for r in rows if r["utterance"] == control["utterance"]
            )
            assert control["output"]["action"] not in base["utterance"]

    def test_a_same_domain_donor_is_never_used(self, tmp_path):
        # Found live: control c128 planted a skill_view of a skill the base
        # request itself listed onto a skills-curation request. On a curation
        # task, viewing any listed skill IS on-path, so the control voided a
        # grader who had correctly passed the same action shape on the real
        # curation rows. Same-family and shared-entity donors must be skipped.
        curation_tail = (
            "curate the skills inventory: - hushh-engineering-board-sync "
            "state=active - xurl state=active - yuanbao state=active"
        )
        poisoned = artifact(tmp_path, "solo_model", [
            record("c1", "skills_list", {}, "skills_list", {},
                   tail=curation_tail),
            record("c2", "skill_view", {"name": "hushh-engineering-board-sync"},
                   "read_file", {"path": "b.md"}, tail="compare the branches"),
            record("c3", "terminal", {"command": "python3 board-sync.py"},
                   "read_file", {"path": "c.md"}, tail="run the report"),
            record("c4", "web_search", {"query": "weather in kirkland"},
                   "read_file", {"path": "d.md"}, tail="what is the weather"),
        ])
        rows, _ = GP.build_rows([poisoned])
        base_utterance = next(
            r["utterance"] for r in rows if "curate the skills" in r["utterance"]
        )
        for control in GP.negative_controls(rows, count=8):
            if control["utterance"] != base_utterance:
                continue
            action = control["output"]["action"]
            # skill_view is the same tool family as the base's own skills_list,
            # and the terminal donor names board-sync, an entity the request
            # lists. Neither may be planted on the curation request.
            assert not action.startswith("skill_view"), action
            assert "board-sync" not in action, action

    def test_a_generic_recon_donor_is_never_used(self, tmp_path):
        # Found live: a locate-the-repo `find`, the real correct opening move
        # on one request, was planted on a different request and voided a
        # grader whose reasoned verdict was on-path -- generic bootstrap
        # recon advances MOST requests, so it makes an unwinnable control.
        # The measurable signature: the same byte-identical action produced
        # for more than one distinct request.
        recon = ("terminal", {"command": "find ~ -name repo -type d"})
        poisoned = artifact(tmp_path, "solo_model", [
            record("c1", *recon, "read_file", {"path": "a.md"},
                   tail="catalog the components"),
            record("c2", *recon, "read_file", {"path": "b.md"},
                   tail="gather context on the items board"),
            record("c3", "web_search", {"query": "weather in kirkland"},
                   "read_file", {"path": "c.md"}, tail="what is the weather"),
            record("c4", "skill_view", {"name": "some-skill"},
                   "read_file", {"path": "d.md"}, tail="inspect that skill"),
        ])
        rows, _ = GP.build_rows([poisoned])
        for control in GP.negative_controls(rows, count=8):
            assert "find ~ -name repo" not in control["output"]["action"], (
                "a request-agnostic recon action was planted as a control"
            )

    def test_control_bases_are_content_seeded_and_deterministic(self, artifacts):
        rows, _ = GP.build_rows(artifacts)
        first = GP.negative_controls(rows, count=3)
        second = GP.negative_controls(rows, count=3)
        assert first == second

    def test_positive_controls_are_byte_equal_to_the_reference(self, artifacts):
        rows, identity = GP.build_rows(artifacts)
        positives = GP.positive_controls(rows, identity, artifacts)
        assert len(positives) == 1  # only c1 on model-a matches byte for byte
        assert "must_not_flag" in positives[0]


class TestTheRuleVocabularyIsClosed:
    def test_the_suite_rules_are_registered(self):
        from hermes_cli.hussh_one_pkm.integrity import rules_for

        assert rules_for("goal_progress") == {
            "wrong-object", "dead-end", "redundant",
            "destructive-detour", "stalls",
        }


def grade_everything(run, identity_path, *, spare_negatives=False):
    """Grade like a diligent judge: catch the swaps, pass the rest.

    The queue is blinded, so the test tells reals from controls the way the
    design intends a judge to be unable to shortcut it: reals are known only
    via the identity map (which the grader must not have), and the planted
    swaps are recognisable by content, since a swapped action does not match
    the reference continuation shown in the utterance.
    """
    run_dir = run.queue_path.parent
    real_ids = set(json.loads(Path(identity_path).read_text(encoding="utf-8")))
    for line in run.queue_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        action = row["output"]["action"]
        is_control = row["id"] not in real_ids
        looks_on_path = action in row["utterance"]
        if is_control and not looks_on_path and not spare_negatives:
            # The writer verifies the citation verbatim against the stored row,
            # and the stored output is JSON, so a citation containing double
            # quotes never matches its escaped form. Cite the quote-free head
            # of the action (the tool name and opening brace).
            verdict_cli.record(
                run_dir=run_dir, row_id=row["id"], verdict="wrong",
                rule="dead-end",
                citation=action.split('"')[0].strip() or action.split(" ", 1)[0],
                note="action lifted from an unrelated request",
            )
        else:
            verdict_cli.record(
                run_dir=run_dir, row_id=row["id"], verdict="correct",
                rule="", citation="", note="",
            )


class TestEndToEnd:
    def _queue(self, artifacts, tmp_path):
        out = tmp_path / "run"
        seal = tmp_path / "secrets" / "seal.json"
        identity = tmp_path / "secrets" / "identity.json"
        run = GP.write_goal_queue(
            artifact_files=artifacts, out_dir=out,
            seal_path=seal, identity_path=identity,
        )
        return run, seal, identity

    def test_identity_lives_outside_the_run_directory(self, artifacts, tmp_path):
        run, _seal, identity = self._queue(artifacts, tmp_path)
        assert identity.exists()
        assert run.queue_path.parent not in identity.parents

    def test_a_diligent_judge_yields_per_model_rates(self, artifacts, tmp_path):
        run, seal, identity = self._queue(artifacts, tmp_path)
        grade_everything(run, identity)
        result = GP.report(
            out_dir=run.queue_path.parent, seal_path=seal,
            identity_path=identity, judge_label="test-judge",
        )
        assert result["void"] is False
        assert result["per_model"]["alpha/model-a"]["graded"] == 3
        assert result["per_model"]["beta/model-b"]["graded"] == 2
        for bucket in result["per_model"].values():
            assert bucket["goal_progress"]["ci95"]
        assert "never added" in result["caveat"]

    def test_a_missed_negative_control_voids_the_run(self, artifacts, tmp_path):
        # A judge that waves the planted swap through is rubber-stamping, and
        # no rate survives that.
        run, seal, identity = self._queue(artifacts, tmp_path)
        grade_everything(run, identity, spare_negatives=True)
        result = GP.report(
            out_dir=run.queue_path.parent, seal_path=seal,
            identity_path=identity, judge_label="test-judge",
        )
        assert result["void"] is True
        assert "per_model" not in result

    def test_unsure_counts_against_the_rate(self, artifacts, tmp_path):
        run, seal, identity = self._queue(artifacts, tmp_path)
        run_dir = run.queue_path.parent
        real_ids = set(json.loads(identity.read_text(encoding="utf-8")))
        first_real = None
        for line in run.queue_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            is_control = row["id"] not in real_ids
            looks_on_path = row["output"]["action"] in row["utterance"]
            if is_control and not looks_on_path:
                action = row["output"]["action"]
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="wrong",
                    rule="dead-end",
                    citation=action.split('"')[0].strip() or action.split(" ", 1)[0],
                    note="swap",
                )
            elif first_real is None and not is_control:
                first_real = row["id"]
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="unsure",
                    rule="", citation="", note="cannot tell",
                )
            else:
                verdict_cli.record(
                    run_dir=run_dir, row_id=row["id"], verdict="correct",
                    rule="", citation="", note="",
                )
        result = GP.report(
            out_dir=run_dir, seal_path=seal,
            identity_path=identity, judge_label="test-judge",
        )
        identity_map = json.loads(identity.read_text(encoding="utf-8"))
        hedged_model = identity_map[first_real]["model"]
        bucket = result["per_model"][hedged_model]
        assert bucket["on_path"] < bucket["graded"]


class TestTheRunIsRecorded:
    """A rate nobody can compare to the next model's is a number, not a
    trend. Every report lands in the evolution ledger in the judge_queue row
    shape, so ``compare_runs`` works on it unchanged, and every judged
    off-path turn lands beside the model's playbook for the learning loop."""

    def _result(self):
        return {
            "void": False,
            "judge": "fable-5.1",
            "models": ["a/one", "b/two"],
            "per_model": {
                "a/one": {
                    "on_path": 3,
                    "graded": 4,
                    "goal_progress": {"rate": 0.75, "n": 4,
                                      "ci95": [0.3, 0.95], "width": 0.65},
                    "off_path_rules": {"dead-end": 1},
                    "judged_failures": [{
                        "case_id": "s#1", "row_id": "c003", "rule": "dead-end",
                        "citation": "ls /x", "note": "n",
                    }],
                },
                "b/two": {
                    "on_path": 4,
                    "graded": 4,
                    "goal_progress": {"rate": 1.0, "n": 4,
                                      "ci95": [0.51, 1.0], "width": 0.49},
                    "off_path_rules": {},
                    "judged_failures": [],
                },
            },
        }

    def test_one_ledger_row_per_model_in_the_shared_shape(self, tmp_path):
        from hermes_cli.hussh_one_pkm import judge_queue as JQ

        ledger = tmp_path / "ledger.jsonl"
        out = GP.append_to_ledger(self._result(), ledger_path=ledger, timestamp=5)
        rows = JQ.read_ledger(ledger)
        assert [r["answerer_model"] for r in rows] == ["a/one", "b/two"]
        assert rows[0]["scoreboard"]["accuracy"] == 0.75
        assert rows[0]["capability_profile"]["probe_mode"] == GP.PROBE_MODE
        assert rows[0]["judge"] == "fable-5.1" and rows[0]["at"] == 5
        assert out["path"] == str(ledger) and len(out["rows"]) == 2

    def test_two_runs_of_the_same_model_compare(self, tmp_path):
        from hermes_cli.hussh_one_pkm import judge_queue as JQ

        ledger = tmp_path / "ledger.jsonl"
        GP.append_to_ledger(self._result(), ledger_path=ledger, timestamp=1)
        later = self._result()
        later["per_model"]["a/one"]["goal_progress"]["rate"] = 1.0
        GP.append_to_ledger(later, ledger_path=ledger, timestamp=2)
        comparison = JQ.compare_runs(ledger, model="a/one")
        assert comparison["comparable"] is True
        assert comparison["delta"] == 0.25

    def test_a_void_run_is_recorded_per_model_without_a_rate(self, tmp_path):
        from hermes_cli.hussh_one_pkm import judge_queue as JQ

        ledger = tmp_path / "ledger.jsonl"
        void = {"void": True, "void_reason": "missed control", "judge": "j",
                "models": ["a/one", "b/two"]}
        GP.append_to_ledger(void, ledger_path=ledger)
        rows = JQ.read_ledger(ledger)
        assert [r["answerer_model"] for r in rows] == ["a/one", "b/two"]
        assert all(r["void"] and r["scoreboard"] == {} for r in rows)
        # And compare_runs ignores it rather than inventing a trend.
        assert JQ.compare_runs(ledger, model="a/one")["comparable"] is False

    def test_judged_failures_land_beside_the_playbook_and_dedupe(self, tmp_path):
        written = GP.write_judged_failures(
            self._result(), directory=tmp_path, timestamp=7
        )
        assert set(written) == {"a/one"}
        again = GP.write_judged_failures(
            self._result(), directory=tmp_path, timestamp=8
        )
        assert again["a/one"]["new_rows"] == 0
        loaded = GP.load_judged_failures("a/one", directory=tmp_path)
        assert list(loaded) == ["s#1"]
        assert loaded["s#1"][0]["rule"] == "dead-end"
        assert loaded["s#1"][0]["judge"] == "fable-5.1"
        assert GP.load_judged_failures("b/two", directory=tmp_path) == {}

    def test_a_void_run_writes_no_judged_failures(self, tmp_path):
        assert GP.write_judged_failures({"void": True}, directory=tmp_path) == {}
