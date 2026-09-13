"""Contract tests for the shared local-model runtime gate."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error

import pytest

from hermes_cli.hussh_one_routing.local_runtime import (
    LocalInferenceAdmission,
    LocalInferenceCircuit,
    LocalCircuitOpen,
    LocalModelOverloaded,
    LocalModelResolver,
    LocalRuntime,
    LocalModelUnavailable,
    ReadinessState,
    normalize_front_url,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Opener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def __call__(self, request, *, timeout):
        self.urls.append(request.full_url)
        payload = self.payloads.pop(0)
        return _Response(json.dumps(payload).encode())


def test_normalize_front_url_rejects_remote_and_keeps_lm_studio_front_route():
    assert normalize_front_url("http://localhost:1234") == "http://localhost:1234/v1"
    with pytest.raises(ValueError, match="loopback"):
        normalize_front_url("https://example.invalid/v1")


def test_resolver_reads_loaded_context_and_caches_verified_route():
    opener = _Opener([
        {"data": [{"id": "meta/muse-glimmer", "state": "loaded", "loaded_context_length": 131072}]}
    ])
    resolver = LocalModelResolver(
        base_url="http://127.0.0.1:1234/v1", opener=opener, cache_ttl=60
    )
    first = resolver.require_ready("meta/muse-glimmer")
    second = resolver.require_ready("meta/muse-glimmer")
    assert first.state is ReadinessState.READY
    assert first.loaded_context_length == 131072
    assert second == first
    assert opener.urls == ["http://127.0.0.1:1234/v1/models"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [("loading", ReadinessState.LOADING), ("busy", ReadinessState.OVERLOADED)],
)
def test_resolver_exposes_non_ready_states(state, expected):
    resolver = LocalModelResolver(
        opener=_Opener([{"data": [{"id": "m", "state": state}]}])
    )
    assert resolver.resolve("m").state is expected


def test_resolver_classifies_http_throttling_as_overloaded():
    def opener(_request, *, timeout):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:1234/v1/models", 429, "busy", {}, io.BytesIO()
        )

    result = LocalModelResolver(opener=opener).resolve("m")
    assert result.state is ReadinessState.OVERLOADED
    assert result.reason == "http_429"


def test_resolver_falls_back_to_native_inventory_without_changing_client_route():
    opener = _Opener([
        {"data": []},
        {"models": [{"key": "meta/muse-glimmer", "loaded_instances": [{"config": {"context_length": 65536}}]}]},
    ])
    resolver = LocalModelResolver(opener=opener)
    result = resolver.require_ready("meta/muse-glimmer")
    assert result.base_url.endswith("/v1")
    assert result.loaded_context_length == 65536
    assert opener.urls == [
        "http://127.0.0.1:1234/v1/models",
        "http://127.0.0.1:1234/api/v1/models",
    ]


def test_resolver_accepts_native_only_inventory_when_front_models_is_missing():
    def opener(request, *, timeout):
        if request.full_url.endswith("/v1/models"):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, io.BytesIO())
        return _Response(json.dumps({"models": [{
            "key": "meta/muse-glimmer",
            "state": "loaded",
            "max_context_length": 131072,
        }]}).encode())

    result = LocalModelResolver(opener=opener).require_ready("meta/muse-glimmer")
    assert result.state is ReadinessState.READY
    assert result.loaded_context_length == 131072


def test_resolver_uses_native_context_when_openai_inventory_omits_capacity():
    opener = _Opener([
        {"data": [{"id": "meta/muse-glimmer"}]},
        {"models": [{
            "key": "meta/muse-glimmer",
            "state": "loaded",
            "max_context_length": 131072,
            "loaded_instances": [{"config": {"context_length": 131072}}],
        }]},
    ])
    resolver = LocalModelResolver(opener=opener)
    result = resolver.require_ready("meta/muse-glimmer")
    assert result.state is ReadinessState.READY
    assert result.loaded_context_length == 131072
    assert opener.urls == [
        "http://127.0.0.1:1234/v1/models",
        "http://127.0.0.1:1234/api/v1/models",
    ]


def test_resolver_invalidation_forces_a_fresh_inventory_probe():
    opener = _Opener([
        {"data": [{"id": "m", "state": "loaded", "context_length": 4096}]},
        {"data": [{"id": "m", "state": "loaded", "context_length": 8192}]},
    ])
    resolver = LocalModelResolver(opener=opener, cache_ttl=60)
    assert resolver.require_ready("m").loaded_context_length == 4096
    resolver.invalidate("m")
    assert resolver.require_ready("m").loaded_context_length == 8192
    assert opener.urls == [
        "http://127.0.0.1:1234/v1/models",
        "http://127.0.0.1:1234/v1/models",
    ]


def test_admission_is_bounded_and_lease_release_is_idempotent():
    key = "test-admission-" + str(time.time_ns())
    first = LocalInferenceAdmission.acquire(key, wait=0)
    try:
        with pytest.raises(LocalModelOverloaded):
            LocalInferenceAdmission.acquire(key, wait=0.01)
    finally:
        first.release()
        first.release()
    second = LocalInferenceAdmission.acquire(key, wait=0)
    second.release()


def test_circuit_opens_only_for_repeated_identical_failures_and_recovers():
    circuit = LocalInferenceCircuit(threshold=2, cooldown=0.01)
    circuit.record_failure("k", RuntimeError("stall"))
    assert circuit.allow("k")
    circuit.record_failure("k", RuntimeError("stall"))
    assert not circuit.allow("k")
    time.sleep(0.02)
    assert circuit.allow("k")


def test_runtime_records_repeated_preflight_outages_without_restarting_process():
    runtime = LocalRuntime(base_url="http://127.0.0.1:1234/v1")
    runtime.circuit.threshold = 2

    def opener(_request, *, timeout):
        raise OSError("front API unavailable")

    runtime.resolver._opener = opener
    with pytest.raises(LocalModelUnavailable):
        runtime.acquire("meta/muse-glimmer")
    with pytest.raises(LocalModelUnavailable):
        runtime.acquire("meta/muse-glimmer")
    with pytest.raises(LocalCircuitOpen):
        runtime.acquire("meta/muse-glimmer")


def test_admission_wait_does_not_block_other_threads_forever():
    key = "thread-admission-" + str(time.time_ns())
    lease = LocalInferenceAdmission.acquire(key, wait=0)
    result = []

    def attempt():
        try:
            LocalInferenceAdmission.acquire(key, wait=0.02)
        except LocalModelOverloaded:
            result.append(True)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=1)
    lease.release()
    assert result == [True]


def test_interactive_waiter_preempts_queued_background_work():
    key = "priority-admission-" + str(time.time_ns())
    first = LocalInferenceAdmission.acquire(key, wait=0)
    background_started = threading.Event()
    interactive_started = threading.Event()
    background_acquired = threading.Event()
    interactive_acquired = threading.Event()
    release_interactive = threading.Event()

    def background():
        background_started.set()
        lease = LocalInferenceAdmission.acquire(
            key, wait=1.0, priority="background"
        )
        background_acquired.set()
        lease.release()

    def interactive():
        interactive_started.set()
        lease = LocalInferenceAdmission.acquire(
            key, wait=1.0, priority="interactive"
        )
        interactive_acquired.set()
        release_interactive.wait(timeout=1.0)
        lease.release()

    background_thread = threading.Thread(target=background)
    interactive_thread = threading.Thread(target=interactive)
    background_thread.start()
    assert background_started.wait(timeout=1.0)
    interactive_thread.start()
    assert interactive_started.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with LocalInferenceAdmission._lock:
            pool = LocalInferenceAdmission._pools.get(key)
        if pool is not None:
            with pool.condition:
                if pool.interactive_waiters:
                    break
        time.sleep(0.005)
    try:
        first.release()
        assert interactive_acquired.wait(timeout=1.0)
        assert not background_acquired.is_set()
    finally:
        release_interactive.set()
        interactive_thread.join(timeout=1.0)
        background_thread.join(timeout=1.0)
    assert background_acquired.is_set()


def test_loaded_instance_capacity_wins_over_catalog_maximum():
    from hermes_cli.hussh_one_routing.local_runtime import _entry_context
    assert _entry_context({"max_context_length": 131072, "loaded_instances": [{"config": {"context_length": 32768}}]}) == 32768


def test_changing_limit_does_not_replace_active_pool():
    lease = LocalInferenceAdmission.acquire("stable-pool", max_concurrency=1)
    try:
        with pytest.raises(LocalModelOverloaded):
            LocalInferenceAdmission.acquire("stable-pool", max_concurrency=2, wait=0)
    finally:
        lease.release()
