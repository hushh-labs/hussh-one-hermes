# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The on-device PKM benchmark.

Two things here are load-bearing beyond ordinary correctness: the loopback
guard, because the entire claim of the benchmark is that the work happened on
this machine, and the scoring, because a number that credits a malformed tool
call reports a speed nobody can use.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_pkm import benchmark as bench


def _completion(tool_calls, *, tokens=64):
    return {
        "choices": [{"message": {"tool_calls": tool_calls}}],
        "usage": {"completion_tokens": tokens},
    }


def _good_call(**overrides):
    arguments = {
        "domain": "health",
        "scope_path": "health.diet.restrictions",
        "merge_patch": {"dairy": "avoided"},
        "summary": "Stopped eating dairy in January.",
    }
    arguments.update(overrides)
    return [
        {
            "function": {
                "name": "save_to_pkm",
                "arguments": json.dumps(arguments),
            }
        }
    ]


class TestLoopbackGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:1234",
            "http://localhost:1234",
            "http://[::1]:1234",
        ],
    )
    def test_accepts_this_machine(self, url):
        bench.assert_loopback(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com",
            "http://192.168.1.50:1234",
            "http://10.0.0.2:1234",
            # The one that defeats a substring check: this resolves to an
            # attacker's host, and "127.0.0.1" appears in the string.
            "http://127.0.0.1.evil.example:1234",
            "http://localhost.evil.example:1234",
        ],
    )
    def test_refuses_anywhere_else(self, url):
        with pytest.raises(bench.RemoteHostRefused):
            bench.assert_loopback(url)

    def test_run_turn_refuses_before_sending_anything(self):
        # The guard has to fire before the request is built, not after a
        # response comes back from somewhere it should never have been sent.
        sent = []

        def _opener(*args, **kwargs):
            sent.append(args)
            raise AssertionError("must not reach the network")

        with pytest.raises(bench.RemoteHostRefused):
            bench.run_turn(
                model="m",
                case={"id": "c", "utterance": "hi"},
                rep=0,
                cold=True,
                base_url="https://api.openai.com",
                opener=_opener,
            )
        assert sent == []


class TestScoring:
    def test_a_complete_call_is_valid(self):
        score = bench.score_tool_call(_completion(_good_call()))
        assert score["valid"] is True
        assert score["missing_fields"] == []

    def test_no_tool_call_is_not_a_fast_result(self):
        # A model that answers in prose is not a fast PKM save, it is a
        # non-answer. Crediting its latency would rank it first.
        payload = {"choices": [{"message": {"content": "Sure, noted!"}}]}
        score = bench.score_tool_call(payload)
        assert score["valid"] is False
        assert score["reason"] == "no_tool_call"

    def test_empty_required_field_counts_as_missing(self):
        score = bench.score_tool_call(_completion(_good_call(scope_path="")))
        assert score["valid"] is False
        assert "scope_path" in score["missing_fields"]

    def test_empty_merge_patch_counts_as_missing(self):
        # An empty patch saves nothing. It is the cheapest possible output, so
        # it must not be the winning one.
        score = bench.score_tool_call(_completion(_good_call(merge_patch={})))
        assert score["valid"] is False
        assert "merge_patch" in score["missing_fields"]

    def test_wrong_tool_is_rejected(self):
        calls = [{"function": {"name": "search_web", "arguments": "{}"}}]
        score = bench.score_tool_call(_completion(calls))
        assert score["valid"] is False
        assert score["reason"].startswith("wrong_tool")

    def test_unparseable_arguments_are_rejected_not_crashed(self):
        calls = [{"function": {"name": "save_to_pkm", "arguments": "{not json"}}]
        score = bench.score_tool_call(_completion(calls))
        assert score["valid"] is False
        assert score["reason"] == "unparseable_arguments"

    def test_arguments_may_arrive_already_parsed(self):
        # Some servers hand back an object rather than a JSON string.
        calls = [
            {
                "function": {
                    "name": "save_to_pkm",
                    "arguments": {
                        "domain": "travel",
                        "scope_path": "travel.seat",
                        "merge_patch": {"seat": "aisle"},
                        "summary": "Prefers an aisle seat.",
                    },
                }
            }
        ]
        assert bench.score_tool_call(_completion(calls))["valid"] is True

    def test_a_malformed_payload_does_not_raise(self):
        for payload in (None, {}, {"choices": []}, {"choices": [None]}, "nope"):
            assert bench.score_tool_call(payload)["valid"] is False


class TestPercentiles:
    def test_empty_sample_is_none_not_zero(self):
        # Zero milliseconds is a measurement claim, and it is the most
        # flattering possible reading of having measured nothing.
        assert bench.pct([], 0.95) is None

    def test_single_sample_is_that_sample(self):
        assert bench.pct([42.0], 0.95) == 42.0

    def test_interpolates(self):
        assert bench.pct([0.0, 100.0], 0.5) == 50.0


