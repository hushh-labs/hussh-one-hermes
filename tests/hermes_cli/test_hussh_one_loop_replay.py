# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Learning from real session turns rather than from a chore.

The central rule tested here: the loop may learn from structural failures and
must not learn from disagreement with the reference. Teaching to agreement would
train imitation of one recorded frontier run, and the model would get better at
copying a trace instead of doing the job.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing import loop_replay as LR
from hermes_cli.hussh_one_routing.exam.model import (
    COMPACTED,
    FAIL,
    HARNESS,
    PASS,
    Outcome,
    Verdict,
)


def verdict(case_id, *, failures=(), label_match=None, indeterminate=""):
    v = Verdict(case_id=case_id, suite="replay", indeterminate=indeterminate)
    v.label_match = label_match
    v.outcomes = [Outcome(name, FAIL, f"{name} went wrong") for name in failures]
    if not failures:
        v.outcomes = [Outcome("shell_parses", PASS)]
    return v


class TestOnlyStructuralFailuresAreTaught:
    def test_a_broken_command_is_learnable(self):
        found = LR.learnable_failures([verdict("a", failures=["shell_parses"])])
        assert len(found) == 1
        assert found[0]["oracles"] == ["shell_parses"]
        assert "shell_parses went wrong" in found[0]["asi"]

    def test_disagreement_alone_is_not_learnable(self):
        # The label is one frontier trajectory. A model that picks a different
        # tool may be right, and teaching to the label trains imitation.
        found = LR.learnable_failures(
            [verdict("a", failures=["tool_name_correct"], label_match=False)]
        )
        assert found == []

    def test_a_disagreeing_but_broken_turn_teaches_only_the_break(self):
        found = LR.learnable_failures(
            [verdict("a", failures=["tool_name_correct", "shell_parses"],
                     label_match=False)]
        )
        assert found[0]["oracles"] == ["shell_parses"]
        assert "tool_name_correct" not in found[0]["asi"]

    def test_a_timeout_teaches_nothing(self):
        # A tactic written about a timeout comes out as "be more concise" aimed
        # at a model that was mid-sentence.
        assert LR.learnable_failures([verdict("a", indeterminate="timeout")]) == []

    def test_a_compacted_turn_teaches_nothing(self):
        assert LR.learnable_failures([verdict("a", indeterminate="truncated")]) == []

    def test_a_clean_turn_teaches_nothing(self):
        assert LR.learnable_failures([verdict("a")]) == []


class TestTheLoopIsMeasuredOnWhatItLearnsFrom:
    def test_the_score_is_structural(self):
        # Optimising one number and reporting another is how a loop appears to
        # work. The signal taught to must be the signal measured.
        verdicts = [verdict("a"), verdict("b", failures=["shell_parses"])]
        assert LR.score(verdicts) == 0.5

    def test_disagreement_does_not_lower_the_score(self):
        verdicts = [verdict("a", failures=["tool_name_correct"], label_match=False)]
        assert LR.score(verdicts) == 1.0

    def test_the_score_is_over_offered_not_graded(self):
        # A model that times out on half the cases must not score well on the
        # half it answered.
        verdicts = [verdict("a"), verdict("b", indeterminate="timeout")]
        assert LR.score(verdicts) == 0.5

    def test_an_empty_run_scores_zero(self):
        assert LR.score([]) == 0.0

    def test_agreement_is_reported_separately(self):
        verdicts = [
            verdict("a", label_match=True),
            verdict("b", label_match=False, failures=["tool_name_correct"]),
        ]
        assert LR.agreement(verdicts) == 0.5
        assert LR.score(verdicts) == 1.0

    def test_agreement_is_none_when_nothing_is_labelled(self):
        assert LR.agreement([verdict("a")]) is None


class TestRoundSummary:
    def test_it_keeps_both_signals_and_the_fault_split(self):
        before = [
            verdict("a", failures=["shell_parses"]),
            verdict("b", indeterminate="timeout"),
            verdict("c", indeterminate="truncated"),
        ]
        after = [verdict("a"), verdict("b"), verdict("c")]
        rows = LR.summarize_round(before, after)
        assert rows["structural_delta"] > 0
        assert rows["timed_out_before"] == 1
        assert rows["compacted_before"] == 1
        assert rows["learnable_failures"] == 1

    def test_the_caveat_travels_with_the_numbers(self):
        rows = LR.summarize_round([verdict("a")], [verdict("a")])
        assert "imitation" in rows["caveat"]

    def test_judged_evidence_is_counted_apart(self):
        rows = LR.summarize_round(
            [verdict("a"), verdict("b", failures=["shell_parses"])],
            [verdict("a"), verdict("b")],
            judged=JUDGED,
        )
        assert rows["learnable_failures"] == 2
        assert rows["judged_taught"] == 1


JUDGED = {
    "a": [{
        "case_id": "a",
        "rule": "dead-end",
        "citation": "ls /nowhere",
        "note": "lists a path the request never named",
    }]
}


