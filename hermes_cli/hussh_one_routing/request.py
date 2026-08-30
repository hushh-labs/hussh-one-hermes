# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The only place a benchmark request body is built, and it is always bounded.

Measured on this host: a ~600-token prompt to ``gemma-4-26b-a4b-qat`` with no
``max_tokens`` and no ``reasoning_effort`` ran past **900 seconds** and returned
nothing. The identical prompt with ``reasoning_effort: "none"`` and
``max_tokens: 1200`` returned in **7.1 seconds**. Same model, same prompt, 128x.

Reasoning tokens are drawn from the same budget as the answer and no model
metadata declares that a model reasons at all, so an unbounded request is not a
slow request -- it is an open-ended one. At 81 turns per model a rung that
ignores its budget costs twenty hours before anyone finds out.

So bounds are not a default here, they are a precondition. There is no code path
in this module that produces a request without them, which is the only version
of this rule that survives someone adding a caller in six months.

Two silent traps this encodes, both measured on this fleet:

  * ``tool_choice: "required"`` is accepted and ignored -- prose comes back with
    ``finish_reason: "stop"``. Never treat it as a guarantee.
  * Unknown parameters are dropped with HTTP 200. Capability can therefore never
    be probed by "does this error", only by inspecting what came back.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_SERVER_ROOT = "http://127.0.0.1:1234"

# Well below run_turn's 180s default. A rung that ignores its reasoning budget
# should cost minutes, not hours, and the circuit breaker below turns repeated
# timeouts into an abandoned rung rather than a slow one.
DEFAULT_TIMEOUT_S = 120.0

# Three consecutive timeouts is a rung that is not going to finish. Abandoning
# is recorded as INDETERMINATE, never as failure -- the model was never given a
# fair chance to answer, and scoring it zero would publish a harness limit as a
# model result.
CONSECUTIVE_TIMEOUT_LIMIT = 3

VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


class UnboundedRequest(ValueError):
    """Raised when a caller tries to build a request without bounds."""


@dataclass
class Turn:
    """One bounded model call and everything needed to judge it fairly."""

    model: str
    ok: bool
    elapsed_ms: Optional[float] = None
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = ""
    completion_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    # `length` means the budget ran out mid-answer. That is indeterminate, not
    # wrong: the model was interrupted, and counting it against correctness
    # reports a harness under-budget as a model failure.
    truncated: bool = False
    timed_out: bool = False
    error: str = ""

    @property
    def indeterminate(self) -> bool:
        """True when this turn cannot be scored either way."""
        return self.truncated or self.timed_out or not self.ok


def build_body(
    *,
    model: str,
    messages: Sequence[dict],
    max_tokens: int,
    reasoning_effort: str,
    tools: Optional[Sequence[dict]] = None,
    temperature: float = 0.0,
    response_format: Optional[dict] = None,
) -> dict[str, Any]:
    """Assemble a bounded request body, or refuse.

    `max_tokens` and `reasoning_effort` are required positional-by-keyword
    arguments with no defaults on purpose. A default would be a bound that a
    caller can forget is there, and the whole point is that forgetting is what
    cost 900 seconds.
    """
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise UnboundedRequest(
            f"max_tokens must be a positive int, got {max_tokens!r}; an "
            "unbounded generation is how a 7-second call becomes a 900-second one"
        )
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise UnboundedRequest(
            f"reasoning_effort must be one of {', '.join(VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r}"
        )

    body: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "stream": False,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
    }
    if tools:
        body["tools"] = list(tools)
        # Deliberately "auto", never "required": this fleet accepts "required"
        # and ignores it, so relying on it would mean believing a guarantee the
        # server does not honour.
        body["tool_choice"] = "auto"
    if response_format:
        body["response_format"] = response_format
    return body


def complete(
    *,
    model: str,
    messages: Sequence[dict],
    max_tokens: int,
    reasoning_effort: str,
    tools: Optional[Sequence[dict]] = None,
    temperature: float = 0.0,
    response_format: Optional[dict] = None,
    base_url: str = DEFAULT_SERVER_ROOT,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: Optional[Callable[..., Any]] = None,
) -> Turn:
    """Run one bounded turn. Never raises; a failure is a Turn that says so."""
    from hermes_cli.hussh_one_pkm.benchmark import assert_loopback

    # A result measured off this machine is a different claim wearing this
    # one's name.
    assert_loopback(base_url)

    body = build_body(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        tools=tools,
        temperature=temperature,
        response_format=response_format,
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    send = opener or urllib.request.urlopen

    started = time.perf_counter()
    try:
        with send(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, urllib.error.URLError) as exc:
        # A socket timeout arrives as URLError wrapping TimeoutError, so both
        # spellings have to be treated as the same event.
        inner = getattr(exc, "reason", None)
        timed_out = isinstance(exc, TimeoutError) or isinstance(inner, TimeoutError)
        return Turn(
            model=model,
            ok=False,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            timed_out=timed_out,
            error=f"{type(exc).__name__}: {exc}",
        )
    except (OSError, ValueError) as exc:
        return Turn(model=model, ok=False, error=f"{type(exc).__name__}: {exc}")

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    choice = (payload.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    usage = payload.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}

    content = message.get("content") or ""
    # Some models return "\n\n" rather than "" alongside a tool call, so strip
    # before any truthiness check on the text.
    if isinstance(content, str):
        content = content.strip()

    return Turn(
        model=model,
        ok=True,
        elapsed_ms=elapsed_ms,
        content=content,
        tool_calls=[c for c in (message.get("tool_calls") or []) if isinstance(c, dict)],
        finish_reason=str(choice.get("finish_reason") or ""),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=details.get("reasoning_tokens"),
        truncated=str(choice.get("finish_reason") or "") == "length",
    )


class CircuitBreaker:
    """Abandon a rung that keeps timing out instead of waiting it out.

    An abandoned rung is INDETERMINATE. It is not a score of zero: the model
    never got to answer, and publishing that as a result would put a harness
    limit into the routing table.
    """

    def __init__(self, limit: int = CONSECUTIVE_TIMEOUT_LIMIT) -> None:
        self._limit = int(limit)
        self._consecutive = 0
        self.abandoned = False
        self.reason = ""

    def record(self, turn: Turn) -> None:
        if turn.timed_out:
            self._consecutive += 1
            if self._consecutive >= self._limit:
                self.abandoned = True
                self.reason = (
                    f"{self._consecutive} consecutive timeouts; rung abandoned as "
                    "indeterminate rather than scored"
                )
        else:
            # Any real answer clears it: an intermittent slow turn is not a
            # rung that cannot finish.
            self._consecutive = 0
