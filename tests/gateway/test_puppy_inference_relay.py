"""Contract tests for Puppy One's inference-only relay adapter."""

from __future__ import annotations

import pytest

from gateway.puppy_inference_relay import PuppyInferenceRelay, _messages, _tools


def test_messages_preserve_tool_call_and_result_identity():
    messages = _messages({
        "systemInstruction": "Be precise",
        "messages": [
            {"role": "user", "text": "Find it"},
            {
                "role": "assistant",
                "text": "",
                "toolName": "lookup",
                "toolCallId": "call-7",
                "toolArguments": {"q": "it"},
            },
            {
                "role": "tool",
                "toolCallId": "call-7",
                "toolResult": {"value": 3},
            },
        ],
    })

    assert messages[0] == {"role": "system", "content": "Be precise"}
    assert messages[2]["tool_calls"][0]["id"] == "call-7"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call-7",
        "content": '{"value":3}',
    }


def test_tools_emit_openai_function_contract():
    assert _tools({
        "tools": [
            {"name": "lookup", "description": "Find", "parameters": {"type": "object"}}
        ]
    }) == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_relay_rejects_non_loopback_model_endpoint():
    with pytest.raises(ValueError, match="loopback"):
        PuppyInferenceRelay(
            relay_url="wss://dev.example/relay",
            token="grant",
            device_id="device-1",
            model_url="https://model.example/v1",
        )


# --------------------------------------------------------------------------- #
# The device says what it is and what it can do, maps every knob the pod set, and
# refuses a capability it lacks BEFORE the local model is called.
# --------------------------------------------------------------------------- #

import json  # noqa: E402

import gateway.puppy_inference_relay as relay_mod  # noqa: E402

_ENDPOINT = "http://127.0.0.1:1234/v1"
_LOCAL_KEY = "local-model-secret-key"


def _relay(**over):
    args = dict(
        relay_url="wss://dev.example/relay",
        token="relay-grant-token",
        device_id="device-1",
        model_url=_ENDPOINT,
        model="qwen3-30b-a3b-mlx",
        model_api_key=_LOCAL_KEY,
    )
    args.update(over)
    return PuppyInferenceRelay(**args)


class _Socket:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def _never_call_the_model(monkeypatch):
    class _Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "the local model was called before the capability check"
            )

    monkeypatch.setattr(relay_mod.httpx, "AsyncClient", _Forbidden)


def test_hello_carries_the_model_and_the_capability_profile_and_never_the_endpoint_or_key():
    hello = _relay().hello()
    assert hello["type"] == "relay.hello" and hello["role"] == "device"
    assert hello["model"] == "qwen3-30b-a3b-mlx"
    assert hello["capabilities"] == {
        "tool_calling": True,
        "json_schema": True,
        "streaming": True,
    }
    assert hello["probe_mode"].startswith(
        "puppy-inference-relay/openai_chat_completions/"
    )
    rendered = json.dumps(hello)
    assert _ENDPOINT not in rendered and "127.0.0.1" not in rendered
    assert _LOCAL_KEY not in rendered and "relay-grant-token" not in rendered


def test_a_declared_profile_overrides_the_default_only_for_known_boolean_names():
    relay = _relay(
        capabilities={"json_schema": False, "shell": True, "tool_calling": "yes"}
    )
    assert relay.capabilities == {
        "tool_calling": True,
        "json_schema": False,
        "streaming": True,
    }


def test_an_endpoint_shaped_model_id_is_not_reported():
    assert _relay(model="http://127.0.0.1:1234/v1").hello()["model"] == ""
    assert _relay(model="/Users/x/models/local").hello()["model"] == ""
    assert _relay(model="two words").hello()["model"] == ""


def test_the_payload_maps_schema_tool_choice_sampling_and_seed():
    payload = _relay()._payload({
        "requestId": "r1",
        "messages": [{"role": "user", "text": "hi"}],
        "tools": [
            {"name": "lookup", "description": "Find", "parameters": {"type": "object"}},
            {"name": "other", "description": "Other", "parameters": {"type": "object"}},
        ],
        "toolChoice": "any",
        "allowedFunctionNames": ["lookup"],
        "responseFormat": {"type": "json_schema", "jsonSchema": {"type": "object"}},
        "temperature": 0.2,
        "maxOutputTokens": 64,
        "topP": 0.9,
        "stopSequences": ["END"],
        "seed": 7,
    })
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": {"type": "object"}},
    }
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "lookup"},
    }
    assert [tool["function"]["name"] for tool in payload["tools"]] == ["lookup"]
    assert payload["top_p"] == 0.9
    assert payload["stop"] == ["END"]
    assert payload["seed"] == 7
    assert payload["temperature"] == 0.2 and payload["max_tokens"] == 64
    assert payload["stream"] is True


