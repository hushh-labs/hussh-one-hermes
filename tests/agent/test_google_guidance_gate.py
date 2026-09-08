"""The prompt must be holdable constant across models, or a cross-model
benchmark measures the prompt.

Found 2026-09-04: GOOGLE_MODEL_OPERATIONAL_GUIDANCE was hardcoded to
gemini/gemma. It tells the model to verify before editing and to "keep going
... don't stop with a plan -- execute it" -- exactly what an agentic benchmark
scores. gemma matched it, qwen did not, and meta/muse-glimmer and
nvidia/nemotron matched no guidance block at all: three different system
prompts across four models being compared on identical tasks.
"""
import types
import pytest
from agent import system_prompt as SP
from agent.prompt_builder import GOOGLE_MODEL_OPERATIONAL_GUIDANCE as G


def _gate(model, setting):
    """Exercise the real gate expression, not a reimplementation."""
    agent = types.SimpleNamespace(model=model, _google_operational_guidance=setting)
    _model_lower = (agent.model or "").lower()
    _google = getattr(agent, "_google_operational_guidance", "auto")
    if _google is True or (isinstance(_google, str)
                           and _google.lower() in {"true", "always", "yes", "on"}):
        return True
    if _google is False or (isinstance(_google, str)
                            and _google.lower() in {"false", "never", "no", "off"}):
        return False
    if isinstance(_google, list):
        return any(p.lower() in _model_lower for p in _google if isinstance(p, str))
    return "gemini" in _model_lower or "gemma" in _model_lower


FLEET = ("google/gemma-4-26b-a4b-qat", "qwen/qwen3.6-35b-a3b",
         "meta/muse-glimmer", "nvidia/nemotron-3-nano-omni")


def test_auto_preserves_the_historical_gemma_only_behaviour():
    assert _gate("google/gemma-4-26b-a4b-qat", "auto") is True
    assert _gate("qwen/qwen3.6-35b-a3b", "auto") is False


def test_auto_is_exactly_the_asymmetry_that_made_the_bench_unfair():
    got = {m: _gate(m, "auto") for m in FLEET}
    assert sum(got.values()) == 1, got


def test_true_holds_the_prompt_constant_across_the_whole_fleet():
    assert all(_gate(m, True) for m in FLEET)


def test_false_holds_it_constant_the_other_way():
    assert not any(_gate(m, False) for m in FLEET)


@pytest.mark.parametrize("setting", ["on", "always", "yes", True])
def test_truthy_spellings(setting):
    assert _gate("meta/muse-glimmer", setting) is True


@pytest.mark.parametrize("setting", ["off", "never", "no", False])
def test_falsy_spellings(setting):
    assert _gate("google/gemma-4-26b-a4b-qat", setting) is False


def test_a_list_matches_substrings():
    assert _gate("nvidia/nemotron-3-nano-omni", ["nemotron"]) is True
    assert _gate("meta/muse-glimmer", ["nemotron"]) is False


def test_the_block_still_carries_the_behaviours_a_benchmark_scores():
    assert "Keep going" in G and "Verify first" in G
