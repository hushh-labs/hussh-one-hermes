# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Foreground admission respects cancellation and preserves capacity."""
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import _acquire_local_for_agent
from hermes_cli.hussh_one_routing.local_runtime import LocalInferenceAdmission


@pytest.mark.parametrize('cancelled', [False, True])
def test_queued_turn_resumes_or_cancels_without_leaking_capacity(cancelled):
    key = f'admission-test-{uuid.uuid4().hex}'
    held = LocalInferenceAdmission.acquire(key)
    waiting = threading.Event()
    cancel = threading.Event()
    events, results = [], []

    def status(_kind, message):
        events.append(message)
        waiting.set()

    agent = SimpleNamespace(base_url='http://127.0.0.1:1234/v1',
                            run_budget_seconds=5, _run_budget_started_at=time.time(),
                            status_callback=status)
    runtime = SimpleNamespace(acquire=lambda _model, **kw: LocalInferenceAdmission.acquire(key, **kw))

    def check_attempt():
        if cancel.is_set():
            raise InterruptedError('attempt stopped')

    def worker():
        try:
            lease = _acquire_local_for_agent(agent, runtime, 'test', attempt_cancel_check=check_attempt)
            results.append('acquired')
            lease.release()
            lease.release()
        except InterruptedError:
            results.append('cancelled')

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert waiting.wait(2)
        if cancelled:
            cancel.set()
            thread.join(2)
            assert not thread.is_alive()
        held.release()
        thread.join(2)
        assert not thread.is_alive()
        assert results == ['cancelled' if cancelled else 'acquired']
        assert any('queued' in event for event in events)
        if not cancelled:
            assert any('resuming' in event for event in events)
        check = LocalInferenceAdmission.acquire(key, wait=0)
        check.release()
    finally:
        cancel.set()
        held.release()
        thread.join(2)


def test_status_failure_does_not_leak_granted_permit():
    key = f'admission-test-{uuid.uuid4().hex}'
    def broken(*_args):
        raise RuntimeError('display unavailable')
    agent = SimpleNamespace(base_url='http://127.0.0.1:1234/v1', status_callback=broken)
    def acquire(_model, **kwargs):
        kwargs['on_wait']()
        return LocalInferenceAdmission.acquire(key, wait=0)
    lease = _acquire_local_for_agent(agent, SimpleNamespace(acquire=acquire), 'test')
    lease.release()
    LocalInferenceAdmission.acquire(key, wait=0).release()


@pytest.mark.parametrize('direct', [False, True])
def test_actual_nonstream_watchdog_removes_queued_attempt_before_dispatch(monkeypatch, direct):
    from unittest.mock import Mock
    from agent import chat_completion_helpers as helpers
    from run_agent import AIAgent

    key = f'watchdog-admission-{uuid.uuid4().hex}'
    held = LocalInferenceAdmission.acquire(key)
    queue_exited = threading.Event()
    def acquire(_model, **kwargs):
        try:
            return LocalInferenceAdmission.acquire(key, **kwargs)
        finally:
            queue_exited.set()
    runtime = SimpleNamespace(acquire=acquire)
    agent = AIAgent(api_key='synthetic', base_url='http://127.0.0.1:1234/v1',
                    model='synthetic', quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._compute_non_stream_stale_timeout = lambda _kwargs: 0.05
    create_client = Mock(side_effect=AssertionError('abandoned attempt dispatched'))
    agent._create_request_openai_client = create_client
    monkeypatch.setattr(helpers, '_local_runtime_for_agent', lambda *_args: runtime)
    monkeypatch.setattr(helpers, 'should_use_direct_api_call', lambda _agent: direct)
    try:
        with pytest.raises(TimeoutError):
            helpers.interruptible_api_call(agent, {'model': 'synthetic', 'messages': []})
        assert queue_exited.wait(2)
        held.release()
        create_client.assert_not_called()
        LocalInferenceAdmission.acquire(key, wait=0).release()
    finally:
        held.release()