class TestSummary:
    def test_cold_and_warm_are_never_blended(self):
        results = [
            bench.TurnResult(
                model="m", case_id="a", rep=0, cold=True, ok=True,
                t_model_ms=5000.0, valid_tool_call=True,
            ),
            bench.TurnResult(
                model="m", case_id="b", rep=0, cold=False, ok=True,
                t_model_ms=400.0, valid_tool_call=True,
            ),
            bench.TurnResult(
                model="m", case_id="c", rep=1, cold=False, ok=True,
                t_model_ms=600.0, valid_tool_call=True,
            ),
        ]
        summary = bench.summarize(results)
        entry = summary["models"][0]
        assert entry["t_model_cold"]["count"] == 1
        assert entry["t_model_cold"]["p50_ms"] == 5000.0
        assert entry["t_model_warm"]["count"] == 2
        assert entry["t_model_warm"]["p50_ms"] == 500.0
        # No key offers a single blended latency: it would hide the load cost.
        assert "t_model_ms" not in entry
        assert "average_ms" not in entry

    def test_errors_are_reported_beside_the_validity_rate(self):
        # Three of four turns failed outright. The survivor is 100% valid, and
        # publishing only that would read as a perfect model.
        results = [
            bench.TurnResult(
                model="m", case_id="a", rep=0, cold=True, ok=True,
                t_model_ms=100.0, valid_tool_call=True,
            ),
        ] + [
            bench.TurnResult(
                model="m", case_id=f"e{i}", rep=0, cold=False, ok=False,
                error="timeout",
            )
            for i in range(3)
        ]
        entry = bench.summarize(results)["models"][0]
        assert entry["valid_tool_call_rate"] == 1.0
        assert entry["errors"] == 3
        assert entry["turns"] == 4

    def test_a_model_that_never_answered_reports_no_rate(self):
        results = [
            bench.TurnResult(
                model="m", case_id="a", rep=0, cold=True, ok=False, error="refused"
            )
        ]
        entry = bench.summarize(results)["models"][0]
        assert entry["valid_tool_call_rate"] is None
        assert entry["t_model_warm"]["p50_ms"] is None


class TestSizeEstimate:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("google/gemma-4-12b-qat", 7.2),
            ("google/gemma-4-31b-qat", 18.6),
            ("qwen/qwen3.6-35b-a3b", 21.0),
        ],
    )
    def test_reads_the_parameter_count_from_the_identifier(self, model, expected):
        assert bench._estimated_size_gb(model) == expected

    def test_an_unreadable_identifier_falls_back_rather_than_raising(self):
        assert bench._estimated_size_gb("some/custom-model") == 8.0


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestTruncationIsNotAFailure:
    """A model that ran out of budget is indeterminate, not bad.

    Reasoning tokens come out of the same budget as the answer, and no model
    metadata declares that a model reasons. So a capable model can think past
    the limit and return a truncated answer that looks exactly like a bad one.
    Scoring that as invalid reports a harness under-budget as a model result --
    precisely the false negative this harness exists to prevent.
    """

    def test_a_truncated_turn_is_excluded_from_the_validity_rate(self):
        results = [
            bench.TurnResult(
                model="m", case_id="a", rep=0, cold=False, ok=True,
                t_model_ms=100.0, valid_tool_call=True, finish_reason="tool_calls",
            ),
            bench.TurnResult(
                model="m", case_id="b", rep=0, cold=False, ok=True,
                t_model_ms=100.0, valid_tool_call=False, finish_reason="length",
                truncated=True, reasoning_tokens=411,
            ),
        ]
        entry = bench.summarize(results)["models"][0]
        # One scorable turn, and it was valid. The truncated one is reported
        # beside the rate rather than dragging it to 50%.
        assert entry["scored_turns"] == 1
        assert entry["valid_tool_call_rate"] == 1.0
        assert entry["truncated"] == 1

    def test_an_all_truncated_model_reports_no_rate_at_all(self):
        results = [
            bench.TurnResult(
                model="m", case_id=f"c{i}", rep=0, cold=False, ok=True,
                t_model_ms=100.0, valid_tool_call=False, finish_reason="length",
                truncated=True,
            )
            for i in range(3)
        ]
        entry = bench.summarize(results)["models"][0]
        # Nothing was measurable. A 0.0 here would assert the model failed.
        assert entry["valid_tool_call_rate"] is None
        assert entry["truncated"] == 3

    def test_finish_reason_and_reasoning_tokens_are_recorded(self):
        payload = {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {
                "completion_tokens": 400,
                "completion_tokens_details": {"reasoning_tokens": 377},
            },
        }
        turn = bench.run_turn(
            model="m", case={"id": "c", "utterance": "u"}, rep=0, cold=False,
            opener=lambda *a, **k: _FakeResponse(payload),
        )
        assert turn.finish_reason == "length"
        assert turn.reasoning_tokens == 377
        assert turn.truncated is True
        assert turn.invalid_reason == "truncated"

    def test_a_normal_stop_is_not_truncated(self):
        payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
            "usage": {"completion_tokens": 10},
        }
        turn = bench.run_turn(
            model="m", case={"id": "c", "utterance": "u"}, rep=0, cold=False,
            opener=lambda *a, **k: _FakeResponse(payload),
        )
        assert turn.truncated is False
        assert turn.finish_reason == "stop"
