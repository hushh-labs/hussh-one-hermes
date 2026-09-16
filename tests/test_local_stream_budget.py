# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Run-budget bounds for local streaming inference."""

import time
from types import SimpleNamespace


def _local_agent(*, budget=None, started=None):
    return SimpleNamespace(
        base_url="http://127.0.0.1:1234/v1",
        run_budget_seconds=budget,
        _run_budget_started_at=started,
    )


def test_local_stream_stale_timeout_is_capped_by_run_budget():
    from agent.chat_completion_helpers import _cap_local_stream_stale_timeout

    agent = _local_agent(budget=60, started=time.time() - 10)
    timeout = _cap_local_stream_stale_timeout(agent, 1_800.0)

    assert timeout == 1_800.0


def test_local_stream_timeout_remains_generous_without_budget():
    from agent.chat_completion_helpers import _cap_local_stream_stale_timeout

    agent = _local_agent()

    assert _cap_local_stream_stale_timeout(agent, 900.0) == 900.0


def test_remote_stream_timeout_is_not_rewritten():
    from agent.chat_completion_helpers import _cap_local_stream_stale_timeout

    agent = SimpleNamespace(
        base_url="https://api.example.test/v1",
        run_budget_seconds=60,
        _run_budget_started_at=time.time() - 10,
    )

    assert _cap_local_stream_stale_timeout(agent, 180.0) == 180.0


def test_expired_local_attempt_fails_before_retry():
    import pytest
    from agent.chat_completion_helpers import _check_local_attempt_deadline, LocalAttemptDeadlineExceeded
    with pytest.raises(LocalAttemptDeadlineExceeded):
        _check_local_attempt_deadline(_local_agent(budget=60, started=time.time() - 61))


def test_unbudgeted_local_attempt_can_continue():
    from agent.chat_completion_helpers import _check_local_attempt_deadline
    _check_local_attempt_deadline(_local_agent())


def test_local_responses_stream_uses_local_stale_budget(monkeypatch):
    from agent.chat_completion_helpers import _local_stream_stale_timeout

    monkeypatch.delenv("HERMES_STREAM_STALE_TIMEOUT", raising=False)
    monkeypatch.setenv("HERMES_LOCAL_STREAM_STALE_TIMEOUT", "321")
    agent = _local_agent()
    agent.provider = "lmstudio"
    agent.model = "meta/muse-glimmer"

    assert _local_stream_stale_timeout(agent) == 321.0


def test_explicit_generic_stale_budget_keeps_precedence(monkeypatch):
    from agent.chat_completion_helpers import _local_stream_stale_timeout

    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "77")
    monkeypatch.setenv("HERMES_LOCAL_STREAM_STALE_TIMEOUT", "321")
    agent = _local_agent()

    assert _local_stream_stale_timeout(agent) == 77.0


def test_remote_responses_stream_has_no_local_stale_budget():
    from agent.chat_completion_helpers import _local_stream_stale_timeout

    agent = SimpleNamespace(
        base_url="https://api.example.test/v1",
        provider="openai",
        model="gpt-test",
    )

    assert _local_stream_stale_timeout(agent) is None


def test_local_prefill_notice_identifies_active_connection_without_content():
    from agent.chat_completion_helpers import local_prefill_wait_notice

    agent = _local_agent()
    agent.model = "meta/muse-glimmer"
    notice = local_prefill_wait_notice(
        agent,
        {
            "model": "meta/muse-glimmer",
            "instructions": "system guidance",
            "input": [{"role": "user", "content": "hello"}],
        },
        elapsed=37,
        stale_timeout=1_800,
    )

    assert notice is not None
    assert "meta/muse-glimmer" in notice
    assert "37s elapsed" in notice
    assert "connection is active" in notice
    assert "1800s" in notice
    assert "hello" not in notice


def test_remote_prefill_notice_is_not_emitted():
    from agent.chat_completion_helpers import local_prefill_wait_notice

    agent = SimpleNamespace(base_url="https://api.example.test/v1", model="gpt-test")

    assert local_prefill_wait_notice(agent, {"model": "gpt-test"}) is None
