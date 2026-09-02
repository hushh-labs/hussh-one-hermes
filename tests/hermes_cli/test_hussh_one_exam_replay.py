# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Replaying real session turns: the exam for the work Hermes actually does.

Everything else in this harness grades a chore. This asks the question the
product asks, on the owner's own history, at the length that work arrives at.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_routing.exam import replay as RP
from hermes_cli.hussh_one_routing.exam.model import FAIL, PASS, SKIP


def case(**kw):
    defaults = dict(
        case_id="c1",
        messages=[{"role": "user", "content": "find the config"}],
        catalog=["read_file", "search_files", "terminal", "write_file"],
        schemas={
            "terminal": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            "search_files": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
        expected_tool="search_files",
        expected_args={"pattern": "config"},
    )
    defaults.update(kw)
    return RP.ReplayCase(**defaults)


def outcome(verdict, name):
    return next((o for o in verdict.outcomes if o.name == name), None)


class TestGradingCombinesSelectionAndArguments:
    def test_matching_the_reference_is_recorded(self):
        v = RP.grade(case(), chosen="search_files", arguments={"pattern": "config"})
        assert v.label_match is True
        assert outcome(v, "tool_name_correct").outcome == PASS

    def test_a_different_tool_still_gets_its_arguments_graded(self):
        # Producing a broken shell command is wrong whether or not the reference
        # would have run one, so a mismatch must not skip the deeper checks.
        v = RP.grade(
            case(), chosen="terminal", arguments={"command": 'echo "unterminated'}
        )
        assert v.label_match is False
        assert outcome(v, "shell_parses").outcome == FAIL

    def test_a_valid_alternative_passes_its_structural_checks(self):
        v = RP.grade(case(), chosen="terminal", arguments={"command": "ls -la"})
        assert v.label_match is False
        assert outcome(v, "shell_parses").outcome == PASS

    def test_a_file_write_is_parse_checked(self):
        v = RP.grade(
            case(),
            chosen="write_file",
            arguments={"path": "a.js", "content": "function f() { # oops\n}"},
        )
        assert outcome(v, "parses").outcome == FAIL

    def test_a_tool_with_no_argument_grader_is_marked_skip(self):
        v = RP.grade(case(), chosen="read_file", arguments={"path": "a.py"})
        assert outcome(v, "argument_depth").outcome == SKIP

    def test_calling_nothing_is_graded_not_ignored(self):
        v = RP.grade(case(), chosen=None, arguments=None)
        assert v.label_match is False


class TestTheCatalogIsTheRealOne:
    def test_a_tool_outside_the_offered_catalog_fails(self):
        v = RP.grade(case(), chosen="invented_tool", arguments={})
        assert outcome(v, "tool_in_catalog").outcome == FAIL

    def test_the_payload_preserves_the_offered_schemas(self):
        payload = RP.tools_payload(case())
        names = {t["function"]["name"] for t in payload}
        assert names == {"read_file", "search_files", "terminal", "write_file"}
        by_name = {t["function"]["name"]: t for t in payload}
        assert by_name["terminal"]["function"]["parameters"]["required"] == ["command"]

    def test_a_tool_without_a_schema_still_gets_an_object_stub(self):
        # Omitting parameters entirely makes some servers reject the request,
        # which would score as a model failure.
        payload = RP.tools_payload(case())
        by_name = {t["function"]["name"]: t for t in payload}
        assert by_name["read_file"]["function"]["parameters"] == {"type": "object"}


class TestSamplingIsRoundRobinNotSequential:
    def test_one_long_session_cannot_crowd_out_the_others(self):
        # Taking the first N in file order drew 472 of 500 cases from one model
        # and every case from a 29-tool catalog, so an exam meant to measure
        # behaviour under a 232-tool catalog contained no large catalog at all.
        buckets = {
            "long": [f"long-{i}" for i in range(100)],
            "short-a": ["a-0"],
            "short-b": ["b-0", "b-1"],
        }
        picked = RP._round_robin(buckets, 6)
        assert "a-0" in picked
        assert "b-0" in picked
        assert picked.count("long-0") == 1

    def test_it_stops_at_the_cap(self):
        buckets = {"a": list(range(50)), "b": list(range(50))}
        assert len(RP._round_robin(buckets, 7)) == 7

    def test_it_drains_everything_when_under_the_cap(self):
        buckets = {"a": [1, 2], "b": [3]}
        assert sorted(RP._round_robin(buckets, 99)) == [1, 2, 3]

    def test_no_sessions_yields_nothing(self):
        assert RP._round_robin({}, 10) == []


def _dump(root, session_id):
    """One minimal OpenAI-style request dump with a single decision."""
    body = {
        "model": "reference-model",
        "tools": [{"type": "function", "function": {
            "name": "terminal", "description": "run a command",
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string"}}},
        }}],
        "messages": [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": f"list the files for {session_id}"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1",
                "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"},
            }]},
        ],
    }
    import json as _json
    (root / f"request_dump_{session_id}_x.json").write_text(
        _json.dumps({"session_id": session_id, "request": {"body": body}}),
        encoding="utf-8",
    )


