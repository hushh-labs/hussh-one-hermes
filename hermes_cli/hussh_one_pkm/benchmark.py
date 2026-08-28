# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Benchmark the PKM 0-to-1 unit on the model running on this machine.

The unit a user actually waits through has two halves, and they are timed
separately here because they scale on completely different things:

    T_model   the local model turn that reads an utterance and emits a
              well-formed save_to_pkm call. Scales with model size, quant and
              memory bandwidth.
    T_commit  HusshPkmWriteService.save -- vault key, propose, approve, commit.
              Crypto plus two HTTP round-trips plus a DB write. Scales with
              network and payload, and is nearly model-independent.

Summing them into one "PKM save took N ms" would hide which half a regression
landed in, and would make a 40b model look like a network problem. Nothing here
ever reports a single blended number.

A tool call that arrives fast but malformed is not a fast result, so validity is
scored alongside latency: a model is only credited when the call it produced
carries every field save_to_pkm requires.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from hermes_cli.hussh_one_lmstudio import (
    DEFAULT_SERVER_ROOT,
    ensure_capacity,
    host_memory,
    loaded_models,
)

logger = logging.getLogger(__name__)

# Loopback only. The whole claim of this benchmark is that the work happened on
# this machine, so a result measured against a remote host is not a weaker
# result, it is a different claim wearing this one's name.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# The fields save_to_pkm declares required (tools/hussh_one_pkm_tool.py).
_REQUIRED_TOOL_FIELDS = ("domain", "scope_path", "merge_patch", "summary")

_TOOL_NAME = "save_to_pkm"

_SYSTEM_PROMPT = (
    "You maintain the owner's private Hussh One PKM on their own machine. "
    "When the owner states a durable fact about themselves, call save_to_pkm "
    "once with a specific scope_path and a minimal merge_patch. Do not ask "
    "follow-up questions and do not narrate your reasoning."
)

# The save_to_pkm schema, verbatim in shape, so the model is asked for exactly
# what the real tool would accept.
_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": (
            "Save one encrypted create, update, merge or delete operation in "
            "the owner's Hussh One PKM."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "scope_path": {"type": "string"},
                "merge_patch": {"type": "object"},
                "summary": {"type": "string", "maxLength": 500},
                "operation": {
                    "type": "string",
                    "enum": [
                        "upsert",
                        "create",
                        "update",
                        "merge",
                        "delete_path",
                        "delete_scope",
                        "delete_domain",
                    ],
                    "default": "upsert",
                },
            },
            "required": list(_REQUIRED_TOOL_FIELDS),
            "additionalProperties": False,
        },
    },
}

# Utterances a PKM save actually starts from. Kept deliberately plain: the point
# is to measure the local model on the real task, not to find a prompt that
# flatters it.
DEFAULT_CORPUS: tuple[dict[str, str], ...] = (
    {
        "id": "diet-restriction",
        "utterance": "I stopped eating dairy in January, it was wrecking my sinuses.",
        "expect_domain_hint": "health",
    },
    {
        "id": "travel-preference",
        "utterance": "Always book me an aisle seat, I get up too often for a window.",
        "expect_domain_hint": "travel",
    },
    {
        "id": "work-context",
        "utterance": "I moved off the payments team and I run the trust org now.",
        "expect_domain_hint": "work",
    },
    {
        "id": "finance-detail",
        "utterance": "My mortgage renews in March 2027 and I want to refinance before then.",
        "expect_domain_hint": "finance",
    },
    {
        "id": "relationship",
        "utterance": "My daughter Maya started at Berkeley this fall, studying architecture.",
        "expect_domain_hint": "relationships",
    },
)

# The ladder from the plan: small to large, plus a cross-family MoE control so a
# result is not just a story about one architecture.
DEFAULT_MODEL_LADDER: tuple[str, ...] = (
    "google/gemma-4-e2b",
    "google/gemma-4-12b-qat",
    "google/gemma-4-26b-a4b-qat",
    "google/gemma-4-31b-qat",
    "qwen/qwen3.6-35b-a3b",
)


class RemoteHostRefused(RuntimeError):
    """Raised when the benchmark is pointed somewhere that is not this machine."""


def assert_loopback(base_url: str) -> None:
    """Refuse any host that is not this machine.

    Fails closed and by hostname, never by substring: a URL like
    http://127.0.0.1.evil.example would pass a naive containment check while
    resolving somewhere else entirely.
    """
    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.hostname or "").strip().casefold()
    if host not in _LOOPBACK_HOSTS:
        raise RemoteHostRefused(
            f"on-device benchmark refuses non-loopback host {host or base_url!r}; "
            "a result measured off this machine is a different claim"
        )


def pct(values: Sequence[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile.

    Returns None for an empty sample rather than 0.0: an unmeasured percentile
    is absent, and zero milliseconds is the most flattering possible reading of
    no data.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1.0 - weight) + ordered[high] * weight, 2)


