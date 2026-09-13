# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Local-model prompt/output budgeting regressions."""

from types import SimpleNamespace


def _local_agent(*, context_length=131_072, provider="lmstudio"):
    return SimpleNamespace(
        provider=provider,
        base_url="http://127.0.0.1:1234/v1",
        context_compressor=SimpleNamespace(context_length=context_length),
        _max_tokens_param=lambda value: {"max_tokens": value},
    )


def test_local_request_gets_cap_when_server_default_would_overreserve():
    from agent.chat_completion_helpers import _fit_local_output_cap
    from agent.model_metadata import estimate_request_tokens_rough

    messages = [{"role": "user", "content": "x" * 280_000}]
    kwargs = {"messages": messages}

    fitted = _fit_local_output_cap(_local_agent(), kwargs)

    prompt_tokens = estimate_request_tokens_rough(messages)
    assert fitted["max_tokens"] < 65_536
    assert prompt_tokens + fitted["max_tokens"] + 1_024 <= 131_072


def test_local_explicit_cap_is_lowered_only_when_prompt_requires_it():
    from agent.chat_completion_helpers import _fit_local_output_cap

    kwargs = {
        "messages": [{"role": "user", "content": "x" * 280_000}],
        "max_tokens": 65_536,
    }

    fitted = _fit_local_output_cap(_local_agent(), kwargs)

    assert 1 < fitted["max_tokens"] < 65_536


def test_remote_request_is_not_rewritten():
    from agent.chat_completion_helpers import _fit_local_output_cap

    agent = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        context_compressor=SimpleNamespace(context_length=131_072),
        _max_tokens_param=lambda value: {"max_tokens": value},
    )
    kwargs = {"messages": [{"role": "user", "content": "x" * 280_000}]}

    assert _fit_local_output_cap(agent, kwargs) == kwargs


def test_local_auxiliary_cap_is_forwarded_to_chat_completions():
    from agent.auxiliary_client import _build_call_kwargs

    kwargs = _build_call_kwargs(
        "lmstudio",
        "meta/muse-glimmer",
        [{"role": "user", "content": "summarize this"}],
        max_tokens=2_000,
        base_url="http://127.0.0.1:1234/v1",
    )

    assert kwargs["max_tokens"] == 2_000


def test_local_omitted_cap_uses_remaining_context():
    from agent.chat_completion_helpers import _fit_local_output_cap
    from agent.model_metadata import estimate_request_tokens_rough

    kwargs = {"messages": [{"role": "user", "content": "short request"}]}
    fitted = _fit_local_output_cap(_local_agent(), kwargs)
    assert fitted["max_tokens"] == 131_072 - estimate_request_tokens_rough(kwargs["messages"]) - 1_024


def test_explicit_large_reasoning_budget_is_preserved():
    from agent.chat_completion_helpers import _fit_local_output_cap

    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        kwargs = {"messages": [{"role": "user", "content": "short request"}], key: 100_000}
        assert _fit_local_output_cap(_local_agent(), kwargs)[key] == 100_000
