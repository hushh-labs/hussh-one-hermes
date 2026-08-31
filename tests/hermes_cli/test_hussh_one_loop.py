# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The learning loop, and the ways it is stopped from flattering itself.

A loop that cannot report "this did not work" will always report that it did.
Most of these tests are about that.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing import loop as L
from hermes_cli.hussh_one_routing import playbook as pb
from hermes_cli.hussh_one_routing.exam.model import (
    FAIL,
    HARNESS,
    PASS,
    Outcome,
    Verdict,
)


class _Case:
    def __init__(self, case_id):
        self.case_id = case_id


def verdict(case_id, *, ok=True, indeterminate="", detail="line 3: bad indent"):
    v = Verdict(case_id=case_id, suite="file_edit", indeterminate=indeterminate)
    v.outcomes = [
        Outcome("parses", PASS if ok else FAIL, "" if ok else detail)
    ]
    return v


SPECIFIC = "Splice new_string into the pre-image before parsing, never the fragment alone"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestTheSplitIsStable:
    def test_the_same_case_always_lands_on_the_same_side(self):
        cases = [_Case(f"c{i}") for i in range(40)]
        first_train, first_hold = L.split_cases(cases)
        second_train, second_hold = L.split_cases(cases)
        assert [c.case_id for c in first_hold] == [c.case_id for c in second_hold]
        assert [c.case_id for c in first_train] == [c.case_id for c in second_train]

    def test_both_sides_are_populated(self):
        cases = [_Case(f"c{i}") for i in range(40)]
        train, hold = L.split_cases(cases)
        assert train and hold
        assert len(train) + len(hold) == 40

    def test_a_case_cannot_migrate_out_of_held_out_when_it_starts_failing(self):
        # A split that moves between rounds is the most flattering possible bug.
        cases = [_Case(f"c{i}") for i in range(40)]
        _, hold_a = L.split_cases(cases)
        _, hold_b = L.split_cases(list(reversed(cases)))
        assert {c.case_id for c in hold_a} == {c.case_id for c in hold_b}


class TestScoringUsesOfferedNotGraded:
    def test_an_indeterminate_case_counts_against_the_score(self):
        # A model that answers 4 of 20 and gets 3 right is not a 75% model.
        verdicts = [verdict("a"), verdict("b", indeterminate="truncated")]
        assert L.score(verdicts) == 0.5

    def test_an_empty_run_scores_zero_not_one(self):
        assert L.score([]) == 0.0


class TestTheReflectorSeesOnlyWhatItShould:
    def test_harness_faults_are_withheld(self):
        # A truncated turn says the budget was too small. Asking for a tactic
        # about it produces "be more concise" aimed at a blameless model.
        verdicts = [verdict("a", indeterminate="truncated"), verdict("b", ok=False)]
        offered = L.failures_for_reflection(verdicts)
        assert [f["case_id"] for f in offered] == ["b"]

    def test_passing_cases_are_withheld(self):
        assert L.failures_for_reflection([verdict("a")]) == []

    def test_the_diagnosis_text_is_what_gets_passed(self):
        # The whole reason the loop can work: text, not a number.
        offered = L.failures_for_reflection([verdict("b", ok=False)])
        assert "line 3: bad indent" in offered[0]["asi"]
        assert offered[0]["oracles"] == ["parses"]


class TestARoundThatCannotBeMeasuredIsVoid:
    def test_no_held_out_cases_voids_the_round(self, home):
        result, _ = L.run_round(
            model="m", suite="file_edit", cases=[],
            answer=lambda c, t: verdict(c.case_id),
            reflect=lambda f, t: [],
        )
        assert result.void is True
        assert "held-out" in result.void_reason