def score_tool_call(payload: Any) -> dict[str, Any]:
    """Judge one model response against what save_to_pkm would actually accept.

    Speed only counts when the output is usable, so this is scored beside the
    latency rather than reported apart from it.
    """
    missing: list[str] = []
    reason = ""
    arguments: dict[str, Any] = {}

    calls = _extract_tool_calls(payload)
    if not calls:
        return {
            "valid": False,
            "reason": "no_tool_call",
            "missing_fields": list(_REQUIRED_TOOL_FIELDS),
            "arguments": {},
        }

    call = calls[0]
    name = str(((call or {}).get("function") or {}).get("name") or "")
    if name != _TOOL_NAME:
        return {
            "valid": False,
            "reason": f"wrong_tool:{name or 'unnamed'}",
            "missing_fields": list(_REQUIRED_TOOL_FIELDS),
            "arguments": {},
        }

    raw = ((call or {}).get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    else:
        try:
            parsed = json.loads(raw or "{}")
            arguments = parsed if isinstance(parsed, dict) else {}
            if not isinstance(parsed, dict):
                reason = "arguments_not_an_object"
        except (TypeError, ValueError):
            return {
                "valid": False,
                "reason": "unparseable_arguments",
                "missing_fields": list(_REQUIRED_TOOL_FIELDS),
                "arguments": {},
            }

    for field_name in _REQUIRED_TOOL_FIELDS:
        value = arguments.get(field_name)
        # Present-but-empty is missing. A model that emits scope_path: "" has
        # produced a call the real tool would reject, and crediting it would
        # make the fast-and-useless case look like the fast case.
        if value is None or (isinstance(value, (str, dict, list)) and not value):
            missing.append(field_name)

    if missing and not reason:
        reason = "missing_required_fields"
    return {
        "valid": not missing and not reason,
        "reason": reason,
        "missing_fields": missing,
        "arguments": arguments,
    }


def _extract_tool_calls(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    message = (choices[0] or {}).get("message")
    if not isinstance(message, dict):
        return []
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


@dataclass
class TurnResult:
    """One model turn: how long, how fast, and whether it was usable."""

    model: str
    case_id: str
    rep: int
    cold: bool
    ok: bool
    t_model_ms: Optional[float] = None
    # No ttft field: these turns are unstreamed, so a time-to-first-token would
    # be permanently null while implying it had been measured.
    completion_tokens: Optional[int] = None
    tokens_per_second: Optional[float] = None
    valid_tool_call: bool = False
    invalid_reason: str = ""
    missing_fields: list[str] = field(default_factory=list)
    error: str = ""


def run_turn(
    *,
    model: str,
    case: dict[str, str],
    rep: int,
    cold: bool,
    base_url: str = DEFAULT_SERVER_ROOT,
    api_key: Optional[str] = None,
    timeout: float = 180.0,
    opener: Optional[Callable[..., Any]] = None,
) -> TurnResult:
    """Time exactly one T_model turn against the local server."""
    assert_loopback(base_url)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": case["utterance"]},
            ],
            "tools": [_TOOL_SCHEMA],
            "tool_choice": "auto",
            # Pinned so a rerun measures the machine, not the sampler.
            "temperature": 0.0,
            "stream": False,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )

    send = opener or urllib.request.urlopen
    started = time.perf_counter()
    try:
        with send(request, timeout=timeout) as response:
            raw = response.read()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return TurnResult(
            model=model,
            case_id=case["id"],
            rep=rep,
            cold=cold,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    usage = payload.get("usage") if isinstance(payload, dict) else None
    completion_tokens = None
    if isinstance(usage, dict):
        raw_tokens = usage.get("completion_tokens")
        if isinstance(raw_tokens, (int, float)) and raw_tokens > 0:
            completion_tokens = int(raw_tokens)

    tokens_per_second = None
    if completion_tokens and elapsed_ms > 0:
        tokens_per_second = round(completion_tokens / (elapsed_ms / 1000.0), 2)

    score = score_tool_call(payload)
    return TurnResult(
        model=model,
        case_id=case["id"],
        rep=rep,
        cold=cold,
        ok=True,
        t_model_ms=round(elapsed_ms, 2),
        completion_tokens=completion_tokens,
        tokens_per_second=tokens_per_second,
        valid_tool_call=bool(score["valid"]),
        invalid_reason=str(score["reason"]),
        missing_fields=list(score["missing_fields"]),
    )


def summarize(results: Sequence[TurnResult]) -> dict[str, Any]:
    """Roll turns up per model, keeping cold and warm apart.

    The first turn after a load pays for weights coming off disk. Averaging it
    with the warm ones describes neither the load cost nor the steady state, so
    both are reported and no blended figure is offered.
    """
    by_model: dict[str, list[TurnResult]] = {}
    for result in results:
        by_model.setdefault(result.model, []).append(result)

    models = []
    for model, turns in by_model.items():
        ok = [turn for turn in turns if turn.ok and turn.t_model_ms is not None]
        cold = [turn.t_model_ms for turn in ok if turn.cold]
        warm = [turn.t_model_ms for turn in ok if not turn.cold]
        throughput = [
            turn.tokens_per_second for turn in ok if turn.tokens_per_second is not None
        ]
        valid = [turn for turn in ok if turn.valid_tool_call]
        models.append(
            {
                "model": model,
                "turns": len(turns),
                "errors": sum(1 for turn in turns if not turn.ok),
                # Validity is reported over turns that actually answered, and
                # the error count sits beside it so a model that mostly failed
                # to respond cannot post a flattering rate on its survivors.
                "valid_tool_call_rate": (
                    round(len(valid) / len(ok), 4) if ok else None
                ),
                "invalid_reasons": sorted(
                    {turn.invalid_reason for turn in ok if turn.invalid_reason}
                ),
                "t_model_cold": _latency_block(cold),
                "t_model_warm": _latency_block(warm),
                "tokens_per_second_p50": pct(throughput, 0.50),
            }
        )

    models.sort(key=lambda entry: entry["model"])
    return {
        "unit": "T_model",
        "note": (
            "T_model only. T_commit (vault, HTTP, DB) is timed separately and "
            "is never summed into this number."
        ),
        "models": models,
    }


def _latency_block(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "count": len(values),
        "p50_ms": pct(values, 0.50),
        "p95_ms": pct(values, 0.95),
        "max_ms": round(max(values), 2),
    }


def run_ladder(
    *,
    models: Iterable[str] = DEFAULT_MODEL_LADDER,
    corpus: Sequence[dict[str, str]] = DEFAULT_CORPUS,
    reps: int = 3,
    base_url: str = DEFAULT_SERVER_ROOT,
    api_key: Optional[str] = None,
    make_room: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Walk the model ladder, timing every case on each model.

    Records the host it ran on. A latency figure without the machine beside it
    is not reproducible, and this benchmark exists to compare machines.
    """
    assert_loopback(base_url)
    reps = max(1, int(reps))
    announce = on_progress or (lambda _message: None)

    results: list[TurnResult] = []
    resident = {entry.get("identifier") for entry in _safe_loaded_models()}
    for model in models:
        # A model already in memory needs no room made for it, and asking would
        # invite an eviction to fit something that is already there.
        if make_room and model not in resident:
            try:
                capacity = ensure_capacity(
                    need_gb=_estimated_size_gb(model), protect=[model]
                )
                # ensure_capacity reports fit False both for "will not fit" and
                # for "could not read host memory". Only the first is a reason
                # to skip: skipping on an unreadable reading would silently drop
                # the entire ladder on any host whose memory we cannot probe.
                readable = isinstance(capacity.get("available_gb"), (int, float)) and (
                    capacity.get("available_gb") or 0
                ) > 0
                if capacity.get("fit") is False and readable:
                    announce(f"skip {model}: will not fit in available memory")
                    results.append(
                        TurnResult(
                            model=model,
                            case_id="*",
                            rep=0,
                            cold=True,
                            ok=False,
                            error="insufficient_memory",
                        )
                    )
                    continue
            except Exception as exc:  # capacity probing must never end the run
                logger.info("capacity check skipped for %s: %s", model, exc)

        for rep in range(reps):
            for index, case in enumerate(corpus):
                # Cold is the very first turn on this model in this run: the one
                # that pays for the weights coming off disk.
                cold = rep == 0 and index == 0
                announce(f"{model} rep{rep} {case['id']}")
                results.append(
                    run_turn(
                        model=model,
                        case=case,
                        rep=rep,
                        cold=cold,
                        base_url=base_url,
                        api_key=api_key,
                    )
                )

    summary = summarize(results)
    summary["host"] = _host_context()
    summary["reps"] = reps
    summary["case_count"] = len(corpus)
    summary["results"] = [asdict(result) for result in results]
    return summary


def _safe_loaded_models() -> list[dict]:
    try:
        return loaded_models()
    except Exception:
        return []


def _estimated_size_gb(model: str) -> float:
    """Best-effort footprint for a model that is not resident yet.

    LM Studio does not report a size for an unloaded model, so this reads the
    parameter count out of the identifier. It is an estimate and is only used to
    ask for room, never reported as a measurement.
    """
    for token in model.replace("/", "-").split("-"):
        lowered = token.casefold()
        if lowered.endswith("b") and lowered[:-1].replace(".", "", 1).isdigit():
            params = float(lowered[:-1])
            # Roughly 0.6 GB per billion at the 4-bit quants in the ladder.
            return round(params * 0.6, 2)
    return 8.0


def _host_context() -> dict[str, Any]:
    context: dict[str, Any] = {}
    try:
        from hermes_cli.hussh_one_host_metrics import host_hardware

        context.update(host_hardware())
    except Exception:
        pass
    try:
        context["memory"] = host_memory()
    except Exception:
        pass
    try:
        context["resident_models"] = [
            entry.get("identifier") for entry in loaded_models()
        ]
    except Exception:
        pass
    return context
