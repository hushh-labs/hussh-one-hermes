# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Forward a local model's reasoning trace to the owner's agent, bounded.

A reasoning model served by LM Studio (Gemma 4, for example) streams its
reasoning on the OpenAI-compatible ``/v1/chat/completions`` stream as
``delta.reasoning_content`` (older servers) or ``delta.reasoning`` (newer
ones), seconds before the first answer word. The agent accepts that trace as
an ``inference.delta`` frame carrying a ``reasoning`` string and shows it to
the owner only, never as answer content (see the agent's
``hushh_mcp/runtime_providers/puppy_reasoning.py``).

Three rules hold here:

* Nothing is invented. A frame is sent only for reasoning text the local model
  actually streamed; a model that streams none produces no reasoning frame.
* The answer is untouched. Reasoning travels in its own frames and never in the
  ``text`` field, and pending reasoning is flushed before the next answer frame
  so the order on the wire matches the order the model wrote.
* It is bounded. The agent's relay buffers 32 frames per request and drops the
  device link when that fills, so per-token reasoning chunks (about 34 a
  second, measured) are coalesced: while the model reasons before answering,
  at most one frame per ``FLUSH_INTERVAL_SECONDS``; pending reasoning is also
  flushed before each answer frame, so a model that interleaves the two sends
  more often. The whole trace stops at ``MAX_REASONING_CHARS``, and frames are
  large enough that even one flush of the entire budget is at most
  ``MAX_BURST_FRAMES`` frames, far below the agent's 32-frame buffer.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

#: Field names an OpenAI-compatible server uses for a streamed reasoning delta,
#: in the order they are read. Only the first non-empty string is used, so a
#: server that mirrors the same text into both fields is not forwarded twice.
REASONING_FIELDS = ("reasoning_content", "reasoning")
#: Per request. Below the agent's 65,536-character display cap for one turn.
MAX_REASONING_CHARS = 64_000
#: Per frame. Even fully escaped (6 bytes a char) this is under 100 KiB, far
#: below the 1 MiB frame limit, and it keeps one flush of the whole budget to
#: ``MAX_BURST_FRAMES`` frames.
MAX_FRAME_CHARS = 16_384
MAX_BURST_FRAMES = -(-MAX_REASONING_CHARS // MAX_FRAME_CHARS)
FLUSH_INTERVAL_SECONDS = 0.25

Send = Callable[[str], Awaitable[None]]


def reasoning_delta(delta: Any) -> str:
    """The reasoning text one stream delta carries, or empty when it has none."""
    if not isinstance(delta, dict):
        return ""
    for name in REASONING_FIELDS:
        value = delta.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


class ReasoningForwarder:
    """Coalesce one request's reasoning deltas into bounded relay frames."""

    def __init__(
        self,
        send: Send,
        request_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        frame_chars: int = MAX_FRAME_CHARS,
        budget: int = MAX_REASONING_CHARS,
    ) -> None:
        self._send = send
        self._request_id = request_id
        self._clock = clock
        self._flush_interval = flush_interval
        self._frame_chars = max(1, frame_chars)
        self._budget = max(0, budget)
        self._pending: list[str] = []
        self._pending_chars = 0
        self._sent_chars = 0
        self._last_flush = clock()
        self.frames = 0

    async def add(self, text: str) -> None:
        """Queue one delta; send when the interval elapsed or a frame is full."""
        room = self._budget - self._sent_chars - self._pending_chars
        if not text or room <= 0:
            return
        piece = text[:room]
        self._pending.append(piece)
        self._pending_chars += len(piece)
        due = self._clock() - self._last_flush >= self._flush_interval
        if due or self._pending_chars >= self._frame_chars:
            await self.flush()

    async def flush(self) -> None:
        """Send everything pending, in frames no longer than ``frame_chars``."""
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending = []
        self._pending_chars = 0
        for start in range(0, len(text), self._frame_chars):
            await self._send(
                json.dumps({
                    "type": "inference.delta",
                    "requestId": self._request_id,
                    "reasoning": text[start : start + self._frame_chars],
                })
            )
            self.frames += 1
        self._sent_chars += len(text)
        self._last_flush = self._clock()


__all__ = [
    "FLUSH_INTERVAL_SECONDS",
    "MAX_BURST_FRAMES",
    "MAX_FRAME_CHARS",
    "MAX_REASONING_CHARS",
    "REASONING_FIELDS",
    "ReasoningForwarder",
    "reasoning_delta",
]
