# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The stronger-model judge.

A judge that rubber-stamps is worse than no judge, because it manufactures
evidence. Every test here is about a way this one could quietly stop measuring
anything while still producing a number.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_pkm import judge as J


def _verdict(verdict, rule="", citation="", note=""):
    return json.dumps(
        {"verdict": verdict, "rule": rule, "citation": citation, "note": note}
    )


def _always(response):
    return lambda _prompt: response


GOOD_CASE = {
    "id": "diet",
    "utterance": "I stopped eating dairy in January.",
    "output": {
        "domain": "health",
        "scope_path": "health.diet.restrictions",
        "merge_patch": {"dairy": "avoided"},
        "summary": "Stopped eating dairy in January.",
    },
}


class TestTheJudgeMayNotBeTheAnswerer:
    def test_identical_models_are_refused(self):
        # The compaction eval in this repo routes judge, answerer and
        # question-generator through one call_llm(task="compression"), so the
        # model grades itself. That number looks like a measurement and is not
        # one, and this is the guard that stops it happening here.
        with pytest.raises(J.JudgeIsTheAnswerer):
            J.assert_distinct_models("gemma-4-26b", "gemma-4-26b")

    def test_case_and_whitespace_do_not_smuggle_a_self_grade_through(self):
        with pytest.raises(J.JudgeIsTheAnswerer):
            J.assert_distinct_models("  Gemma-4-26B ", "gemma-4-26b")

    def test_an_absent_judge_is_refused_rather_than_defaulted(self):
        with pytest.raises(J.JudgeIsTheAnswerer):
            J.assert_distinct_models("", "gemma-4-26b")

    def test_distinct_models_are_allowed(self):
        J.assert_distinct_models("claude-opus-5", "gemma-4-26b-a4b-qat")

    def test_run_judgement_refuses_before_spending_a_single_call(self):
        calls = []

        def _counting(prompt):
            calls.append(prompt)
            return _verdict(J.VERDICT_CORRECT)

        with pytest.raises(J.JudgeIsTheAnswerer):
            J.run_judgement(
                judge_model="m",
                answerer_model="m",
                cases=[GOOD_CASE],
                ask_judge=_counting,
            )
        assert calls == []


class TestNegativeControlsGateTheRun:
    def test_a_judge_that_passes_planted_failures_voids_the_run(self):
        # Every control breaks a rule the agent's instruction states in plain
        # words. A judge that waves them through is not being lenient, it is
        # not reading, and nothing it said about the real cases is worth having.
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_always(_verdict(J.VERDICT_CORRECT)),
        )
        assert report.void is True
        assert "planted failures" in report.void_reason

    def test_a_void_run_publishes_no_accuracy_at_all(self):
        # Not "accuracy: 0.97, but caveat". A number with a caveat gets quoted
        # without the caveat.
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_always(_verdict(J.VERDICT_CORRECT)),
        )
        board = report.scoreboard()
        assert board["void"] is True
        assert board["accuracy"] is None
        assert "correct" not in board

    def test_a_void_run_yields_no_regression_corpus(self):
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_always(_verdict(J.VERDICT_CORRECT)),
        )
        assert report.regression_corpus() == []

    def test_controls_never_enter_the_score(self):
        # They decide whether the score is trustworthy; they are not part of it.
        def _judge(prompt):
            if "finance.accounts.primary" in prompt or "AA-4471182" in prompt:
                return _verdict(J.VERDICT_WRONG, "right-domain", "finance")
            if "_parser_version" in prompt:
                return _verdict(J.VERDICT_WRONG, "no-metadata", "_parser_version")
            if "call the dentist" in prompt:
                return _verdict(J.VERDICT_WRONG, "durable-only", "call the dentist")
            return _verdict(J.VERDICT_CORRECT)

        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_judge,
        )
        assert report.void is False
        assert report.scoreboard()["graded"] == 1


def _strict_judge(on_real):
    """A judge that catches every control, then answers real cases with on_real."""

    controls = {c["id"]: c for c in J.NEGATIVE_CONTROLS}
    markers = {
        "control-wrong-domain": ("right-domain", "finance"),
        "control-invented-fact": ("no-invention", "AA-4471182"),
        "control-operational-request": ("durable-only", "call the dentist"),
        "control-internal-metadata": ("no-metadata", "_parser_version"),
    }

    def _judge(prompt):
        for cid, control in controls.items():
            if control["utterance"] in prompt and json.dumps(
                control["output"], indent=2, sort_keys=True
            ) in prompt:
                rule, citation = markers[cid]
                return _verdict(J.VERDICT_WRONG, rule, citation)
        return on_real(prompt)

    return _judge


class TestCitationsMustBeReal:
    def test_a_wrong_verdict_citing_text_not_in_the_output_is_discarded(self):
        # Without this the judge can invent the evidence for its own verdict,
        # which is precisely what makes a bad grader worse than none.
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_strict_judge(
                _always(_verdict(J.VERDICT_WRONG, "no-invention", "a passport number"))
            ),
        )
        assert report.void is False
        board = report.scoreboard()
        assert board["discarded_uncited"] == 1
        assert board["failures"] == []

    def test_a_wrong_verdict_quoting_the_output_is_counted(self):
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_strict_judge(
                _always(_verdict(J.VERDICT_WRONG, "right-domain", "health.diet.restrictions"))
            ),
        )
        board = report.scoreboard()
        assert board["discarded_uncited"] == 0
        assert len(board["failures"]) == 1
        assert board["failures"][0]["rule"] == "right-domain"

    def test_a_discarded_case_does_not_reach_the_regression_corpus(self):
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_strict_judge(
                _always(_verdict(J.VERDICT_WRONG, "no-invention", "never appears"))
            ),
        )
        assert report.regression_corpus() == []


