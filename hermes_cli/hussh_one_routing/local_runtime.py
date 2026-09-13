# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Bounded local-model readiness, admission, and attempt bookkeeping.

LM Studio exposes a stable OpenAI-compatible front server (normally port
1234) while its llama-server worker may use another port.  Hermes must route
through the front server, verify the resident model before sending work, and
avoid letting background calls consume every local inference slot.

This module deliberately has no model fallback policy.  It supplies typed
readiness and admission signals to the existing agent retry/fallback owner and
keeps all probing loopback-only.  It is safe to use from gateway threads and
from the synchronous cron executor.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_FRONT_URL = "http://127.0.0.1:1234/v1"
DEFAULT_PROBE_TIMEOUT_S = 3.0
DEFAULT_CACHE_TTL_S = 5.0
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_ADMISSION_WAIT_S = 0.25
DEFAULT_CIRCUIT_THRESHOLD = 3
DEFAULT_CIRCUIT_COOLDOWN_S = 30.0


class ReadinessState(str, Enum):
    READY = "READY"
    LOADING = "LOADING"
    OVERLOADED = "OVERLOADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ModelReadiness:
    """A redacted, cacheable local-model readiness result."""

    state: ReadinessState
    base_url: str
    model: str
    loaded_context_length: Optional[int] = None
    available_models: tuple[str, ...] = ()
    reason: str = ""
    observed_at: float = 0.0

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY


class LocalModelUnavailable(RuntimeError):
    """The configured local model cannot accept a request right now."""

    def __init__(self, readiness: ModelReadiness):
        self.readiness = readiness
        super().__init__(
            f"local model {readiness.model!r} is {readiness.state.value.lower()}: "
            f"{readiness.reason or 'readiness probe failed'}"
        )


class LocalModelOverloaded(RuntimeError):
    """A bounded local inference slot was unavailable within the wait budget."""


class LocalCircuitOpen(RuntimeError):
    """Repeated identical local failures are temporarily short-circuited."""


