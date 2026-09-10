"""Contract tests for Puppy One's inference-only relay adapter."""

from __future__ import annotations

import pytest

from gateway.puppy_inference_relay import PuppyInferenceRelay, _messages, _tools


def test_messages_preserve_tool_call_and_result_identity():
    messages = _messages(
        {
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
        }
    )

    assert messages[0] == {"role": "system", "content": "Be precise"}
    assert messages[2]["tool_calls"][0]["id"] == "call-7"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call-7",
        "content": '{"value":3}',
    }


def test_tools_emit_openai_function_contract():
    assert _tools(
        {"tools": [{"name": "lookup", "description": "Find", "parameters": {"type": "object"}}]}
    ) == [
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
