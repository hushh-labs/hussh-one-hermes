# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Every model is told what it is, not just the one vendor that forced it.

The identity statement began as an Alibaba workaround (their Coding Plan API
returns "glm-4.7" whatever you request) and was gated to that provider. Every
other model got only the bare ``Model: <id>`` line, which states the fact but
never says to prefer it over what the environment appears to show.

Measured 2026-09-06 on the quest harness: ``google/gemma-4-12b`` and
``google/gemma-4-12b-qat`` -- different quantizations, different context windows
-- both wrote ``gemini-3.5-flash`` in byte-identical answers when asked what
model they were, one citing the ``AGENT_GEMINI_MODEL`` environment variable as
confirmation. That variable is real live config (the PKM KYC engine reads it to
choose an extraction model), so the fix cannot be to delete it.
``google/gemma-4-31b`` answered correctly, so this is a capability floor: the
prompt has to carry the guard rather than assume the model will.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt_parts

IDENTITY = "You are powered by the model named"


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="google/gemma-4-12b",
        provider="lmstudio",
        platform="",
        pass_session_id=False,
        session_id="",
        _emit_status=lambda *_a, **_k: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stable(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


class TestIdentityIsStatedForEveryProvider:
    @pytest.mark.parametrize(
        "provider",
        ["lmstudio", "alibaba", "openai", "anthropic", "ollama", "custom:lmstudio"],
    )
    def test_named_whatever_the_provider(self, provider):
        text = _stable(_make_agent(provider=provider))
        assert IDENTITY in text, (
            f"provider {provider!r} left without an identity statement; the "
            "bare 'Model:' line does not tell the model to prefer it"
        )
        assert "google/gemma-4-12b" in text

    def test_local_providers_were_the_regression(self):
        """The exact case that produced 'gemini-3.5-flash' twice."""
        assert IDENTITY in _stable(_make_agent(provider="lmstudio"))

    def test_alibaba_keeps_the_behaviour_it_originally_needed(self):
        assert IDENTITY in _stable(_make_agent(
            model="qwen/glm-4.7", provider="alibaba"))


class TestItNamesTheTrapsActuallyFallenInto:
    @pytest.mark.parametrize(
        "trap", ["environment variable", "config file", "returned by the API"]
    )
    def test_each_trap_is_named(self, trap):
        """A generic 'not the API' warning did not cover the real failure."""
        assert trap in _stable(_make_agent()), (
            f"the prompt must warn about {trap!r}: two models read "
            "AGENT_GEMINI_MODEL and reported themselves as gemini-3.5-flash"
        )


class TestIdShaping:
    def test_slugged_id_is_shortened_and_the_full_id_survives(self):
        text = _stable(_make_agent(model="meta/muse-glimmer"))
        assert "named muse-glimmer" in text
        assert "meta/muse-glimmer" in text

    def test_unslugged_id_used_as_is(self):
        assert "named qwen3.8-27b" in _stable(_make_agent(model="qwen3.8-27b"))

    def test_no_model_means_no_claim(self):
        """Never assert an identity we do not have."""
        assert IDENTITY not in _stable(_make_agent(model=""))
