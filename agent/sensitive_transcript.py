# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Durable-transcript boundary for owner-private tool calls.

Live tool messages stay intact for the active model turn. Durable and secondary
consumers receive a redacted projection so decrypted PKM/source information is
not copied out of its authorized in-memory boundary.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Iterable


SENSITIVE_CONTENT_SENTINEL = (
    "[Owner-private tool content omitted from durable history. "
    "Re-authorize a narrow read to use it again.]"
)
SENSITIVE_ARGUMENTS_SENTINEL = json.dumps(
    {"redacted": "owner_private_memory_only"},
    separators=(",", ":"),
)

_SENSITIVE_TOOL_NAMES = frozenset(
    {
        "read_my_pkm",
        "save_to_pkm",
        "ask_source_library_steward",
        "ask_file_steward",
    }
)


def _argument_text(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments.lower()
    try:
        return json.dumps(arguments, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(arguments).lower()


def is_sensitive_tool_call(name: Any, arguments: Any = None) -> bool:
    """Return whether a tool call can carry owner-private plaintext."""
    normalized = str(name or "").strip()
    if normalized in _SENSITIVE_TOOL_NAMES or normalized.startswith(
        "hussh_one_source_"
    ):
        return True
    if normalized not in {"terminal", "execute_code"}:
        return False
    text = _argument_text(arguments)
    return "hussh_consent_lease" in text and "consume" in text


def is_sensitive_tool_name(name: Any) -> bool:
    """Name-only check for result rows whose originating arguments are absent."""
    normalized = str(name or "").strip()
    return normalized in _SENSITIVE_TOOL_NAMES or normalized.startswith(
        "hussh_one_source_"
    )


def _tool_call_parts(tool_call: Any) -> tuple[str, Any, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            return (
                str(function.get("name") or ""),
                function.get("arguments"),
                tool_call.get("id"),
            )
        return (
            str(tool_call.get("name") or ""),
            tool_call.get("arguments"),
            tool_call.get("id") or tool_call.get("tool_call_id"),
        )
    function = getattr(tool_call, "function", None)
    return (
        str(getattr(function, "name", "") or getattr(tool_call, "name", "")),
        getattr(function, "arguments", None)
        if function is not None
        else getattr(tool_call, "arguments", None),
        getattr(tool_call, "id", None),
    )


def _redact_tool_call(tool_call: Any) -> Any:
    name, arguments, _tool_call_id = _tool_call_parts(tool_call)
    if not is_sensitive_tool_call(name, arguments):
        return copy.deepcopy(tool_call)
    if isinstance(tool_call, dict):
        redacted = copy.deepcopy(tool_call)
        function = redacted.get("function")
        if isinstance(function, dict):
            function["arguments"] = SENSITIVE_ARGUMENTS_SENTINEL
        else:
            redacted["arguments"] = SENSITIVE_ARGUMENTS_SENTINEL
        return redacted
    if hasattr(tool_call, "model_dump"):
        return _redact_tool_call(tool_call.model_dump())
    return {
        "id": _tool_call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": SENSITIVE_ARGUMENTS_SENTINEL,
        },
    }


def _sensitive_layout(
    messages: Iterable[Any],
) -> tuple[set[Any], set[int], set[int]]:
    sensitive_ids: set[Any] = set()
    redact_assistant_indices: set[int] = set()
    redact_tool_indices: set[int] = set()
    sensitive_turn = False
    first_sensitive_index: int | None = None
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            continue
        if raw.get("role") == "user":
            sensitive_turn = False
            first_sensitive_index = None
        calls = raw.get("tool_calls") or []
        for call in calls if isinstance(calls, list) else []:
            name, arguments, tool_call_id = _tool_call_parts(call)
            if is_sensitive_tool_call(name, arguments):
                sensitive_turn = True
                first_sensitive_index = index
                if tool_call_id:
                    sensitive_ids.add(tool_call_id)
        if (
            sensitive_turn
            and first_sensitive_index is not None
            and index >= first_sensitive_index
        ):
            if raw.get("role") == "assistant":
                redact_assistant_indices.add(index)
            elif raw.get("role") == "tool":
                # Historical serializers did not always retain tool-call IDs.
                # Once a private call begins, every tool result before the next
                # user turn is part of that private execution boundary.
                redact_tool_indices.add(index)
    return sensitive_ids, redact_assistant_indices, redact_tool_indices


def redact_messages_for_durable_boundary(messages: Iterable[Any]) -> list[Any]:
    """Return a copy safe for SQLite, logs, exports, review, and memory sinks."""
    source = list(messages or [])
    sensitive_ids, redact_assistant_indices, redact_tool_indices = (
        _sensitive_layout(source)
    )
    output: list[Any] = []
    for index, raw in enumerate(source):
        if not isinstance(raw, dict):
            output.append(copy.deepcopy(raw))
            continue
        message = copy.deepcopy(raw)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            message["tool_calls"] = [_redact_tool_call(call) for call in calls]
        role = message.get("role")
        result_is_sensitive = (
            role == "tool"
            and (
                message.get("_sensitive_transcript")
                or index in redact_tool_indices
                or message.get("tool_call_id") in sensitive_ids
                or is_sensitive_tool_name(message.get("tool_name"))
            )
        )
        assistant_is_sensitive = role == "assistant" and index in redact_assistant_indices
        if result_is_sensitive or assistant_is_sensitive:
            if message.get("content") is not None:
                message["content"] = SENSITIVE_CONTENT_SENTINEL
            message.pop("api_content", None)
            for field in (
                "reasoning",
                "reasoning_content",
                "reasoning_details",
                "codex_reasoning_items",
                "codex_message_items",
            ):
                message.pop(field, None)
        message.pop("_sensitive_transcript", None)
        output.append(message)
    return output


def current_turn_uses_sensitive_tools(messages: Iterable[Any]) -> bool:
    """Return whether the most recent user turn invoked an owner-private tool."""
    current: list[Any] = []
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "user":
            current = [message]
        else:
            current.append(message)
    sensitive_ids, sensitive_assistants, sensitive_tools = _sensitive_layout(current)
    return bool(sensitive_ids or sensitive_assistants or sensitive_tools)