class TestOnlyHeldOutGainCounts:
    def _cases(self):
        return [_Case(f"c{i}") for i in range(30)]

    def test_a_real_gain_is_recorded_as_improvement(self, home):
        state = {"round": 0}

        def answer(case, text):
            # Fails until the playbook is non-empty, then passes.
            return verdict(case.case_id, ok=bool(text.strip()))

        def reflect(failures, text):
            return [pb.Bullet(text=SPECIFIC, case_id="c1", suite="file_edit")]

        result, book = L.run_round(
            model="m", suite="file_edit", cases=self._cases(),
            answer=answer, reflect=reflect,
        )
        assert result.accepted == [SPECIFIC]
        assert result.improved is True
        assert result.delta and result.delta > 0

    def test_a_flat_round_is_reported_as_no_gain(self, home):
        def answer(case, text):
            return verdict(case.case_id, ok=case.case_id.endswith("0"))

        def reflect(failures, text):
            return [pb.Bullet(text=SPECIFIC, case_id="c1", suite="file_edit")]

        result, _ = L.run_round(
            model="m", suite="file_edit", cases=self._cases(),
            answer=answer, reflect=reflect,
        )
        assert result.improved is False

    def test_a_gain_below_the_noise_floor_is_not_an_improvement(self):
        assert L.MIN_MEANINGFUL_GAIN > 0

    def test_nothing_to_learn_from_is_a_clean_outcome(self, home):
        result, book = L.run_round(
            model="m", suite="file_edit", cases=[_Case(f"c{i}") for i in range(20)],
            answer=lambda c, t: verdict(c.case_id, ok=True),
            reflect=lambda f, t: pytest.fail("reflector must not be called"),
        )
        assert result.proposed == []
        assert result.void is False

    def test_a_rejected_proposal_does_not_trigger_a_rerun(self, home):
        calls = {"n": 0}

        def answer(case, text):
            calls["n"] += 1
            return verdict(case.case_id, ok=False)

        # Vague bullets are rejected by the playbook, so there is nothing new to
        # measure and the expensive re-run must be skipped.
        result, _ = L.run_round(
            model="m", suite="file_edit", cases=[_Case(f"c{i}") for i in range(20)],
            answer=answer,
            reflect=lambda f, t: [
                pb.Bullet(text="be careful", case_id="c1", suite="file_edit")
            ],
        )
        assert result.accepted == []
        assert result.rejected == ["be careful"]
        assert result.delta is None


class TestTheLoopHasItsOwnNegativeControl:
    def test_shuffling_detaches_each_diagnosis_from_its_case(self):
        failures = [
            {"case_id": "a", "asi": "A failed", "oracles": ["parses"]},
            {"case_id": "b", "asi": "B failed", "oracles": ["idempotent"]},
        ]
        shuffled = L.shuffled_control(failures)
        assert shuffled[0]["case_id"] == "a"
        assert shuffled[0]["asi"] == "B failed"
        assert shuffled[0]["oracles"] == ["idempotent"]

    def test_the_case_ids_are_left_alone(self):
        # Only the evidence moves; the identities must not, or the control also
        # changes which cases the reflector thinks it is looking at.
        failures = [{"case_id": f"c{i}", "asi": f"d{i}", "oracles": []} for i in range(4)]
        assert [f["case_id"] for f in L.shuffled_control(failures)] == [
            "c0", "c1", "c2", "c3"
        ]

    def test_a_single_failure_cannot_be_shuffled(self):
        one = [{"case_id": "a", "asi": "x", "oracles": []}]
        assert L.shuffled_control(one)[0]["asi"] == "x"

    def test_uniform_evidence_makes_the_control_degenerate(self):
        # The real run that taught this: 11 training failures, all
        # paths_grounded. Both arms proposed the same single tactic and moved
        # the held-out score identically, and the harness announced "LOOP FAILS
        # ITS OWN CONTROL" on what was actually the same experiment twice.
        same = [
            {"case_id": f"c{i}", "asi": "paths_grounded: bad path",
             "oracles": ["paths_grounded"]}
            for i in range(11)
        ]
        degenerate, reason = L.control_is_degenerate(same)
        assert degenerate is True
        assert "same diagnosis" in reason

    def test_varied_evidence_makes_a_real_control(self):
        varied = [
            {"case_id": "a", "asi": "shell_parses: unbalanced quote",
             "oracles": ["shell_parses"]},
            {"case_id": "b", "asi": "paths_grounded: invented path",
             "oracles": ["paths_grounded"]},
        ]
        degenerate, reason = L.control_is_degenerate(varied)
        assert degenerate is False
        assert reason == ""

    def test_a_single_failure_cannot_be_a_control(self):
        one = [{"case_id": "a", "asi": "x", "oracles": ["shell_parses"]}]
        assert L.control_is_degenerate(one)[0] is True

    def test_no_failures_cannot_be_a_control(self):
        assert L.control_is_degenerate([])[0] is True

    def test_the_control_exists_at_all(self):
        # If the playbook "improves" on mismatched evidence, the real run's
        # gains mean nothing either. A loop with no way to fail this check is
        # not measured, it is believed.
        assert callable(L.shuffled_control)


class TestPersistenceAcrossRounds:
    def test_the_playbook_survives_a_round(self, home):
        result, book = L.run_round(
            model="m", suite="file_edit",
            cases=[_Case(f"c{i}") for i in range(30)],
            answer=lambda c, t: verdict(c.case_id, ok=bool(t.strip())),
            reflect=lambda f, t: [
                pb.Bullet(text=SPECIFIC, case_id="c1", suite="file_edit")
            ],
        )
        reloaded = pb.load("m", "file_edit")
        assert [b.text for b in reloaded.active_bullets] == [SPECIFIC]
        assert reloaded.round_number == 1

    def test_the_report_serialises(self, home, tmp_path):
        result = L.RoundResult(round_number=1, model="m", suite="file_edit")
        L.write_report(result, tmp_path / "r.json")
        assert (tmp_path / "r.json").exists()
