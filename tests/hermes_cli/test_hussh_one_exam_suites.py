# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Tool selection under a large catalog, and long-context behaviour.

Both suites carry a caveat that is more important than their scores, and both
have a negative control without which a lazy strategy scores well.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing.exam import long_context as LC
from hermes_cli.hussh_one_routing.exam import tool_select as TS
from hermes_cli.hussh_one_routing.exam.model import FAIL, PASS, SKIP


def outcome(verdict, name):
    return next(o for o in verdict.outcomes if o.name == name)


CATALOG = ["read_file", "search_files", "terminal", "write_file", "browser_click"]
SCHEMAS = {
    "read_file": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
        "required": ["path"],
    },
    "terminal": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


class TestToolNameChecks:
    def test_the_reference_tool_matches(self):
        v = TS.grade(case_id="c", chosen="read_file", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "tool_name_correct").outcome == PASS
        assert v.label_match is True

    def test_a_different_tool_fails(self):
        v = TS.grade(case_id="c", chosen="terminal", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "tool_name_correct").outcome == FAIL
        assert v.label_match is False

    def test_a_hallucinated_tool_is_a_separate_failure(self):
        # Calling something never offered is a different error from calling the
        # wrong real thing, and the fixes differ.
        v = TS.grade(case_id="c", chosen="read_filez", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "tool_in_catalog").outcome == FAIL
        assert "not among the 5 tools offered" in outcome(v, "tool_in_catalog").detail

    def test_no_catalog_skips_rather_than_failing(self):
        v = TS.grade(case_id="c", chosen="x", expected="y")
        assert outcome(v, "tool_in_catalog").outcome == SKIP


class TestFamilyIsASofterSignal:
    def test_a_wrong_tool_in_the_right_family_is_flagged_separately(self):
        v = TS.grade(case_id="c", chosen="search_files", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "tool_name_correct").outcome == FAIL
        assert outcome(v, "tool_family_match").outcome == PASS

    def test_a_wrong_family_is_a_bigger_miss(self):
        v = TS.grade(case_id="c", chosen="browser_click", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "tool_family_match").outcome == FAIL
        assert "browse action" in outcome(v, "tool_family_match").detail

    def test_an_mcp_prefixed_variant_maps_to_its_family(self):
        assert TS._family_of("mcp_hushh_wiki_wiki_read") == "read"

    def test_an_unknown_tool_skips_rather_than_guessing(self):
        v = TS.grade(case_id="c", chosen="mystery_tool", expected="read_file",
                     catalog=CATALOG + ["mystery_tool"])
        assert outcome(v, "tool_family_match").outcome == SKIP


class TestArgumentValidation:
    def test_valid_arguments_pass(self):
        v = TS.grade(case_id="c", chosen="read_file", arguments={"path": "a.py"},
                     expected="read_file", catalog=CATALOG, schemas=SCHEMAS)
        assert outcome(v, "arguments_valid").outcome == PASS

    def test_a_missing_required_field_fails(self):
        v = TS.grade(case_id="c", chosen="read_file", arguments={},
                     expected="read_file", catalog=CATALOG, schemas=SCHEMAS)
        assert outcome(v, "arguments_valid").outcome == FAIL

    def test_a_wrong_type_fails(self):
        v = TS.grade(case_id="c", chosen="read_file",
                     arguments={"path": "a.py", "offset": "ten"},
                     expected="read_file", catalog=CATALOG, schemas=SCHEMAS)
        assert outcome(v, "arguments_valid").outcome == FAIL

    def test_an_invented_parameter_is_caught_separately(self):
        # Most real schemas do not set additionalProperties:false, so an
        # invented key validates fine and is then silently dropped by the
        # server. The model believes it asked for something it did not.
        v = TS.grade(case_id="c", chosen="read_file",
                     arguments={"path": "a.py", "encoding": "utf8"},
                     expected="read_file", catalog=CATALOG, schemas=SCHEMAS)
        assert outcome(v, "arguments_valid").outcome == PASS
        assert outcome(v, "no_invented_arguments").outcome == FAIL

    def test_a_tool_with_no_offered_schema_skips(self):
        v = TS.grade(case_id="c", chosen="write_file", arguments={"x": 1},
                     expected="write_file", catalog=CATALOG, schemas=SCHEMAS)
        assert outcome(v, "arguments_valid").outcome == SKIP


class TestAbstentionIsTheNegativeControl:
    def test_calling_nothing_when_nothing_was_right_passes(self):
        v = TS.grade(case_id="c", chosen=None, expected=None, catalog=CATALOG)
        assert outcome(v, "abstains_when_no_tool_fits").outcome == PASS

    def test_calling_something_when_nothing_was_right_fails(self):
        # Over-calling is the characteristic small-model failure under a large
        # catalog; without this a model that always calls something scores well.
        v = TS.grade(case_id="c", chosen="terminal", expected=None, catalog=CATALOG)
        assert outcome(v, "abstains_when_no_tool_fits").outcome == FAIL

    def test_it_does_not_apply_when_a_tool_was_expected(self):
        v = TS.grade(case_id="c", chosen="read_file", expected="read_file",
                     catalog=CATALOG)
        assert outcome(v, "abstains_when_no_tool_fits").outcome == SKIP


