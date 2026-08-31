# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Grading file edits, built around the two incidents that actually cost time.

Both are reproduced here as fixtures that must FAIL. An oracle that cannot fail
is not an oracle, and an oracle that cannot fail on the real defect it was
written for is worse: it is reassurance.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing.exam import file_edit as E
from hermes_cli.hussh_one_routing.exam.model import FAIL, PASS, SKIP


def outcome(verdict, name):
    return next(o for o in verdict.outcomes if o.name == name)


class TestTheWhatsAppOutage:
    """A Python `#` comment in a .js file. 42 hours down."""

    def test_a_hash_comment_in_javascript_fails(self):
        broken = "function gate(jid) {\n  # reject self chat\n  return jid !== SELF;\n}\n"
        verdict = E.grade(
            case_id="outage", path="bridge_helpers.js", args={"content": broken}
        )
        assert outcome(verdict, "parses").outcome == FAIL
        assert verdict.ok is False

    def test_the_same_file_with_a_real_comment_passes(self):
        fixed = "function gate(jid) {\n  // reject self chat\n  return jid !== SELF;\n}\n"
        verdict = E.grade(
            case_id="fixed", path="bridge_helpers.js", args={"content": fixed}
        )
        assert outcome(verdict, "parses").outcome == PASS
        assert verdict.ok is True


class TestTheEscapedDelimiterWrite:
    """12 escaped triple-quotes plus one stray raw one at byte 0.

    The real result reported `verified: true` and `bytes_written: 12680` beside
    a lint status of `error`. The broken file landed.
    """

    def _body(self, escaped: bool):
        delim = '\\"\\"\\"' if escaped else '"""'
        return (
            ('"""' if escaped else "")
            + "# SPDX-License-Identifier: Apache-2.0\n"
            + f"def f():\n    {delim}Doc.{delim}\n    return 1\n"
        )

    def test_escaped_delimiters_are_caught(self):
        verdict = E.grade(
            case_id="kyc", path="mapper.py", args={"content": self._body(True)}
        )
        assert outcome(verdict, "no_escaped_delimiter").outcome == FAIL
        assert "escaped triple-quote" in outcome(
            verdict, "no_escaped_delimiter"
        ).detail

    def test_the_repaired_version_passes(self):
        verdict = E.grade(
            case_id="kyc-fixed", path="mapper.py", args={"content": self._body(False)}
        )
        assert outcome(verdict, "no_escaped_delimiter").outcome == PASS

    def test_a_bare_escaped_quote_is_not_flagged(self):
        # 14 of 30 intact real files legitimately contain bare \" inside string
        # literals. A single-escape rule would be 47% false-positive.
        body = 'x = "she said \\"hi\\" loudly"\n'
        verdict = E.grade(case_id="quotes", path="a.py", args={"content": body})
        assert outcome(verdict, "no_escaped_delimiter").outcome == PASS

    def test_it_still_fires_where_no_parser_exists(self):
        # The only check that covers .md/.ts/.tsx, which are 7 of 72 real calls.
        verdict = E.grade(
            case_id="md", path="notes.md", args={"content": 'a \\"\\"\\" b'}
        )
        assert outcome(verdict, "parses").outcome == SKIP
        assert outcome(verdict, "no_escaped_delimiter").outcome == FAIL


class TestSkipIsNeverAPass:
    def test_an_unvalidatable_extension_skips_rather_than_passes(self):
        verdict = E.grade(
            case_id="ts", path="a.tsx", args={"content": "const x: number = 1;"}
        )
        assert outcome(verdict, "parses").outcome == SKIP

    def test_a_verdict_of_only_skips_is_not_checked(self):
        verdict = E.grade(
            case_id="md", path="a.md", args={"content": "# hi\n"}
        )
        parses = outcome(verdict, "parses")
        assert parses.outcome == SKIP
        # It did not fail, but nothing about the content was actually verified.
        assert verdict.ok is True
        assert parses.outcome != PASS


