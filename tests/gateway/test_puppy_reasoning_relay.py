# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The relay forwards a local model's streamed reasoning, bounded and never invented.

Measured 2026-10-05: LM Studio streamed 340 reasoning chunks for
google/gemma-4-12b from 0.6 s, and the first answer word at 10.4 s. The relay
dropped every one, so the owner's agent could never show a thinking trail.
"""

from __future__ import annotations

import base64
import functools
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import gateway.puppy_inference_relay as relay_mod
from gateway.puppy_direct_pod import DeviceEnvelope, _nonce, _SealedSender
from gateway.puppy_inference_relay import PuppyInferenceRelay
from gateway.puppy_reasoning_relay import (
    FLUSH_INTERVAL_SECONDS,
    MAX_BURST_FRAMES,
    MAX_FRAME_CHARS,
    MAX_REASONING_CHARS,
    ReasoningForwarder,
    reasoning_delta,
)

_ENDPOINT = "http://127.0.0.1:1234/v1"
_REQUEST = {"requestId": "r1", "messages": [{"role": "user", "text": "17 x 23?"}]}


def _line(delta: dict) -> str:
    return "data: " + json.dumps({
        "model": "google/gemma-4-12b",
        "choices": [{"delta": delta}],
    })


class _Stream:
    def __init__(self, lines, tick):
        self._lines = lines
        self._tick = tick

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            self._tick()
            yield line


def _serve(monkeypatch, lines, tick=lambda: None):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _Stream([*lines, "data: [DONE]"], tick)

    monkeypatch.setattr(relay_mod.httpx, "AsyncClient", _Client)


class _Socket:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def _relay():
    return PuppyInferenceRelay(
        relay_url="wss://dev.example/relay",
        token="relay-grant-token",
        device_id="device-1",
        model_url=_ENDPOINT,
        model="google/gemma-4-12b",
    )


async def _run(monkeypatch, lines):
    _serve(monkeypatch, lines)
    socket = _Socket()
    await _relay()._infer(dict(_REQUEST), socket)
    return socket.frames


def _reasoning(frames):
    return "".join(frame["reasoning"] for frame in frames if "reasoning" in frame)


def _answer(frames):
    return [frame["text"] for frame in frames if "text" in frame]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
async def test_streamed_reasoning_reaches_the_agent_before_the_answer(
    monkeypatch, field
):
    frames = await _run(
        monkeypatch,
        [
            _line({field: "The user asks "}),
            _line({field: "for 17 times 23."}),
            _line({"content": "It is "}),
            _line({"content": "391."}),
        ],
    )
    assert _reasoning(frames) == "The user asks for 17 times 23."
    assert _answer(frames) == ["It is ", "391."]
    first_answer = next(i for i, f in enumerate(frames) if "text" in f)
    assert all(i < first_answer for i, f in enumerate(frames) if "reasoning" in f)
    for frame in frames:
        if "reasoning" in frame:
            assert frame["type"] == "inference.delta" and frame["requestId"] == "r1"
            assert "text" not in frame
    assert [f["type"] for f in frames][-2:] == ["inference.result", "inference.done"]


@pytest.mark.asyncio
async def test_a_model_that_streams_no_reasoning_gets_no_reasoning_frame_negative_control(
    monkeypatch,
):
    frames = await _run(
        monkeypatch, [_line({"content": "hel"}), _line({"content": "lo"})]
    )
    assert [f["type"] for f in frames] == [
        "inference.delta",
        "inference.delta",
        "inference.result",
        "inference.done",
    ]
    assert not any("reasoning" in frame for frame in frames)


@pytest.mark.asyncio
async def test_non_text_or_empty_reasoning_fields_are_never_turned_into_a_trace(
    monkeypatch,
):
    frames = await _run(
        monkeypatch,
        [
            _line({"reasoning_content": None, "reasoning": {"summary": "x"}}),
            _line({"reasoning": ["not", "text"]}),
            _line({"reasoning_content": "", "content": "ok"}),
        ],
    )
    assert not any("reasoning" in frame for frame in frames)
    assert _answer(frames) == ["ok"]


@pytest.mark.asyncio
async def test_reasoning_and_answer_in_one_chunk_keep_the_answer_exact(monkeypatch):
    frames = await _run(
        monkeypatch,
        [
            _line({"reasoning_content": "Thinking.", "content": "Answer."}),
        ],
    )
    assert [("reasoning" in f, "text" in f) for f in frames[:2]] == [
        (True, False),
        (False, True),
    ]
    assert _answer(frames) == ["Answer."]


@pytest.mark.asyncio
async def test_a_mirrored_field_is_forwarded_once():
    assert reasoning_delta({"reasoning_content": "a", "reasoning": "a"}) == "a"
    assert reasoning_delta({"reasoning": "b"}) == "b"
    assert reasoning_delta("not a delta") == ""


@pytest.mark.asyncio
async def test_the_measured_340_chunks_coalesce_without_losing_a_char(monkeypatch):
    """The measured stream: 340 reasoning chunks between 0.6 s and 10.4 s.

    The agent's relay buffers 32 frames per request and drops the device link
    when that fills, so the frame rate is what matters, not only the total.
    """
    now = [0.0]
    step = (10.4 - 0.6) / 340
    monkeypatch.setattr(
        relay_mod,
        "ReasoningForwarder",
        functools.partial(ReasoningForwarder, clock=lambda: now[0]),
    )
    pieces = [f"t{i} " for i in range(340)]
    lines = [_line({"reasoning_content": p}) for p in pieces]
    _serve(
        monkeypatch,
        [*lines, _line({"content": "391."})],
        tick=lambda: now.__setitem__(0, now[0] + step),
    )
    socket = _Socket()
    await _relay()._infer(dict(_REQUEST), socket)
    reasoning_frames = [f for f in socket.frames if "reasoning" in f]
    assert _reasoning(socket.frames) == "".join(pieces)
    assert len(reasoning_frames) <= (10.4 - 0.6) / FLUSH_INTERVAL_SECONDS + 2
    assert len(reasoning_frames) * 8 <= 340
    assert _answer(socket.frames) == ["391."]


@pytest.mark.asyncio
async def test_coalescing_flushes_on_the_interval_and_on_a_full_frame():
    now = [0.0]
    socket = _Socket()
    forwarder = ReasoningForwarder(
        socket.send, "r1", clock=lambda: now[0], flush_interval=0.15, frame_chars=10
    )
    await forwarder.add("ab")
    assert socket.frames == []
    now[0] = 0.2
    await forwarder.add("cd")
    assert [f["reasoning"] for f in socket.frames] == ["abcd"]
    await forwarder.add("0123456789xy")
    assert [f["reasoning"] for f in socket.frames][1:] == ["0123456789", "xy"]
    await forwarder.flush()
    assert forwarder.frames == 3


@pytest.mark.asyncio
async def test_one_huge_delta_never_bursts_near_the_agents_32_frame_buffer():
    """A single 70,000-character delta used to leave as 32 back-to-back frames,
    exactly the agent's per-request buffer, which closes the device link."""
    socket = _Socket()
    forwarder = ReasoningForwarder(socket.send, "r1", clock=lambda: 0.0)
    await forwarder.add("r" * 70_000)
    await forwarder.flush()
    assert forwarder.frames == MAX_BURST_FRAMES <= 4
    assert len(_reasoning(socket.frames)) == MAX_REASONING_CHARS


