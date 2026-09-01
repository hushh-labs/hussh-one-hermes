# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Grading merge-conflict resolutions.

Built around a real failure. Given one real conflict, gemma-4-26b-a4b-qat chose
the right side semantically and still produced an unusable result: first line at
indent 0 against a body at indent 8, and the surrounding context re-emitted so a
block appeared twice after splicing. Neither needed a judge to catch.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing.suites import merge_conflict as M


# The context deliberately shares no substantial line with either side. A
# fixture whose context repeats a resolution line makes every clean answer trip
# the duplication check, and the suite would look broken when it is not.
CONFLICTED = '''\
def route(target, platform):
    if platform == "email":
        return target.lower(), None, True
<<<<<<< HEAD
    if platform == "signal":
        return f"signal:{target}", None, True
=======
    if platform == "whatsapp":
        return target.strip(), None, True
>>>>>>> upstream/main
    return None, None, False
'''

OURS = '    if platform == "signal":\n        return f"signal:{target}", None, True\n'
THEIRS = '    if platform == "whatsapp":\n        return target.strip(), None, True\n'


@pytest.fixture
def case(tmp_path):
    path = tmp_path / "route.py"
    path.write_text(CONFLICTED)
    return M.extract_cases(path)[0], CONFLICTED


class TestExtraction:
    def test_it_finds_the_hunk_and_splits_both_sides(self, case):
        c, _ = case
        assert c.ours.strip().startswith('if platform == "signal"')
        assert c.theirs.strip().startswith('if platform == "whatsapp"')

    def test_it_finds_every_hunk_in_a_multi_conflict_file(self, tmp_path):
        text = CONFLICTED + "\n" + CONFLICTED
        path = tmp_path / "two.py"
        path.write_text(text)
        assert len(M.extract_cases(path)) == 2

    def test_an_unterminated_conflict_is_not_reported_as_a_hunk(self):
        # A half-written marker block is not a case; treating it as one would
        # feed the model a prompt with no "theirs" side at all.
        assert M.find_conflicts("<<<<<<< HEAD\nx = 1\n") == []

    def test_a_clean_file_yields_nothing(self):
        assert M.find_conflicts("x = 1\ny = 2\n") == []


class TestB1MarkersGone:
    def test_a_resolution_that_leaves_markers_fails_immediately(self, case):
        c, original = case
        v = M.grade("<<<<<<< HEAD\nx = 1\n", c, original)
        assert v.failed_check == "markers-left"
        assert v.markers_gone is False
        # The rule must be one the merge suite defines, or the run voids.
        from hermes_cli.hussh_one_pkm.integrity import rules_for

        assert set(v.rules) <= rules_for("merge")


class TestB2SplicesAndParses:
    def test_broken_indentation_is_caught(self, case):
        c, original = case
        # The exact observed shape: first line flush left, body indented.
        broken = 'if platform == "whatsapp":\n        return target, None, True\n'
        v = M.grade(broken, c, original)
        assert v.failed_check == "broken-structure"
        assert "unindent" in v.detail or "indent" in v.detail

    def test_a_correct_region_parses_once_spliced(self, case):
        c, original = case
        v = M.grade(THEIRS, c, original)
        assert v.splices_and_parses is True
        assert v.failed_check == ""

    def test_the_fragment_is_graded_spliced_not_in_isolation(self, case):
        c, original = case
        # This region is valid only inside the function. Judged alone it is a
        # stray indented block, and failing it would be the harness's fault.
        spliced = M.splice(THEIRS, c, original)
        assert "def route" in spliced
        assert M.CONFLICT_START not in spliced


class TestB4NoDuplication:
    def test_re_emitting_the_surrounding_context_is_caught(self, case):
        c, original = case
        # What the model actually did: returned context plus resolution.
        echoed = c.pre + THEIRS
        v = M.grade(echoed, c, original)
        assert v.failed_check == "duplicated-region"
        assert "re-emitted" in v.detail

    def test_short_recurring_lines_do_not_false_positive(self, case):
        c, original = case
        # ")" and "else:" legitimately recur; flagging them would make the
        # check useless on real code.
        v = M.grade(THEIRS + "    )\n", c, original)
        assert v.failed_check != "duplicated-region"