class TestAnchorUniqueness:
    PRE = "x = 1\nMAX = 5\ny = 2\nMAX = 5\n"

    def test_a_unique_anchor_passes(self):
        v = E.grade(
            case_id="a", path="a.py",
            args={"old_string": "x = 1", "new_string": "x = 9"}, pre=self.PRE,
        )
        assert outcome(v, "anchor_unique").outcome == PASS

    def test_an_ambiguous_anchor_fails(self):
        v = E.grade(
            case_id="b", path="a.py",
            args={"old_string": "MAX = 5", "new_string": "MAX = 9"}, pre=self.PRE,
        )
        assert outcome(v, "anchor_unique").outcome == FAIL
        assert "2 times" in outcome(v, "anchor_unique").detail

    def test_a_missing_anchor_fails_differently(self):
        v = E.grade(
            case_id="c", path="a.py",
            args={"old_string": "nope", "new_string": "x"}, pre=self.PRE,
        )
        assert "cannot apply" in outcome(v, "anchor_unique").detail

    def test_replace_all_opts_out(self):
        v = E.grade(
            case_id="d", path="a.py",
            args={"old_string": "MAX = 5", "new_string": "MAX = 9",
                  "replace_all": True},
            pre=self.PRE,
        )
        assert outcome(v, "anchor_unique").outcome == SKIP


class TestIdempotence:
    """2 of 25 real patches fail this, both in scripts/whatsapp-bridge/."""

    def test_an_anchor_that_survives_its_replacement_fails(self):
        v = E.grade(
            case_id="dup", path="gate.js",
            args={"old_string": "const SELF = me;",
                  "new_string": "const SELF = me;\nconst GUARD = true;"},
        )
        assert outcome(v, "idempotent").outcome == FAIL
        assert "retry duplicates" in outcome(v, "idempotent").detail

    def test_a_consumed_anchor_passes(self):
        v = E.grade(
            case_id="ok", path="gate.js",
            args={"old_string": "const SELF = me;",
                  "new_string": "const SELF = normalise(me);"},
        )
        assert outcome(v, "idempotent").outcome == PASS

    def test_this_survives_every_parse_check(self):
        # The point of the check: the duplicating patch is valid JavaScript.
        v = E.grade(
            case_id="dup", path="gate.js",
            args={"old_string": "const SELF = me;\n",
                  "new_string": "const SELF = me;\nconst GUARD = true;\n"},
            pre="const SELF = me;\nexport default gate;\n",
        )
        assert outcome(v, "parses").outcome == PASS
        assert outcome(v, "idempotent").outcome == FAIL


class TestFragmentsAreNeverParsedAlone:
    def test_a_valid_fragment_that_does_not_parse_alone_still_passes(self):
        # 15 of 22 real new_string values fail to parse in isolation while the
        # resulting file is fine. Parsing the fragment is a 68% false-failure.
        pre = "def route(x):\n    if x:\n        return 1\n    return 0\n"
        v = E.grade(
            case_id="frag", path="r.py",
            args={"old_string": "        return 1", "new_string": "        return 2"},
            pre=pre,
        )
        assert outcome(v, "parses").outcome == PASS

    def test_without_a_pre_image_parsing_skips_rather_than_guesses(self):
        v = E.grade(
            case_id="nopre", path="r.py",
            args={"old_string": "a", "new_string": "b"},
        )
        assert outcome(v, "parses").outcome == SKIP


class TestConfinement:
    def test_a_byte_exact_result_passes(self):
        pre = "a\nb\nc\n"
        v = E.grade(
            case_id="x", path="a.py",
            args={"old_string": "b", "new_string": "B"}, pre=pre, actual="a\nB\nc\n",
        )
        assert outcome(v, "confined").outcome == PASS

    def test_a_collateral_change_fails(self):
        pre = "a\nb\nc\n"
        v = E.grade(
            case_id="y", path="a.py",
            args={"old_string": "b", "new_string": "B"}, pre=pre, actual="a\nB\nC\n",
        )
        assert outcome(v, "confined").outcome == FAIL


