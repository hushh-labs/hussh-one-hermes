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