class TestB3SideClassification:
    def test_taking_theirs_is_recognised(self, case):
        c, _ = case
        assert M.classify_side(THEIRS, c) == M.SIDE_THEIRS

    def test_taking_ours_is_recognised(self, case):
        c, _ = case
        assert M.classify_side(OURS, c) == M.SIDE_OURS

    def test_keeping_both_is_a_union(self, case):
        c, _ = case
        assert M.classify_side(OURS + THEIRS, c) == M.SIDE_UNION

    def test_a_rewrite_is_synthesis_not_a_side(self, case):
        c, _ = case
        rewritten = '    if platform in ("signal", "whatsapp"):\n        return target.strip(), None, True\n'
        assert M.classify_side(rewritten, c) in (M.SIDE_SYNTHESIS, M.SIDE_NEITHER)

    def test_dropping_everything_is_neither(self, case):
        c, _ = case
        assert M.classify_side("    pass\n", c) == M.SIDE_NEITHER

    def test_a_shared_line_does_not_decide_the_side(self, tmp_path):
        # Both sides contain "return None, None, False"; counting shared lines
        # would classify every resolution as a union.
        text = (
            "def f():\n"
            "<<<<<<< HEAD\n"
            "    a = 1\n"
            "    return None, None, False\n"
            "=======\n"
            "    b = 2\n"
            "    return None, None, False\n"
            ">>>>>>> upstream/main\n"
        )
        path = tmp_path / "shared.py"
        path.write_text(text)
        c = M.extract_cases(path)[0]
        assert M.classify_side("    b = 2\n    return None, None, False\n", c) == (
            M.SIDE_THEIRS
        )


class TestTheJudgeOnlySeesWhatDeterminismCannot:
    def test_a_reference_match_needs_no_judge(self, case):
        c, original = case
        c.reference_side = M.SIDE_THEIRS
        v = M.grade(THEIRS, c, original)
        assert v.reference_match is True
        assert v.needs_judge is False

    def test_a_different_but_clean_resolution_goes_to_the_judge(self, case):
        c, original = case
        c.reference_side = M.SIDE_THEIRS
        v = M.grade(OURS, c, original)
        # Not a failure. The shipped resolution is one correct answer, not the
        # only one, so this is exactly the question determinism cannot settle.
        assert v.deterministically_ok is True
        assert v.reference_match is False
        assert v.needs_judge is True

    def test_a_structurally_broken_answer_never_reaches_the_judge(self, case):
        c, original = case
        c.reference_side = M.SIDE_THEIRS
        v = M.grade("if x:\n        y = 1\n", c, original)
        assert v.needs_judge is False


class TestSummaryKeepsTheNumbersApart:
    def test_reference_match_and_stage_counts_are_separate(self, case):
        c, original = case
        c.reference_side = M.SIDE_THEIRS
        verdicts = [M.grade(THEIRS, c, original), M.grade(OURS, c, original)]
        summary = M.summarize(verdicts)
        assert summary["deterministically_ok"] == 2
        assert summary["reference_match"] == 1
        assert summary["needs_judge"] == 1
        # No blended score: adding reference_match to judge results would
        # assert the shipped resolution is the only correct one.
        assert not any(
            isinstance(v, float) and 0 < v < 1 for v in summary.values()
        )

    def test_failed_checks_are_named_not_counted_into_a_rate(self, case):
        c, original = case
        verdicts = [M.grade("<<<<<<< HEAD\n", c, original)]
        assert M.summarize(verdicts)["failed_checks"] == ["markers-left"]


class TestEveryRuleIsValidForTheMergeSuite:
    def test_no_oracle_can_void_a_run_by_citing_an_unknown_rule(self, case):
        # The whole point of suite-scoped rules: an oracle citing a rule the
        # contract does not define voids the entire run at ingest.
        from hermes_cli.hussh_one_pkm.integrity import rules_for

        c, original = case
        answers = [
            "<<<<<<< HEAD\nx\n",
            "if x:\n        y = 1\n",
            c.pre + THEIRS,
        ]
        for answer in answers:
            for rule in M.grade(answer, c, original).rules:
                assert rule in rules_for("merge"), rule
