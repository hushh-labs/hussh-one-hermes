# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Raising a model's thinking using the control it actually honours.

The API parameter is inert on this stack, so the control is prompt-embedded and
per-family. These pin the behaviours that were measured on real models, and the
one that is counter-intuitive: a plain system prompt makes models think *more*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hermes_cli.hussh_one_routing import reasoning as R


@dataclass
class _Turn:
    reasoning_tokens: Optional[int] = None


class _Host:
    """Scripted reasoning spend, keyed by what appears in the system prompt."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, messages, max_tokens):
        self.calls.append((messages, max_tokens))
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )
        for marker, spend in self.table.items():
            if marker and marker in system:
                return _Turn(spend)
        return _Turn(self.table.get("", 0))


class TestFamilyDetection:
    def test_gemma_ids_resolve_to_the_think_token(self):
        assert R.control_for("google/gemma-4-31b-qat", R.MAX).startswith(
            R.GEMMA_THINK
        )

    def test_qwen_ids_resolve_to_the_soft_switch(self):
        assert R.control_for("qwen/qwen3.8-27b", R.MAX) == R.QWEN_THINK

    def test_qwen_off_uses_no_think(self):
        assert R.control_for("qwen/qwen3.8-27b", R.OFF) == R.QWEN_NO_THINK

    def test_an_unknown_family_gets_no_control_rather_than_a_guess(self):
        # Injecting Gemma's token into a model that does not parse it puts a
        # literal <|think|> in the visible output, which the oracles would then
        # score as the model's mistake.
        assert R.control_for("nvidia/nemotron-3-nano-omni", R.MAX) == ""

    def test_only_qwen38_claims_native_effort(self):
        assert R.supports_native_effort("qwen/qwen3.8-27b") is True
        assert R.supports_native_effort("google/gemma-4-31b-qat") is False


class TestApplyingTheControl:
    def _profile(self, prefix):
        return R.ReasoningProfile(model="m", prefix=prefix)

    def test_it_prepends_to_an_existing_system_message(self):
        out = self._profile("<|think|>").apply(
            [{"role": "system", "content": "You resolve merges."},
             {"role": "user", "content": "go"}]
        )
        assert out[0]["content"].startswith("<|think|>")
        assert "You resolve merges." in out[0]["content"]
        assert len(out) == 2

    def test_it_adds_a_system_message_when_there_is_none(self):
        out = self._profile("/think").apply([{"role": "user", "content": "go"}])
        assert out[0] == {"role": "system", "content": "/think"}

    def test_an_empty_prefix_changes_nothing(self):
        messages = [{"role": "user", "content": "go"}]
        assert self._profile("").apply(messages) == messages

    def test_the_caller_messages_are_not_mutated(self):
        # Mutating would let one case contaminate the next, and a suite that
        # runs the same corpus twice would accumulate control tokens.
        original = [{"role": "system", "content": "base"}]
        self._profile("<|think|>").apply(original)
        assert original[0]["content"] == "base"

    def test_applying_twice_does_not_stack_on_the_original(self):
        profile = self._profile("<|think|>")
        messages = [{"role": "system", "content": "base"}]
        first = profile.apply(messages)
        second = profile.apply(messages)
        assert first[0]["content"] == second[0]["content"]


class TestTheBudgetFollowsMeasuredSpend:
    def test_it_scales_with_the_worst_observed_reasoning(self):
        profile = R.ReasoningProfile(model="m", measured={"max": 5492})
        assert profile.max_tokens >= 5492 * 2
        assert profile.max_tokens > R.MIN_BUDGET

    def test_a_floor_applies_when_nothing_was_measured(self):
        # A zero from a short probe means "this prompt did not reason", not
        # "reasoning is off", and the real task reasons anyway.
        assert R.ReasoningProfile(model="m").max_tokens == R.MIN_BUDGET

    def test_the_worst_mode_sets_the_budget_not_the_average(self):
        profile = R.ReasoningProfile(
            model="m", measured={"off": 10, "brief": 200, "max": 6000}
        )
        assert profile.max_tokens >= 6000 * 2


class TestProbingMeasuresRatherThanAssumes:
    def test_it_records_spend_for_each_mode(self):
        host = _Host({"": 300, R.GEMMA_THINK: 1200})
        profile = R.probe("google/gemma-4-31b-qat", ask=host)
        assert profile.measured[R.MAX] == 1200
        assert profile.family == "gemma"

    def test_it_detects_that_a_plain_system_prompt_inflates_reasoning(self):
        # Measured on both real models: a bare system prompt roughly doubled
        # reasoning (1484 -> 2468 and 345 -> 586). This is why the control
        # belongs per-model rather than in a shared suite prompt.
        host = _Host({"": 345, "You answer questions.": 586, R.GEMMA_THINK: 257})
        profile = R.probe("google/gemma-4-31b-qat", ask=host)
        assert profile.prompt_inflates_reasoning is True

    def test_it_records_when_a_prompt_does_not_inflate(self):
        host = _Host({"": 500, "You answer questions.": 400, R.GEMMA_THINK: 900})
        profile = R.probe("google/gemma-4-31b-qat", ask=host)
        assert profile.prompt_inflates_reasoning is False

    def test_max_is_the_default_because_accuracy_beats_latency(self):
        host = _Host({"": 100, R.GEMMA_THINK: 900})
        assert R.probe("google/gemma-4-12b-qat", ask=host).mode == R.MAX

    def test_a_probe_prompt_must_be_able_to_provoke_reasoning(self):
        # A trivial question answers straight through on every model here, so
        # probing with one compares two zeros and concludes the knob is dead.
        assert len(R.PROBE_PROMPT) > 80
        assert any(ch.isdigit() for ch in R.PROBE_PROMPT)

    def test_an_unknown_family_still_profiles_without_crashing(self):
        host = _Host({"": 420})
        profile = R.probe("nvidia/nemotron-3-nano-omni", ask=host)
        assert profile.prefix == ""
        assert profile.measured["no_system_prompt"] == 420

    def test_a_host_that_reports_nothing_does_not_poison_the_budget(self):
        profile = R.probe("google/gemma-4-31b-qat", ask=lambda m, t: _Turn(None))
        assert profile.max_tokens == R.MIN_BUDGET


class TestSerialisation:
    def test_the_profile_records_what_was_measured(self):
        host = _Host({"": 300, R.QWEN_THINK: 1500})
        payload = R.probe("qwen/qwen3.8-27b", ask=host).to_dict()
        assert payload["family"] == "qwen"
        assert payload["native_effort"] is True
        assert payload["max_tokens"] >= 1500 * 2
        assert "measured" in payload