class TestTruncationIsCorpusHygieneNotAModelFailure:
    def test_the_compactor_sentinel_is_caught(self):
        v = E.grade(
            case_id="t", path="a.py",
            args={"content": "import os\n# ...[truncated]"},
        )
        assert outcome(v, "no_truncation").outcome == FAIL

    def test_clean_content_passes(self):
        v = E.grade(case_id="t2", path="a.py", args={"content": "import os\n"})
        assert outcome(v, "no_truncation").outcome == PASS


class TestStaleReads:
    def test_a_partial_prior_read_fails(self):
        v = E.grade(
            case_id="s", path="a.py", args={"content": "x = 1\n"},
            context={"last_read_partial": True},
        )
        assert outcome(v, "fresh_read").outcome == FAIL

    def test_a_full_prior_read_passes(self):
        v = E.grade(
            case_id="s2", path="a.py", args={"content": "x = 1\n"},
            context={"last_read_partial": False},
        )
        assert outcome(v, "fresh_read").outcome == PASS

    def test_no_recorded_read_skips(self):
        v = E.grade(case_id="s3", path="a.py", args={"content": "x = 1\n"})
        assert outcome(v, "fresh_read").outcome == SKIP


class TestTruncationIsNotATimeout:
    """The founder's correction: a truncated turn is compaction, not failure.

    Hermes compacts and continues when a turn hits max_tokens, so calling that
    a harness fault implies a setting to fix when often there is none, and
    calling it a model failure is worse. A timeout is genuinely our clock.
    """

    def _verdict(self, reason):
        from hermes_cli.hussh_one_routing.exam.model import Verdict

        return Verdict(case_id="c", suite="file_edit", indeterminate=reason)

    def test_a_truncated_turn_is_its_own_category(self):
        from hermes_cli.hussh_one_routing.exam.model import COMPACTED

        assert self._verdict("truncated").fault == COMPACTED

    def test_a_length_finish_is_also_compaction(self):
        from hermes_cli.hussh_one_routing.exam.model import COMPACTED

        assert self._verdict("finish_reason=length").fault == COMPACTED

    def test_a_timeout_is_our_clock(self):
        from hermes_cli.hussh_one_routing.exam.model import HARNESS

        assert self._verdict("timeout").fault == HARNESS

    def test_a_transport_error_is_plumbing(self):
        from hermes_cli.hussh_one_routing.exam.model import HARNESS

        assert self._verdict("connection reset").fault == HARNESS

    def test_the_summary_reports_them_apart(self):
        # One of these is a number to act on and the other is not; a single
        # 'indeterminate' count hides which.
        from hermes_cli.hussh_one_routing.exam.model import summarize

        rows = summarize(
            [self._verdict("truncated"), self._verdict("timeout"),
             self._verdict("truncated")]
        )
        assert rows["compacted"] == 2
        assert rows["timed_out"] == 1
        assert rows["indeterminate"] == 3


class TestFaultAttribution:
    def test_a_broken_write_is_an_execution_fault(self):
        from hermes_cli.hussh_one_routing.exam.model import EXECUTION

        v = E.grade(
            case_id="e", path="a.js", args={"content": "function f() { # x\n}"}
        )
        assert v.fault == EXECUTION

    def test_the_asi_names_the_oracle_and_the_instance(self):
        # This string is what the reflector reads. A bare score cannot be
        # learned from; "parses: line 2 ..." names the fix.
        v = E.grade(
            case_id="e", path="a.js", args={"content": "function f() { # x\n}"}
        )
        assert v.asi.startswith("parses:")
        assert len(v.asi) > len("parses:")


class TestHelpers:
    def test_line_number_gutters_are_stripped(self):
        assert E.strip_line_numbers("1|import os\n2|x = 1\n") == "import os\nx = 1\n"

    def test_arguments_parse_from_either_shape(self):
        assert E.parse_arguments('{"a": 1}') == {"a": 1}
        assert E.parse_arguments({"a": 1}) == {"a": 1}
        assert E.parse_arguments("not json") == {}

    def test_the_suite_states_what_it_cannot_catch(self):
        # The corpus contains a patch that passes everything here and still
        # references an undefined variable. That has to be written down.
        assert any("not defined" in c for c in E.CANNOT_CATCH)
