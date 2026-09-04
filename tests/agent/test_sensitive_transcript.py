# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from types import SimpleNamespace

from agent.sensitive_transcript import (
    SENSITIVE_ARGUMENTS_SENTINEL,
    SENSITIVE_CONTENT_SENTINEL,
    current_turn_uses_sensitive_tools,
    is_sensitive_tool_call,
    redact_messages_for_durable_boundary,
)


def _sensitive_turn(canary: str) -> list[dict]:
    return [
        {"role": "user", "content": "Read my private profile."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "pkm-call",
                    "type": "function",
                    "function": {
                        "name": "read_my_pkm",
                        "arguments": json.dumps(
                            {"action": "read", "domain": "identity"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_name": "read_my_pkm",
            "tool_call_id": "pkm-call",
            "content": json.dumps({"value": {"private_canary": canary}}),
        },
        {"role": "assistant", "content": f"Your private value is {canary}."},
    ]


def test_owner_private_turn_is_redacted_without_mutating_live_messages() -> None:
    canary = "PKM_CANARY_MUST_STAY_MEMORY_ONLY"
    live = _sensitive_turn(canary)

    durable = redact_messages_for_durable_boundary(live)

    assert canary in json.dumps(live)
    assert canary not in json.dumps(durable)
    assert (
        durable[1]["tool_calls"][0]["function"]["arguments"]
        == SENSITIVE_ARGUMENTS_SENTINEL
    )
    assert durable[2]["content"] == SENSITIVE_CONTENT_SENTINEL
    assert durable[3]["content"] == SENSITIVE_CONTENT_SENTINEL
    assert current_turn_uses_sensitive_tools(live) is True


def test_consent_lease_consume_is_sensitive_even_through_terminal() -> None:
    args = {
        "command": (
            ".venv/bin/python -m hermes_cli.hussh_consent_lease "
            "consume lease_example"
        )
    }

    assert is_sensitive_tool_call("terminal", args) is True
    assert is_sensitive_tool_call("terminal", {"command": "pwd"}) is False


def test_private_result_is_redacted_when_legacy_tool_call_id_is_missing() -> None:
    canary = "LEGACY_TOOL_RESULT_WITHOUT_ID"
    live = [
        {"role": "user", "content": "Use my approved source."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "name": "terminal",
                    "arguments": (
                        ".venv/bin/python -m hermes_cli.hussh_consent_lease "
                        "consume lease_example"
                    ),
                }
            ],
        },
        {"role": "tool", "tool_name": "terminal", "content": canary},
        {"role": "assistant", "content": f"Approved result: {canary}"},
    ]

    durable = redact_messages_for_durable_boundary(live)

    assert canary not in json.dumps(durable)
    assert durable[2]["content"] == SENSITIVE_CONTENT_SENTINEL


def test_later_ordinary_turn_is_not_classified_as_sensitive() -> None:
    messages = _sensitive_turn("old-canary") + [
        {"role": "user", "content": "What time is it?"},
        {"role": "assistant", "content": "Noon."},
    ]

    assert current_turn_uses_sensitive_tools(messages) is False


def test_background_review_receives_only_durable_projection(monkeypatch) -> None:
    from agent import background_review

    canary = "PKM_BACKGROUND_CANARY"
    captured: dict = {}

    def fake_run(_agent, messages, _prompt, **_kwargs):
        captured["messages"] = messages

    monkeypatch.setattr(background_review, "_run_review_in_thread", fake_run)
    target, _prompt = background_review.spawn_background_review_thread(
        SimpleNamespace(),
        _sensitive_turn(canary),
        review_memory=True,
        task_cfg={},
    )

    target()
    assert canary not in json.dumps(captured["messages"])
    assert SENSITIVE_CONTENT_SENTINEL in json.dumps(captured["messages"])