@pytest.mark.asyncio
async def test_the_trace_is_capped_and_every_frame_is_bounded_while_the_answer_is_whole(
    monkeypatch,
):
    big = "r" * 5_000
    lines = [_line({"reasoning_content": big}) for _ in range(20)]
    answer = ["a" * 3_000, "b" * 3_000]
    frames = await _run(monkeypatch, [*lines, *[_line({"content": c}) for c in answer]])
    assert len(_reasoning(frames)) == MAX_REASONING_CHARS
    assert all(
        len(f["reasoning"]) <= MAX_FRAME_CHARS for f in frames if "reasoning" in f
    )
    assert _answer(frames) == answer


def _keypair():
    key = X25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, base64.b64encode(public).decode()


@pytest.mark.asyncio
async def test_the_direct_pod_path_seals_reasoning_frames_the_agent_can_open(
    monkeypatch,
):
    """The direct BYOC relay reuses ``_infer`` behind a sealed sender."""
    device_key, device_public = _keypair()
    pod_key, pod_public = _keypair()
    binding = dict(hushh_id="owner-1", device_id="device-1", session_id="s1", epoch=1)
    device = DeviceEnvelope(device_key, pod_public, **binding)
    pod_view = DeviceEnvelope(pod_key, device_public, **binding)
    socket = _Socket()
    _serve(monkeypatch, [_line({"reasoning": "Hmm."}), _line({"content": "Yes."})])
    await _relay()._infer(dict(_REQUEST), _SealedSender(socket, device))
    opened = [
        json.loads(
            pod_view._aead.decrypt(
                _nonce("d2p", wire["seq"]),
                base64.b64decode(wire["ciphertext"]),
                pod_view._aad("d2p", wire["seq"], wire["innerType"]),
            )
        )
        for wire in socket.frames
    ]
    assert opened[0] == {
        "type": "inference.delta",
        "requestId": "r1",
        "reasoning": "Hmm.",
    }
    assert opened[1] == {"type": "inference.delta", "requestId": "r1", "text": "Yes."}
