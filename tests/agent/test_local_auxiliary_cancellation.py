# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Real SDK/socket proof of compression timeout, disconnect and admission."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest

from agent import auxiliary_client as aux
from agent.conversation_compression import (
    CompressionAttemptCancellation, CompressionCommitFence,
    run_compress_context_with_progress_timeout,
)
from hermes_cli.hussh_one_routing.local_runtime import LocalInferenceAdmission, LocalModelOverloaded


@pytest.fixture
def server():
    started, disconnected = threading.Event(), threading.Event()
    mode = {"sse": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if payload['messages'][0]['content'] == 'quick':
                body = json.dumps({'id': 'test', 'object': 'chat.completion', 'model': 'test',
                    'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if mode['sse']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"id":"test","object":"chat.completion.chunk","model":"test","choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n')
                self.wfile.flush()
            started.set()
            self.connection.settimeout(10)
            try:
                if not self.connection.recv(1):
                    disconnected.set()
            except (OSError, TimeoutError):
                pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{http.server_port}/v1'
    client = openai.OpenAI(base_url=url, api_key='test', max_retries=0)
    try:
        yield client, started, disconnected, mode, f'{url}|test'
    finally:
        client.close()
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)


def invoke(client, timeout=5):
    return aux._relay_sync_completion(client, {
        'model': 'test', 'messages': [{'role': 'user', 'content': 'stall'}], 'timeout': timeout,
    }, create=lambda owned, request: aux._create_with_progress(owned, request, 'compression', force_stream=True))


@pytest.mark.parametrize('sse', [False, True])
def test_timeout_disconnects_owned_request_before_next_admission(server, sse):
    client, started, disconnected, mode, key = server
    mode['sse'] = sse
    exited = threading.Event()
    original = [{'role': 'user', 'content': 'preserve'}]
    fence = CompressionCommitFence()

    def worker(commit_fence):
        try:
            with aux.aux_interrupt_protection(cancel_event=CompressionAttemptCancellation(commit_fence)):
                invoke(client)
            pytest.fail('stalled request unexpectedly completed')
        finally:
            exited.set()

    result, _ = run_compress_context_with_progress_timeout(
        worker=worker, messages=original, system_prompt_fallback='unchanged',
        idle_timeout_seconds=0.4, total_ceiling_seconds=1, fence=fence,
    )
    assert started.is_set()
    assert result is original
    assert fence.is_cancelled
    assert exited.wait(3)
    assert disconnected.wait(3), 'cancellation must disconnect the actual request'
    lease = LocalInferenceAdmission.acquire(key, wait=3)
    lease.release()
    assert not client.is_closed()
    response = client.chat.completions.create(model='test', messages=[{'role': 'user', 'content': 'quick'}])
    assert response.choices[0].message.content == 'ok'


def test_failed_abort_retains_capacity_until_worker_timeout(server, monkeypatch):
    from agent import agent_runtime_helpers
    client, started, disconnected, _, key = server
    monkeypatch.setattr(agent_runtime_helpers, 'force_close_tcp_sockets', lambda _: 0)
    cancel = threading.Event()
    owner_done = threading.Event()

    def owner():
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel):
                invoke(client, timeout=1.5)
        except aux.AuxiliaryExplicitCancellation:
            pass
        finally:
            owner_done.set()

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    assert started.wait(3)
    cancel.set()
    assert owner_done.wait(3)
    with pytest.raises(LocalModelOverloaded):
        LocalInferenceAdmission.acquire(key, wait=0)
    assert disconnected.wait(4)
    lease = LocalInferenceAdmission.acquire(key, wait=3)
    lease.release()
    thread.join(timeout=3)


def test_host_stop_and_fence_are_independent_sources():
    hard = threading.Event()
    fence = CompressionCommitFence()
    signal = CompressionAttemptCancellation(fence, hard)
    assert not signal.is_set()
    hard.set()
    assert signal.is_set()
    hard.clear()
    fence.try_cancel_before_commit()
    assert signal.is_set()


def test_cancel_during_client_creation_never_dispatches_or_releases_early(monkeypatch):
    from types import SimpleNamespace
    from agent import process_bootstrap
    from agent.local_auxiliary_request import LocalAuxiliaryRequest

    creating, proceed, closed = threading.Event(), threading.Event(), threading.Event()
    key = 'late-client-registration'
    calls, outcomes = [], []
    owned = SimpleNamespace(close=closed.set)
    def copy_client(**_kwargs):
        creating.set()
        assert proceed.wait(3)
        return owned
    shared = SimpleNamespace(base_url='http://127.0.0.1:1234/v1', copy=copy_client)
    monkeypatch.setattr(process_bootstrap, 'build_keepalive_http_client', lambda *_a, **_k: SimpleNamespace(close=lambda: None))
    request = LocalAuxiliaryRequest(shared, lambda: LocalInferenceAdmission.acquire(key))
    def run():
        try:
            request.run({}, lambda *_args: calls.append('dispatched'))
        except aux.AuxiliaryExplicitCancellation:
            outcomes.append('cancelled')
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert creating.wait(3)
        request.cancel()
        with pytest.raises(LocalModelOverloaded):
            LocalInferenceAdmission.acquire(key, wait=0)
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()
        assert outcomes == ['cancelled']
        assert calls == []
        assert closed.is_set() and request.done.is_set()
        LocalInferenceAdmission.acquire(key, wait=0).release()
    finally:
        proceed.set()
        worker.join(3)
