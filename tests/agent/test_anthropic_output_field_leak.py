"""Regression coverage for replaying Anthropic SDK response blocks.

Reproduces HTTP 400 `messages.N.content.M.text.parsed_output: Extra inputs are
not permitted`. Anthropic SDK response blocks carry output-only attributes
(text blocks: `parsed_output`, `citations=None`; tool_use blocks: `caller`)
that the Messages *input* schema forbids. normalize_response captured blocks
verbatim via _to_plain_data and replayed them as input → 400.

Fix: whitelist input-permitted fields per block type at three points —
normalize_response capture, _sanitize_replay_block (ordered-blocks replay), and
_convert_content_part_to_anthropic (content-list replay).
"""
import pytest

from agent.anthropic_adapter import (
    _convert_assistant_message,
    _convert_content_part_to_anthropic,
    _sanitize_replay_block,
)


def _assert_clean(block):
    assert isinstance(block, dict)
    assert "parsed_output" not in block
    assert "caller" not in block
    if "citations" in block:
        assert isinstance(block["citations"], list) and block["citations"]


class TestSanitizeReplayBlock:

    def test_tool_use_strips_caller(self):
        poisoned = {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                    "input": {"path": "a"}, "caller": {"type": "agent"}}
        out = _sanitize_replay_block(poisoned)
        _assert_clean(out)
        assert out["name"] == "read_file" and out["input"] == {"path": "a"}



    def test_unknown_type_dropped(self):
        assert _sanitize_replay_block({"type": "server_tool_use", "foo": 1}) is None


def test_sanitize_replay_tool_use_strips_caller_and_preserves_input():
    result = _sanitize_replay_block(
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read_file",
            "input": {"path": "a"},
            "caller": {"type": "agent"},
        }
    )
    _assert_clean(result)
    assert result["name"] == "read_file"
    assert result["input"] == {"path": "a"}


def test_content_part_conversion_strips_sdk_only_fields():
    result = _convert_content_part_to_anthropic(
        {"type": "text", "text": "hello", "parsed_output": None, "citations": None}
    )
    _assert_clean(result)


def test_ordered_anthropic_blocks_replay_cleanly_and_in_order():
    result = _convert_assistant_message(
        {
            "role": "assistant",
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "plan", "signature": "s1"},
                {"type": "text", "text": "doing it", "parsed_output": None, "citations": None},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "a"},
                    "caller": {"type": "agent"},
                },
            ],
        }
    )
    blocks = result["content"]
    assert [block["type"] for block in blocks] == ["thinking", "text", "tool_use"]
    for block in blocks:
        _assert_clean(block)
    assert blocks[0]["signature"] == "s1"