class TestScoring:
    def test_unsure_counts_against_accuracy(self):
        # Treating a hedge as a pass lets a judge inflate the score for free.
        hedged = {
            "id": "second",
            "utterance": "I moved to the trust org.",
            "output": {
                "domain": "work",
                "scope_path": "work.role.team",
                "merge_patch": {"team": "trust"},
                "summary": "Moved to the trust org.",
            },
        }
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE, hedged],
            ask_judge=_strict_judge(
                lambda p: _verdict(J.VERDICT_UNSURE)
                if "trust org" in p
                else _verdict(J.VERDICT_CORRECT)
            ),
        )
        board = report.scoreboard()
        assert board["graded"] == 2
        assert board["correct"] == 1
        assert board["accuracy"] == 0.5

    def test_failures_become_reusable_fixtures(self):
        # The compounding part: a failure that is only a number teaches
        # nothing; the same failure as a fixture proves whether the next change
        # actually fixed it.
        report = J.run_judgement(
            judge_model="opus",
            answerer_model="gemma",
            cases=[GOOD_CASE],
            ask_judge=_strict_judge(
                _always(_verdict(J.VERDICT_WRONG, "minimal-patch", "dairy"))
            ),
        )
        corpus = report.regression_corpus()
        assert len(corpus) == 1
        assert corpus[0]["case_id"] == "diet"
        assert corpus[0]["rule"] == "minimal-patch"


class TestVerdictParsing:
    def test_fenced_json_is_accepted(self):
        # Models fence their JSON even when told not to.
        parsed = J.parse_verdict('```json\n{"verdict": "correct"}\n```')
        assert parsed["verdict"] == J.VERDICT_CORRECT

    def test_an_already_parsed_object_is_accepted(self):
        assert J.parse_verdict({"verdict": "wrong", "rule": "r"})["rule"] == "r"

    @pytest.mark.parametrize(
        "raw", ["not json at all", "", None, [], '{"verdict": "excellent"}', "{}"]
    )
    def test_anything_unreadable_becomes_unsure_not_correct(self, raw):
        # Defaulting a broken response to "correct" would turn every judge
        # outage into a perfect score.
        assert J.parse_verdict(raw)["verdict"] == J.VERDICT_UNSURE

    def test_a_judge_that_raises_is_unsure_not_a_crash(self):
        def _explode(_prompt):
            raise TimeoutError("judge is down")

        graded = J.grade_one(
            case_id="c",
            model="gemma",
            utterance="u",
            output={"a": 1},
            ask_judge=_explode,
        )
        assert graded.verdict == J.VERDICT_UNSURE
        assert "TimeoutError" in graded.note


class TestControlsAreRealViolations:
    def test_every_control_declares_what_must_be_caught(self):
        for control in J.NEGATIVE_CONTROLS:
            assert control["must_catch"], control["id"]
            assert control["utterance"]
            assert isinstance(control["output"], dict)

    def test_controls_are_structurally_valid_so_only_semantics_fail_them(self):
        # A control that fails the SHAPE check would be caught by the cheap
        # benchmark and prove nothing about the judge.
        from hermes_cli.hussh_one_pkm.benchmark import score_tool_call

        for control in J.NEGATIVE_CONTROLS:
            payload = {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "save_to_pkm",
                                        "arguments": json.dumps(control["output"]),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
            assert score_tool_call(payload)["valid"] is True, control["id"]


class TestTheAuditorIsNeverALocalModel:
    """After a local model caused a 42-hour outage on 2026-08-28.

    A local model auditing local models is being asked to certify its own
    failure modes, and "the local models checked each other and agreed" is not
    evidence. The strong model is already running; insisting on it costs
    nothing.
    """

    @pytest.mark.parametrize(
        "provider", ["lmstudio", "LM-Studio", "ollama", "lm_studio"]
    )
    def test_a_local_provider_is_refused(self, provider):
        with pytest.raises(J.JudgeIsOnDevice, match="runs on this machine"):
            J.assert_auditor_is_not_local("some-model", provider)

    @pytest.mark.parametrize(
        "model",
        ["google/gemma-4-26b-a4b-qat", "qwen/qwen3.6-35b", "nemotron-3-nano"],
    )
    def test_a_local_looking_model_is_refused_without_a_provider(self, model):
        # Provider is the reliable signal, but a caller that omits it must not
        # thereby smuggle a local model through.
        with pytest.raises(J.JudgeIsOnDevice):
            J.assert_auditor_is_not_local(model)

    def test_the_claude_code_auditor_is_allowed(self):
        J.assert_auditor_is_not_local("claude-code-opus")
        J.assert_auditor_is_not_local("claude-opus-5", "anthropic")

    def test_provider_is_checked_before_the_name_heuristic(self):
        with pytest.raises(J.JudgeIsOnDevice, match="runs on this machine"):
            J.assert_auditor_is_not_local("some-unfamiliar-model", "lmstudio")
