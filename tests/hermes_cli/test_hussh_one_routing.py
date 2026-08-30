# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The routing harness: bounded requests and measured capability.

Two failures these exist to prevent, both observed on this fleet:

  * An unbounded request. A ~600-token prompt with no max_tokens and no
    reasoning_effort ran past 900 seconds; bounded, it returned in 7.1. At 81
    turns per model that is the difference between minutes and a day.
  * A capability answer cached per model. `reasoning_effort: "none"` suppresses
    reasoning on gemma-4-e2b alone and is defeated when combined with
    json_schema -- 0 tokens vs 241, same model, same instruction.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_routing import request as R
from hermes_cli.hussh_one_routing import profile as P


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _payload(*, content="ok", finish="stop", reasoning=0, tool_calls=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {
            "completion_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


class TestARequestCannotBeUnbounded:
    @pytest.mark.parametrize("bad", [0, -1, None, "600", 1.5])
    def test_a_missing_or_bad_max_tokens_is_refused(self, bad):
        with pytest.raises(R.UnboundedRequest, match="max_tokens"):
            R.build_body(
                model="m", messages=[], max_tokens=bad, reasoning_effort="none"
            )

    def test_an_unknown_reasoning_effort_is_refused(self):
        with pytest.raises(R.UnboundedRequest, match="reasoning_effort"):
            R.build_body(
                model="m", messages=[], max_tokens=100, reasoning_effort="banana"
            )

    def test_both_bounds_reach_the_wire(self):
        body = R.build_body(
            model="m", messages=[], max_tokens=900, reasoning_effort="low"
        )
        assert body["max_tokens"] == 900
        assert body["reasoning_effort"] == "low"

    def test_there_is_no_default_that_can_be_forgotten(self):
        # Defaults are how an unbounded request gets built by accident. Both
        # must be supplied at every call site.
        import inspect

        params = inspect.signature(R.build_body).parameters
        assert params["max_tokens"].default is inspect.Parameter.empty
        assert params["reasoning_effort"].default is inspect.Parameter.empty

    def test_tool_choice_is_never_required(self):
        # This fleet accepts "required" and ignores it, returning prose with
        # finish_reason "stop". Relying on it means trusting a guarantee the
        # server does not honour.
        body = R.build_body(
            model="m",
            messages=[],
            max_tokens=100,
            reasoning_effort="none",
            tools=[{"type": "function", "function": {"name": "f"}}],
        )
        assert body["tool_choice"] == "auto"


class TestTurnClassification:
    def test_a_truncated_turn_is_indeterminate_not_wrong(self):
        turn = R.complete(
            model="m",
            messages=[],
            max_tokens=10,
            reasoning_effort="none",
            opener=lambda *a, **k: _Resp(_payload(finish="length")),
        )
        assert turn.truncated is True
        assert turn.indeterminate is True

    def test_a_normal_answer_is_determinate(self):
        turn = R.complete(
            model="m",
            messages=[],
            max_tokens=100,
            reasoning_effort="none",
            opener=lambda *a, **k: _Resp(_payload()),
        )
        assert turn.indeterminate is False
        assert turn.content == "ok"

    def test_whitespace_only_content_is_stripped(self):
        # qwen returns "\n\n" alongside a tool call where gemma returns "".
        turn = R.complete(
            model="m",
            messages=[],
            max_tokens=100,
            reasoning_effort="none",
            opener=lambda *a, **k: _Resp(_payload(content="\n\n")),
        )
        assert turn.content == ""

    def test_a_timeout_is_marked_as_such_not_as_a_bad_answer(self):
        def _boom(*_a, **_k):
            raise TimeoutError("timed out")

        turn = R.complete(
            model="m", messages=[], max_tokens=100, reasoning_effort="none",
            opener=_boom,
        )
        assert turn.timed_out is True
        assert turn.indeterminate is True


class TestCircuitBreaker:
    def test_three_consecutive_timeouts_abandon_the_rung(self):
        # 81 turns at a 900s ceiling is 20 hours. Abandoning costs minutes.
        breaker = R.CircuitBreaker()
        for _ in range(3):
            breaker.record(R.Turn(model="m", ok=False, timed_out=True))
        assert breaker.abandoned is True
        assert "indeterminate" in breaker.reason

    def test_an_intermittent_timeout_does_not_abandon(self):
        breaker = R.CircuitBreaker()
        breaker.record(R.Turn(model="m", ok=False, timed_out=True))
        breaker.record(R.Turn(model="m", ok=True))
        breaker.record(R.Turn(model="m", ok=False, timed_out=True))
        assert breaker.abandoned is False

    def test_a_real_failure_that_is_not_a_timeout_does_not_trip_it(self):
        breaker = R.CircuitBreaker()
        for _ in range(5):
            breaker.record(R.Turn(model="m", ok=False, error="bad json"))
        assert breaker.abandoned is False


class TestCapabilityIsMeasuredPerCombination:
    def test_suppression_defeated_by_schema_is_recorded_separately(self):
        # The real gemma-4-e2b behaviour: 0 reasoning tokens alone, 241 with a
        # schema. A single per-model answer records the wrong one.
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.capabilities["reasoning_suppression"] = P.Capability(
            name="reasoning_suppression", supported=True,
            measured={"reasoning_tokens": 0})
        prof.capabilities["reasoning_suppression_with_schema"] = P.Capability(
            name="reasoning_suppression_with_schema", supported=False,
            measured={"reasoning_tokens": 241})
        rec = P._recommend(prof)
        # The budget must grow to cover reasoning the model will spend before
        # writing anything, or a capable model gets scored as truncated.
        assert rec["reasoning_tokens_observed"] == 241
        assert rec["max_tokens"] > 1200

    def test_a_clean_suppressor_gets_the_base_budget(self):
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.capabilities["reasoning_suppression"] = P.Capability(
            name="reasoning_suppression", supported=True,
            measured={"reasoning_tokens": 0})
        assert P._recommend(prof)["max_tokens"] == 1200

    def test_a_model_without_tools_is_tested_via_json_not_scored_zero(self):
        # Scoring a capable model 0 because the probe assumed something it does
        # not do is a harness bug published as a model result.
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.capabilities["tool_calling"] = P.Capability(
            name="tool_calling", supported=False)
        prof.capabilities["json_schema"] = P.Capability(
            name="json_schema", supported=True)
        assert P._recommend(prof)["probe_shape"] == "json_schema"

    def test_a_model_with_neither_falls_back_to_text(self):
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.capabilities["tool_calling"] = P.Capability(
            name="tool_calling", supported=False)
        prof.capabilities["json_schema"] = P.Capability(
            name="json_schema", supported=False)
        assert P._recommend(prof)["probe_shape"] == "text"


class TestProbeModeIsTheComparabilityKey:
    def test_output_protocol_is_part_of_the_key(self):
        # A model asked for a whole file and one asked for a region were not
        # asked the same question. That difference has already been mistaken
        # for a difference between models.
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.recommended = {"reasoning_effort": "none", "max_tokens": 1200}
        assert prof.probe_mode("merge", "region") != prof.probe_mode("merge", "whole")

    def test_the_suite_is_part_of_the_key(self):
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.recommended = {"reasoning_effort": "none", "max_tokens": 1200}
        # This is what makes averaging across suites structurally impossible:
        # compare_runs already refuses across a differing probe_mode.
        assert prof.probe_mode("pkm", "region") != prof.probe_mode("code", "region")

    def test_the_budget_is_part_of_the_key(self):
        a = P.CapabilityProfile(schema_version=1, model="m")
        a.recommended = {"reasoning_effort": "none", "max_tokens": 1200}
        b = P.CapabilityProfile(schema_version=1, model="m")
        b.recommended = {"reasoning_effort": "none", "max_tokens": 1923}
        assert a.probe_mode("code", "region") != b.probe_mode("code", "region")


class TestADeadModelFailsLoudly:
    def test_no_response_fails_the_profile_rather_than_scoring_five_zeros(self):
        def _boom(*_a, **_k):
            raise TimeoutError("down")

        import hermes_cli.hussh_one_routing.profile as mod

        original = mod.complete
        mod.complete = lambda **kw: R.complete(opener=_boom, **kw)
        try:
            prof = mod.probe_capabilities("dead-model")
        finally:
            mod.complete = original
        assert prof.failed is True
        assert prof.capabilities == {}
