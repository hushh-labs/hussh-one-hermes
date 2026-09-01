# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Recovering exam cases from real session dumps.

Two of these classes exist because the first implementation got it wrong against
real data and inflated a 3.8% model failure rate into 42%. Both mistakes were
invisible in unit tests and only appeared when the oracles were pointed at the
actual corpus, which is why that validation step comes before any model runs.
"""

from __future__ import annotations

import json

from hermes_cli.hussh_one_routing.exam import build as B


class TestTruncationIsDetectedPerValue:
    def test_a_truncated_content_field_is_caught(self):
        assert B.is_truncated({"content": "import os\n# ...[truncated]"})

    def test_serialising_first_would_have_missed_it(self):
        # The original checked json.dumps(args) against an end-anchored regex.
        # The sentinel then sits mid-string with '"}' after it, so the anchor
        # never matches and every truncated case reaches the scoring set. That
        # single mistake reports a 36% syntax-failure rate instead of 3.8%.
        args = {"content": "import os\n# ...[truncated]", "path": "a.py"}
        assert B.TRUNCATION_SENTINEL.search(json.dumps(args)) is None
        assert B.is_truncated(args) is True

    def test_clean_content_is_not_flagged(self):
        assert not B.is_truncated({"content": "import os\nx = 1\n"})

    def test_the_character_count_form_is_caught(self):
        assert B.is_truncated({"content": "body [... 4,201 characters omitted]"})

    def test_a_sentinel_mid_string_is_not_a_truncation(self):
        # Prose mentioning the marker is not the dumper cutting the value.
        assert not B.is_truncated(
            {"content": "we log '...[truncated]' when compacting\nx = 1\n"}
        )

    def test_nested_values_are_searched(self):
        assert B.is_truncated({"a": {"b": ["x", "y ...[truncated]"]}})


class TestAPaginatedReadIsNotTheFile:
    def test_offset_marks_a_partial_read(self):
        assert B.is_partial_read({"path": "a.py", "offset": 200})

    def test_limit_marks_a_partial_read(self):
        assert B.is_partial_read({"path": "a.py", "limit": 50})

    def test_a_whole_file_read_is_not_partial(self):
        assert not B.is_partial_read({"path": "a.py"})

    def test_offset_zero_still_counts_as_a_window(self):
        # `offset: 0, limit: 50` is still a window, and `if args.get("offset")`
        # would call it a full read.
        assert B.is_partial_read({"path": "a.py", "offset": 0, "limit": 50})


class TestBothWireFormatsAreWalked:
    def test_openai_tool_calls_are_found(self):
        message = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "patch",
                                          "arguments": '{"path": "a.py"}'}}
            ],
        }
        found = list(B.iter_tool_calls(message))
        assert found == [("patch", {"path": "a.py"}, "c1")]

    def test_anthropic_tool_use_blocks_are_found(self):
        # Skipping these drops 3 patch calls, and those 3 are the only .ts/.tsx
        # edits in the whole corpus, so the omission removes exactly the
        # extension class no validator covers.
        message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "editing"},
                {"type": "tool_use", "id": "u1", "name": "patch",
                 "input": {"path": "a.tsx"}},
            ],
        }
        assert list(B.iter_tool_calls(message)) == [
            ("patch", {"path": "a.tsx"}, "u1")
        ]

    def test_unparseable_arguments_are_kept_not_dropped(self):
        message = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "patch", "arguments": "{oops"}}
            ],
        }
        name, args, _ = next(iter(B.iter_tool_calls(message)))
        assert name == "patch"
        assert "__unparseable__" in args

    def test_catalog_schemas_come_from_either_shape(self):
        body = {
            "tools": [
                {"function": {"name": "a", "parameters": {"type": "object"}}},
                {"name": "b", "input_schema": {"type": "object"}},
            ]
        }
        assert set(B.catalog_schemas(body)) == {"a", "b"}


class TestWireSizeCountsWhatIsActuallySent:
    def test_tool_schemas_count_toward_the_prompt(self):
        # Summing only message content omits tool_calls JSON and the tools[]
        # block, both of which are on the wire. That undercounts the real prompt
        # by about 4x at the median on this corpus.
        bare = {"messages": [{"role": "user", "content": "hi"}]}
        with_tools = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"function": {"name": "x", "parameters": {"a": "b" * 400}}}],
        }
        assert B.wire_size(with_tools) > B.wire_size(bare) + 400

    def test_assistant_tool_calls_count(self):
        body = {
            "messages": [
                {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "t",
                                              "arguments": "x" * 500}}]}
            ]
        }
        assert B.wire_size(body) > 500

    def test_the_token_ratio_is_the_measured_one(self):
        # 3.05 chars/token, tokenized with the real Gemma-4 tokenizer on this
        # corpus. The generic 4.0 undercounts by 31% because the content is
        # code, JSON and base64.
        assert B.CHARS_PER_TOKEN == 3.05
        assert B.estimate_tokens("x" * 305) == 100


class TestFingerprintDedupesReplayedHistory:
    def test_the_same_call_fingerprints_identically(self):
        assert B.fingerprint("patch", {"a": 1}) == B.fingerprint("patch", {"a": 1})

    def test_key_order_does_not_change_the_fingerprint(self):
        assert B.fingerprint("patch", {"a": 1, "b": 2}) == B.fingerprint(
            "patch", {"b": 2, "a": 1}
        )

    def test_different_arguments_differ(self):
        assert B.fingerprint("patch", {"a": 1}) != B.fingerprint("patch", {"a": 2})