def _loopback(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def normalize_front_url(base_url: str | None) -> str:
    """Normalize a configured OpenAI base URL and reject non-loopback routes."""

    value = (base_url or DEFAULT_FRONT_URL).strip().rstrip("/")
    if not _loopback(value):
        raise ValueError("local model endpoint must be loopback-only")
    if not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


def _model_entries(payload: Any) -> list[dict[str, Any]]:
    """Read both OpenAI ``data`` and LM Studio native ``models`` envelopes."""

    if not isinstance(payload, dict):
        return []
    raw = payload.get("data")
    if not isinstance(raw, list):
        raw = payload.get("models")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _entry_id(entry: dict[str, Any]) -> str:
    for key in ("id", "key", "model", "identifier"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entry_context(entry: dict[str, Any]) -> Optional[int]:
    for key in ("loaded_context_length", "context_length"):
        value = entry.get(key)
        if isinstance(value, int) and value > 0:
            return value
    instances = entry.get("loaded_instances")
    if isinstance(instances, list):
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            config = instance.get("config")
            if isinstance(config, dict):
                value = config.get("context_length")
                if isinstance(value, int) and value > 0:
                    return value
    value = entry.get("max_context_length")
    return value if isinstance(value, int) and value > 0 else None


def _entry_state(entry: dict[str, Any]) -> str:
    if entry.get("loaded_instances") == []:
        return "unloaded"
    for key in ("state", "status", "loading_state"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


class LocalModelResolver:
    """Discover and pin a verified LM Studio front route in memory."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT_S,
        cache_ttl: float = DEFAULT_CACHE_TTL_S,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = normalize_front_url(base_url)
        self.api_key = api_key or ""
        self.timeout = max(0.5, float(timeout))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._cache: dict[str, ModelReadiness] = {}

    def _request(self, url: str) -> Any:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        with self._opener(request, timeout=self.timeout) as response:
            return json.load(response)

    def resolve(self, model: str, *, force: bool = False) -> ModelReadiness:
        model = str(model or "").strip()
        if not model:
            return ModelReadiness(
                ReadinessState.UNAVAILABLE,
                self.base_url,
                model,
                reason="no local model was configured",
                observed_at=time.monotonic(),
            )
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(model)
            if (
                cached is not None
                and not force
                and now - cached.observed_at < self.cache_ttl
            ):
                return cached
        result = self._probe(model)
        with self._lock:
            self._cache[model] = result
        logger.info(
            "local_model_readiness state=%s model=%s context=%s reason=%s",
            result.state.value,
            model,
            result.loaded_context_length or "unknown",
            result.reason or "-",
        )
        return result

    def _probe(self, model: str) -> ModelReadiness:
        observed = time.monotonic()
        try:
            # Probe both inventories.  The OpenAI-compatible route is the
            # request path Hermes pins, while the native route supplies the
            # loaded instance state and effective context capacity on LM
            # Studio versions that omit those fields from /v1/models.
            try:
                payload = self._request(f"{self.base_url}/models")
                entries = _model_entries(payload)
            except urllib.error.HTTPError as exc:
                # A native-only LM Studio build can omit the OpenAI inventory;
                # other HTTP failures still describe the front route's state.
                if exc.code != 404:
                    raise
                entries = []
            root = self.base_url[: -len("/v1")]
            native_entries: list[dict[str, Any]] = []
            openai_match = next((entry for entry in entries if _entry_id(entry) == model), None)
            if openai_match is None or _entry_context(openai_match) is None or not _entry_state(openai_match):
                for native_path in ("/api/v1/models", "/api/v0/models"):
                    try:
                        native_entries = _model_entries(self._request(f"{root}{native_path}"))
                    except Exception:
                        # Native inventory is supplemental.  Older LM Studio
                        # builds may expose only one native version, and a test or
                        # compatibility server may expose neither.
                        continue
                    if native_entries:
                        break
            all_entries = [*entries, *native_entries]
            ids = tuple(
                sorted(
                    {
                        item_id
                        for item_id in (_entry_id(e) for e in [*entries, *native_entries])
                        if item_id
                    }
                )
            )
            match = next((entry for entry in all_entries if _entry_id(entry) == model), None)
            native_match = next(
                (entry for entry in native_entries if _entry_id(entry) == model), None
            )
            if match is None:
                return ModelReadiness(
                    ReadinessState.UNAVAILABLE,
                    self.base_url,
                    model,
                    available_models=ids,
                    reason="model is not present in the local inventory",
                    observed_at=observed,
                )
            # Prefer native state/capacity when available, but retain the
            # OpenAI inventory as the authoritative list of client models.
            state = _entry_state(native_match or match) or _entry_state(match)
            context_length = _entry_context(native_match or match)
            if context_length is None:
                context_length = _entry_context(match)
            if state in {"loading", "starting", "queued", "unloaded"}:
                readiness = ReadinessState.LOADING
            elif state in {"busy", "generating", "overloaded"}:
                readiness = ReadinessState.OVERLOADED
            else:
                readiness = ReadinessState.READY
            return ModelReadiness(
                readiness,
                self.base_url,
                model,
                loaded_context_length=context_length,
                available_models=ids,
                reason=state or "inventory available",
                observed_at=observed,
            )
        except urllib.error.HTTPError as exc:
            state = (
                ReadinessState.OVERLOADED
                if exc.code in {408, 425, 429, 500, 502, 503, 504, 529}
                else ReadinessState.UNAVAILABLE
            )
            return ModelReadiness(
                state,
                self.base_url,
                model,
                reason=f"http_{exc.code}",
                observed_at=observed,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            return ModelReadiness(
                ReadinessState.UNAVAILABLE,
                self.base_url,
                model,
                reason=f"{type(exc).__name__}: {exc}",
                observed_at=observed,
            )

    def require_ready(self, model: str, *, force: bool = False) -> ModelReadiness:
        result = self.resolve(model, force=force)
        if not result.ready:
            raise LocalModelUnavailable(result)
        return result

    def invalidate(self, model: str | None = None) -> None:
        """Drop cached readiness after a transport failure or model change."""
        with self._lock:
            if model is None:
                self._cache.clear()
            else:
                self._cache.pop(str(model).strip(), None)


@dataclass
class _AdmissionPool:
    limit: int
    condition: threading.Condition = field(default_factory=threading.Condition)
    active: int = 0
    waiters: int = 0
    interactive_waiters: int = 0
    background_waiters: int = 0


class AdmissionLease:
    """A single bounded local inference permit."""

    def __init__(self, pool: _AdmissionPool, key: str) -> None:
        self._pool = pool
        self.key = key
        self._released = False
        self._process_permit = None
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            if self._process_permit is not None:
                self._process_permit.release()
            self._released = True
        with self._pool.condition:
            self._pool.active = max(0, self._pool.active - 1)
            self._pool.condition.notify_all()

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class LocalInferenceAdmission:
    """Process-wide admission control shared by chat, relay, and cron."""

    _pools: dict[str, _AdmissionPool] = {}
    _lock = threading.RLock()

    @classmethod
    def acquire(
        cls,
        key: str,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        wait: float = DEFAULT_ADMISSION_WAIT_S,
        priority: str = "interactive",
    ) -> AdmissionLease:
        from agent.local_admission import acquire_process_permit

        lease = cls._acquire_thread_permit(
            key, max_concurrency=max_concurrency, wait=wait, priority=priority
        )
        try:
            lease._process_permit = acquire_process_permit(
                key, max_concurrency, wait, priority
            )
            return lease
        except TimeoutError as exc:
            lease.release()
            raise LocalModelOverloaded(str(exc)) from exc
        except BaseException:
            lease.release()
            raise

    @classmethod
    def _acquire_thread_permit(
        cls, key: str, *, max_concurrency: int, wait: float, priority: str,
    ) -> AdmissionLease:
        key = str(key or "local").strip()
        limit = max(1, int(max_concurrency))
        priority = "background" if str(priority).strip().lower() == "background" else "interactive"
        with cls._lock:
            pool = cls._pools.get(key)
            if pool is None:
                pool = _AdmissionPool(limit)
                cls._pools[key] = pool
        timeout = max(0.0, float(wait))
        deadline = time.monotonic() + timeout
        with pool.condition:
            pool.waiters += 1
            if priority == "interactive":
                pool.interactive_waiters += 1
            else:
                pool.background_waiters += 1
            try:
                while True:
                    interactive_priority = (
                        priority == "interactive" or pool.interactive_waiters == 0
                    )
                    if pool.active < pool.limit and interactive_priority:
                        pool.active += 1
                        return AdmissionLease(pool, key)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LocalModelOverloaded(
                            f"local inference admission is full for {key}; "
                            f"priority={priority} waited {timeout:.2f}s"
                        )
                    pool.condition.wait(timeout=remaining)
            finally:
                pool.waiters = max(0, pool.waiters - 1)
                if priority == "interactive":
                    pool.interactive_waiters = max(0, pool.interactive_waiters - 1)
                else:
                    pool.background_waiters = max(0, pool.background_waiters - 1)
                pool.condition.notify_all()


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float = 0.0
    fingerprint: str = ""


class LocalInferenceCircuit:
    """Short-circuit repeated identical local failures across sessions."""

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_CIRCUIT_THRESHOLD,
        cooldown: float = DEFAULT_CIRCUIT_COOLDOWN_S,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown = max(0.0, float(cooldown))
        self._states: dict[str, _CircuitState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def fingerprint(error: BaseException | str) -> str:
        message = str(error).splitlines()[0][:240]
        return hashlib.sha256(message.encode("utf-8", "replace")).hexdigest()[:16]

    def allow(self, key: str) -> bool:
        with self._lock:
            state = self._states.get(key)
            if state is None or not state.opened_at:
                return True
            if time.monotonic() - state.opened_at >= self.cooldown:
                state.opened_at = 0.0
                state.failures = 0
                state.fingerprint = ""
                return True
            return False

    def before(self, key: str) -> None:
        if not self.allow(key):
            raise LocalCircuitOpen(
                f"local inference circuit is open for {key}; "
                f"retrying after the cooldown would repeat the same failure"
            )

    def record_failure(self, key: str, error: BaseException | str) -> None:
        fingerprint = self.fingerprint(error)
        with self._lock:
            state = self._states.setdefault(key, _CircuitState())
            if state.fingerprint != fingerprint:
                state.failures = 0
                state.opened_at = 0.0
                state.fingerprint = fingerprint
            state.failures += 1
            if state.failures >= self.threshold:
                state.opened_at = time.monotonic()
                logger.warning(
                    "local_model_circuit_open key=%s failures=%d fingerprint=%s cooldown=%.1fs",
                    key,
                    state.failures,
                    fingerprint,
                    self.cooldown,
                )

    def record_success(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)


_RUNTIMES: dict[tuple[str, str], "LocalRuntime"] = {}
_RUNTIMES_LOCK = threading.RLock()


class LocalRuntime:
    """Composition root for readiness, admission, and circuit state."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        self.resolver = LocalModelResolver(base_url=base_url, api_key=api_key)
        self.circuit = LocalInferenceCircuit()

    def key(self, model: str) -> str:
        return f"{self.resolver.base_url}|{model}"

    def acquire(
        self,
        model: str,
        *,
        wait: float = DEFAULT_ADMISSION_WAIT_S,
        priority: str = "interactive",
    ) -> AdmissionLease:
        model = str(model or "").strip()
        key = self.key(model)
        self.circuit.before(key)
        try:
            self.resolver.require_ready(model)
        except BaseException as exc:
            self.resolver.invalidate(model)
            self.circuit.record_failure(key, exc)
            raise
        return LocalInferenceAdmission.acquire(key, wait=wait, priority=priority)

    def record_success(self, model: str) -> None:
        self.circuit.record_success(self.key(model))

    def record_failure(self, model: str, error: BaseException | str) -> None:
        self.resolver.invalidate(model)
        self.circuit.record_failure(self.key(model), error)


def runtime_for(base_url: str | None, api_key: str | None = None) -> LocalRuntime:
    """Return a process-shared runtime for one verified front route."""

    normalized = normalize_front_url(base_url)
    cache_key = (normalized, api_key or "")
    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.get(cache_key)
        if runtime is None:
            runtime = LocalRuntime(base_url=normalized, api_key=api_key)
            _RUNTIMES[cache_key] = runtime
        return runtime


class LeasedStream:
    """Proxy a synchronous provider stream and release its local slot exactly once."""

    def __init__(
        self,
        stream: Any,
        lease: AdmissionLease,
        runtime: LocalRuntime,
        model: str,
        on_finish: Optional[Callable[[bool, BaseException | None], None]] = None,
    ) -> None:
        self._stream = stream
        self._lease = lease
        self._runtime = runtime
        self._model = model
        self._closed = False
        self._finished = False
        self._on_finish = on_finish

    def _finish(self, success: bool, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._on_finish is not None:
            try:
                self._on_finish(success, error)
            except Exception:
                logger.debug("local attempt ledger finish failed", exc_info=True)

    def __iter__(self) -> Iterator[Any]:
        try:
            yield from self._stream
        except BaseException as exc:
            self._runtime.record_failure(self._model, exc)
            self._finish(False, exc)
            raise
        else:
            self._runtime.record_success(self._model)
            self._finish(True, None)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        finally:
            if not self._finished:
                error = RuntimeError("local provider stream closed before completion")
                self._runtime.record_failure(self._model, error)
                self._finish(False, error)
            self._lease.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextlib.contextmanager
def guarded_call(
    *,
    base_url: str | None,
    model: str,
    api_key: str | None = None,
    wait: float = DEFAULT_ADMISSION_WAIT_S,
    priority: str = "interactive",
) -> Iterator[LocalRuntime | None]:
    """Guard one synchronous local call; non-local routes are a no-op."""

    if not _loopback(str(base_url or "")):
        yield None
        return
    runtime = runtime_for(base_url, api_key)
    lease = runtime.acquire(model, wait=wait, priority=priority)
    try:
        yield runtime
    except BaseException as exc:
        runtime.record_failure(model, exc)
        raise
    else:
        runtime.record_success(model)
    finally:
        lease.release()


__all__ = [
    "AdmissionLease",
    "LeasedStream",
    "LocalCircuitOpen",
    "LocalInferenceAdmission",
    "LocalInferenceCircuit",
    "LocalModelOverloaded",
    "LocalModelResolver",
    "LocalModelUnavailable",
    "LocalRuntime",
    "ModelReadiness",
    "ReadinessState",
    "guarded_call",
    "normalize_front_url",
    "runtime_for",
]