class TestJudgedVerdictsAreLearnable:
    """An independent judge's off-path verdict is the one non-structural
    signal allowed in: it grades this model's own turn against a rule, with a
    citation, so it is truth about the turn rather than imitation of a
    reference."""

    def test_a_judged_off_path_turn_is_learnable(self):
        found = LR.learnable_failures([verdict("a")], judged=JUDGED)
        assert found[0]["oracles"] == ["judge:dead-end"]
        assert "ls /nowhere" in found[0]["asi"]
        assert "never named" in found[0]["asi"]

    def test_judged_rows_attach_only_to_their_own_case(self):
        assert LR.learnable_failures([verdict("b")], judged=JUDGED) == []

    def test_a_judged_timeout_still_teaches_nothing(self):
        found = LR.learnable_failures(
            [verdict("a", indeterminate="timeout")], judged=JUDGED
        )
        assert found == []

    def test_structural_and_judged_evidence_combine_on_one_case(self):
        found = LR.learnable_failures(
            [verdict("a", failures=["shell_parses"])], judged=JUDGED
        )
        assert found[0]["oracles"] == ["shell_parses", "judge:dead-end"]

    def test_without_judged_rows_nothing_changes(self):
        assert LR.learnable_failures([verdict("a")]) == []

    def test_load_judged_reads_beside_the_playbook(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.hussh_one_routing import playbook as pb
        from hermes_cli.hussh_one_routing.exam import goal_progress as GP

        result = {
            "void": False,
            "judge": "j",
            "per_model": {"pub/m": {
                "on_path": 0,
                "graded": 1,
                "goal_progress": {"rate": 0.0, "n": 1, "ci95": [0.0, 0.79],
                                  "width": 0.79},
                "off_path_rules": {"dead-end": 1},
                "judged_failures": list(JUDGED["a"]),
            }},
        }
        GP.write_judged_failures(result)
        GP.write_judged_failures(result)  # the same verdict twice is one failure
        loaded = LR.load_judged("pub/m")
        assert [r["rule"] for r in loaded["a"]] == ["dead-end"]
        assert LR.load_judged("pub/other") == {}
        # The judged file lives in the model's own playbook directory.
        assert (GP.judged_failures_path("pub/m").parent
                == pb.path_for("pub/m", "replay").parent)


class TestTheAnswerer:
    class _Turn:
        def __init__(self, name=None, args=None, indeterminate=False):
            self.tool_calls = (
                [{"function": {"name": name, "arguments": args or "{}"}}]
                if name else []
            )
            self.timed_out = False
            self.truncated = indeterminate
            self.error = ""
            self.indeterminate = indeterminate

    def _case(self):
        return RPCase()

    def test_the_playbook_reaches_the_system_message(self):
        captured = {}

        def fake_complete(**kw):
            captured["messages"] = kw["messages"]
            return self._Turn("terminal", '{"command": "ls"}')

        answer = LR.make_answerer(
            model="m", max_tokens=1000, timeout=10.0, complete_fn=fake_complete
        )
        answer(self._case(), "- always quote paths")
        system = captured["messages"][0]
        assert system["role"] == "system"
        assert "always quote paths" in system["content"]

    def test_the_reasoning_control_is_prepended_too(self):
        captured = {}

        def fake_complete(**kw):
            captured["messages"] = kw["messages"]
            return self._Turn("terminal", '{"command": "ls"}')

        answer = LR.make_answerer(
            model="m", max_tokens=1000, timeout=10.0,
            reasoning_prefix="<|think|>", complete_fn=fake_complete,
        )
        answer(self._case(), "")
        assert "<|think|>" in captured["messages"][0]["content"]

    def test_the_original_case_is_not_mutated(self):
        case = self._case()
        original = case.messages[0]["content"]

        answer = LR.make_answerer(
            model="m", max_tokens=1000, timeout=10.0,
            complete_fn=lambda **kw: self._Turn("terminal", '{"command": "ls"}'),
        )
        answer(case, "- a tactic")
        answer(case, "- a tactic")
        assert case.messages[0]["content"] == original

    def test_an_indeterminate_turn_is_labelled(self):
        answer = LR.make_answerer(
            model="m", max_tokens=1000, timeout=10.0,
            complete_fn=lambda **kw: self._Turn(indeterminate=True),
        )
        assert answer(self._case(), "").indeterminate == "truncated"

    def test_unparseable_arguments_do_not_raise(self):
        answer = LR.make_answerer(
            model="m", max_tokens=1000, timeout=10.0,
            complete_fn=lambda **kw: self._Turn("terminal", "{not json"),
        )
        assert answer(self._case(), "") is not None


def RPCase():
    from hermes_cli.hussh_one_routing.exam.replay import ReplayCase

    return ReplayCase(
        case_id="c1",
        messages=[{"role": "system", "content": "You are Hermes."},
                  {"role": "user", "content": "list the files"}],
        catalog=["terminal", "read_file"],
        schemas={"terminal": {"type": "object",
                              "properties": {"command": {"type": "string"}}}},
        expected_tool="terminal",
    )