class TestInactiveCronSessionsAreExcluded:
    SESSIONS = (
        "20260610_165051_314e17",              # interactive: always counts
        "cron_29a6e1247b31_20260630_051615",   # active job
        "cron_8a9375e7bf7c_20260618_045000",   # disabled PR-train job
    )

    def test_an_explicit_active_set_filters_the_corpus(self, tmp_path):
        for sid in self.SESSIONS:
            _dump(tmp_path, sid)
        cases = RP.extract_cases(root=tmp_path, active_jobs={"29a6e1247b31"})
        assert sorted(c.session_id for c in cases) == sorted(self.SESSIONS[:2])

    def test_a_frozen_manifest_decides_when_no_set_is_given(self, tmp_path):
        import json as _json

        for sid in self.SESSIONS:
            _dump(tmp_path, sid)
        (tmp_path / "manifest.json").write_text(
            _json.dumps({"active_cron_jobs": ["8a9375e7bf7c"]}), encoding="utf-8"
        )
        cases = RP.extract_cases(root=tmp_path)
        assert sorted(c.session_id for c in cases) == sorted(
            [self.SESSIONS[0], self.SESSIONS[2]]
        )


class TestKnownPathsComeFromTheRealPrefix:
    def test_paths_mentioned_in_the_conversation_are_collected(self):
        # A model is only faulted for inventing a path nobody mentioned, not for
        # using one the conversation had already established.
        messages = [
            {"role": "user", "content": "look at /Users/me/repo/config.yaml"},
            {"role": "assistant", "content": "checking"},
        ]
        assert "/Users/me/repo/config.yaml" in RP._known_paths(messages)

    def test_paths_inside_tool_calls_are_collected(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file",
                                  "arguments": '{"path": "/Users/me/x/y.py"}'}}
                ],
            }
        ]
        assert any("/Users/me/x/y.py" in p for p in RP._known_paths(messages))

    def test_an_empty_prefix_yields_no_paths(self):
        assert RP._known_paths([]) == []


class TestTheTwoNumbersAreNeverAdded:
    def _verdicts(self):
        good = RP.grade(case(), chosen="search_files", arguments={"pattern": "x"})
        different_but_valid = RP.grade(
            case(), chosen="terminal", arguments={"command": "ls"}
        )
        broken = RP.grade(
            case(), chosen="terminal", arguments={"command": 'echo "oops'}
        )
        return [good, different_but_valid, broken]

    def test_agreement_and_structural_are_reported_separately(self):
        summary = RP.summarize(self._verdicts())
        assert summary["agreement"] is not None
        assert summary["structural"] is not None
        assert summary["agreement"] != summary["structural"]

    def test_a_different_but_valid_action_hurts_agreement_not_structure(self):
        # This is the whole reason they are two numbers: a model that does
        # something else correct is not wrong, it just is not imitating.
        summary = RP.summarize(self._verdicts())
        # Rates are rounded to 4 places for reporting, so compare at that scale.
        assert summary["agreement"] == pytest.approx(1 / 3, abs=1e-4)
        assert summary["structural"] > summary["agreement"]

    def test_indeterminate_turns_are_counted_apart(self):
        verdicts = self._verdicts()
        verdicts[0].indeterminate = "truncated"
        summary = RP.summarize(verdicts)
        assert summary["indeterminate"] == 1
        assert summary["graded"] == 2

    def test_the_caveat_travels_with_the_numbers(self):
        summary = RP.summarize(self._verdicts())
        assert "imitation" in summary["caveat"]
        assert "never added" in summary["caveat"]

    def test_an_empty_run_reports_none_rather_than_zero(self):
        # 0.0 reads as "measured and bad"; None reads as "not measured".
        summary = RP.summarize([])
        assert summary["agreement"] is None
        assert summary["structural"] is None


class TestCaseMetadata:
    def test_tokens_use_the_measured_ratio(self):
        assert case(wire_chars=305).tokens == 100

    def test_catalog_size_is_reported(self):
        assert case().catalog_size == 4