def test_tool_choice_words_map_to_the_openai_vocabulary():
    tools = [
        {"name": "a", "description": "", "parameters": {}},
        {"name": "b", "description": "", "parameters": {}},
    ]
    relay = _relay()
    assert (
        relay._payload({"tools": tools, "toolChoice": "auto"})["tool_choice"] == "auto"
    )
    assert (
        relay._payload({"tools": tools, "toolChoice": "any"})["tool_choice"]
        == "required"
    )
    assert (
        relay._payload({"tools": tools, "toolChoice": "none"})["tool_choice"] == "none"
    )
    several = relay._payload({
        "tools": tools,
        "toolChoice": "any",
        "allowedFunctionNames": ["a", "b"],
    })
    assert several["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in several["tools"]] == ["a", "b"]
    assert relay._payload({"responseFormat": {"type": "json"}})["response_format"] == {
        "type": "json_object"
    }


def test_a_plain_request_sets_no_knobs_negative_control():
    payload = _relay()._payload({"messages": [{"role": "user", "text": "hi"}]})
    for key in ("tools", "tool_choice", "response_format", "top_p", "stop", "seed"):
        assert key not in payload


@pytest.mark.asyncio
async def test_a_missing_json_schema_capability_is_refused_before_the_model_is_called(
    monkeypatch,
):
    _never_call_the_model(monkeypatch)
    socket = _Socket()
    await _relay(capabilities={"json_schema": False})._infer(
        {
            "requestId": "r1",
            "messages": [{"role": "user", "text": "hi"}],
            "responseFormat": {"type": "json_schema", "jsonSchema": {"type": "object"}},
        },
        socket,
    )
    assert socket.frames == [
        {
            "type": "inference.error",
            "requestId": "r1",
            "code": "UNSUPPORTED_CAPABILITY",
            "capability": "json_schema",
        }
    ]


@pytest.mark.asyncio
async def test_a_missing_tool_calling_capability_is_refused_before_the_model_is_called(
    monkeypatch,
):
    _never_call_the_model(monkeypatch)
    socket = _Socket()
    await _relay(capabilities={"tool_calling": False})._infer(
        {
            "requestId": "r2",
            "messages": [{"role": "user", "text": "hi"}],
            "tools": [{"name": "lookup", "description": "", "parameters": {}}],
        },
        socket,
    )
    assert socket.frames[0]["code"] == "UNSUPPORTED_CAPABILITY"
    assert socket.frames[0]["capability"] == "tool_calling"
    assert len(socket.frames) == 1


class _Stream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Client:
    """A fake httpx.AsyncClient that serves a scripted SSE completion."""

    lines: list[str] = []
    seen: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        _Client.seen.append({"url": url, "json": json})
        return _Stream(_Client.lines)


@pytest.mark.asyncio
async def test_the_result_names_the_model_that_answered_and_never_the_endpoint_or_key(
    monkeypatch,
):
    _Client.lines = [
        'data: {"model":"qwen3-30b-a3b-mlx","choices":[{"delta":{"content":"hel"}}]}',
        'data: {"model":"qwen3-30b-a3b-mlx","choices":[{"delta":{"content":"lo"}}]}',
        "data: [DONE]",
    ]
    _Client.seen = []
    monkeypatch.setattr(relay_mod.httpx, "AsyncClient", _Client)
    socket = _Socket()
    await _relay()._infer(
        {"requestId": "r3", "messages": [{"role": "user", "text": "hi"}]}, socket
    )
    kinds = [frame["type"] for frame in socket.frames]
    assert kinds == [
        "inference.delta",
        "inference.delta",
        "inference.result",
        "inference.done",
    ]
    result = socket.frames[2]
    assert result["model"] == "qwen3-30b-a3b-mlx"
    rendered = json.dumps(socket.frames)
    assert _ENDPOINT not in rendered and "127.0.0.1" not in rendered
    assert _LOCAL_KEY not in rendered
    assert _Client.seen[0]["url"] == f"{_ENDPOINT}/chat/completions"


@pytest.mark.asyncio
async def test_a_server_model_that_looks_like_an_endpoint_falls_back_to_the_resident_id(
    monkeypatch,
):
    _Client.lines = [
        'data: {"model":"http://127.0.0.1:1234/v1/models/x","choices":[{"delta":{"content":"ok"}}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(relay_mod.httpx, "AsyncClient", _Client)
    socket = _Socket()
    await _relay()._infer(
        {"requestId": "r4", "messages": [{"role": "user", "text": "hi"}]}, socket
    )
    result = next(
        frame for frame in socket.frames if frame["type"] == "inference.result"
    )
    assert result["model"] == "qwen3-30b-a3b-mlx"
    assert "127.0.0.1" not in json.dumps(socket.frames)


@pytest.mark.asyncio
async def test_a_request_needing_nothing_the_profile_lacks_reaches_the_model_negative_control(
    monkeypatch,
):
    """Refusal is per capability the request needs, never blanket: a plain text
    request on a profile without tool calling must still run."""
    _Client.lines = ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
    monkeypatch.setattr(relay_mod.httpx, "AsyncClient", _Client)
    socket = _Socket()
    await _relay(capabilities={"tool_calling": False, "json_schema": False})._infer(
        {"requestId": "r5", "messages": [{"role": "user", "text": "hi"}]}, socket
    )
    assert [frame["type"] for frame in socket.frames] == [
        "inference.delta",
        "inference.result",
        "inference.done",
    ]