class TestTheSuiteAdmitsWhatItIs:
    def test_it_states_that_labels_are_not_truth(self):
        # The label is what a frontier model did. A local model that picks
        # better is scored wrong, so this measures imitation, not competence.
        assert any("best tool" in c for c in TS.CANNOT_CATCH)

    def test_it_states_the_catalog_size_confound(self):
        assert any("confounded" in c for c in TS.CANNOT_CATCH)


class TestNeedleConstruction:
    def test_the_needle_cannot_be_guessed(self):
        # A memorable needle can be answered without reading the context, which
        # is how a needle test accidentally measures nothing.
        sentence, token = LC.make_needle("session-1")
        assert token in sentence
        assert len(token) == 10
        assert token.isalnum()

    def test_it_is_stable_for_the_same_seed(self):
        assert LC.make_needle("s")[1] == LC.make_needle("s")[1]

    def test_different_seeds_give_different_needles(self):
        assert LC.make_needle("a")[1] != LC.make_needle("b")[1]

    def test_planting_lands_on_a_line_boundary(self):
        # Splitting a JSON blob mid-token would corrupt the input, and then a
        # failure means "we broke it" rather than "the model missed it".
        filler = "\n".join(f"line {i}" for i in range(100))
        planted = LC.plant(filler, "NEEDLE HERE", 0.5)
        assert "NEEDLE HERE" in planted.splitlines()
        assert len(planted.splitlines()) == 101

    def test_depth_controls_position(self):
        filler = "\n".join(f"line {i}" for i in range(100))
        early = LC.plant(filler, "N", 0.1).splitlines().index("N")
        late = LC.plant(filler, "N", 0.9).splitlines().index("N")
        assert early < late

    def test_empty_filler_does_not_crash(self):
        assert LC.plant("", "N", 0.5) == "N"


class TestNeedleGrading:
    def test_the_recalled_token_passes(self):
        v = LC.grade_needle(case_id="c", answer="The reference is ABC1234567.",
                            token="ABC1234567")
        assert outcome(v, "needle_recalled").outcome == PASS

    def test_a_missing_token_fails(self):
        v = LC.grade_needle(case_id="c", answer="I could not find it.",
                            token="ABC1234567")
        assert outcome(v, "needle_recalled").outcome == FAIL

    def test_reporting_a_needle_that_was_never_planted_fails(self):
        # Without this, a model that emits a plausible reference on every query
        # scores as a perfect recaller.
        v = LC.grade_needle(case_id="c", answer="The reference is DEADBEEF01.",
                            token="ABC1234567", decoy="DEADBEEF01")
        assert outcome(v, "needle_negative_control").outcome == FAIL

    def test_a_clean_answer_passes_the_control(self):
        v = LC.grade_needle(case_id="c", answer="It is ABC1234567.",
                            token="ABC1234567", decoy="DEADBEEF01")
        assert outcome(v, "needle_negative_control").outcome == PASS


class TestDegenerateOutput:
    def test_a_repeated_line_is_caught(self):
        answer = "\n".join(["I cannot find it."] * 8)
        v = LC.grade_needle(case_id="c", answer=answer, token="X")
        assert outcome(v, "no_degenerate_output").outcome == FAIL

    def test_a_repeated_token_tail_is_caught(self):
        answer = "The answer is " + ("na " * 60)
        v = LC.grade_needle(case_id="c", answer=answer, token="X")
        assert outcome(v, "no_degenerate_output").outcome == FAIL

    def test_an_empty_answer_is_caught(self):
        v = LC.grade_needle(case_id="c", answer="", token="X")
        assert outcome(v, "no_degenerate_output").outcome == FAIL

    def test_normal_prose_passes(self):
        v = LC.grade_needle(
            case_id="c",
            answer="The audit reference appears near the middle: ABC1234567.",
            token="ABC1234567",
        )
        assert outcome(v, "no_degenerate_output").outcome == PASS


class TestPairedDegradation:
    def _verdicts(self, ids, ok):
        out = []
        for case_id in ids:
            v = LC.grade_needle(
                case_id=case_id, answer="TOKEN123AB" if ok else "no idea",
                token="TOKEN123AB",
            )
            out.append(v)
        return out

    def test_a_matched_pair_reports_a_delta(self):
        short = self._verdicts(["a", "b"], ok=True)
        long = self._verdicts(["a", "b"], ok=False)
        report = LC.degradation(short, long)
        assert report["comparable"] is True
        assert report["delta"] < 0

    def test_different_cases_on_each_side_are_not_comparable(self):
        # Different tasks at different lengths is not a paired design; the delta
        # would mix task difficulty into a length effect.
        short = self._verdicts(["a", "b"], ok=True)
        long = self._verdicts(["c", "d"], ok=True)
        report = LC.degradation(short, long)
        assert report["comparable"] is False
        assert "same task at both lengths" in report["reason"]

    def test_no_gradeable_turns_is_not_comparable(self):
        report = LC.degradation([], [])
        assert report["comparable"] is False

    def test_the_delta_carries_its_caveat(self):
        report = LC.degradation(
            self._verdicts(["a"], ok=True), self._verdicts(["a"], ok=True)
        )
        assert "scales with it" in report["caveat"]

    def test_the_token_ratio_is_the_measured_one(self):
        assert LC.CHARS_PER_TOKEN == 3.05
        assert LC.estimate_tokens("x" * 305) == 100

    def test_it_states_that_some_real_prompts_cannot_be_run(self):
        assert any("262,144" in c for c in LC.CANNOT_CATCH)
