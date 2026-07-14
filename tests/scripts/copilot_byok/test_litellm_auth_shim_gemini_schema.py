"""Tests for the Gemini tool-schema sanitizer in the Copilot BYOK auth shim.

Regression coverage for a real production bug: Vertex's Gemini function-
calling validator hard-400s on any tool `parameters` schema carrying a
ROOT-LEVEL `anyOf` / `oneOf` / `allOf` (e.g. "provide `scope` OR
`request_id`") — even though Claude, on the same Vertex proxy, accepts the
identical shape natively. Real MCP servers emit this pattern routinely (the
hushh-consent MCP server's `check_consent_status` and `request_consent`
tools both do), so without the shim-side fix every Gemini model in the BYOK
lineup would 400 on any Copilot turn that includes one of these tools in its
`tools` array — even if the model never ends up calling it, because Vertex
validates the whole manifest up front.

See `scripts/copilot-byok/litellm_auth_shim.py`:
`_scrub_tools_for_gemini` / `_sanitize_gemini_tool_schema`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SHIM_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "copilot-byok"
    / "litellm_auth_shim.py"
)


@pytest.fixture(scope="module")
def shim():
    """Import the shim module directly from its script path (it lives
    outside any package — this mirrors how it's deployed/run standalone)."""
    os.environ.setdefault("LITELLM_MASTER_KEY", "test-key-for-import-only")
    spec = importlib.util.spec_from_file_location(
        "litellm_auth_shim_under_test", _SHIM_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# The real hushh-consent check_consent_status tool shape (root-level anyOf,
# "provide scope OR request_id").
ROOT_ANYOF_TOOL = {
    "type": "function",
    "function": {
        "name": "check_consent_status",
        "description": "Check consent status for a user/scope pair or a specific request id.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "scope": {"type": "string"},
                "request_id": {"type": "string"},
            },
            "required": ["user_id"],
            "anyOf": [{"required": ["scope"]}, {"required": ["request_id"]}],
        },
    },
}


class TestIsGeminiModel:
    def test_matches_gemini_family(self, shim):
        assert shim._is_gemini_model("gemini-3.5-flash")
        assert shim._is_gemini_model("gemini-3.1-pro-preview")
        assert shim._is_gemini_model("GEMINI-3-PRO")  # case-insensitive

    def test_does_not_match_claude(self, shim):
        assert not shim._is_gemini_model("claude-opus-4-8")
        assert not shim._is_gemini_model("claude-sonnet-4-6")

    def test_handles_non_string_input(self, shim):
        assert not shim._is_gemini_model(None)
        assert not shim._is_gemini_model(123)


class TestSanitizeGeminiToolSchema:
    def test_strips_root_level_anyof(self, shim):
        params = ROOT_ANYOF_TOOL["function"]["parameters"]
        out = shim._sanitize_gemini_tool_schema(params)
        assert "anyOf" not in out
        # Original untouched (pure function, no in-place mutation).
        assert "anyOf" in params

    def test_folds_constraint_into_description(self, shim):
        params = ROOT_ANYOF_TOOL["function"]["parameters"]
        out = shim._sanitize_gemini_tool_schema(params)
        assert "scope" in out["description"]
        assert "request_id" in out["description"]

    def test_preserves_properties_and_required(self, shim):
        params = ROOT_ANYOF_TOOL["function"]["parameters"]
        out = shim._sanitize_gemini_tool_schema(params)
        assert out["type"] == "object"
        assert out["properties"] == params["properties"]
        assert out["required"] == ["user_id"]

    def test_noop_when_no_composition_keyword(self, shim):
        params = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
        out = shim._sanitize_gemini_tool_schema(params)
        assert out == params

    def test_strips_oneof_and_allof_too(self, shim):
        for key in ("oneOf", "allOf"):
            params = {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                key: [{"required": ["a"]}, {"required": ["b"]}],
            }
            out = shim._sanitize_gemini_tool_schema(params)
            assert key not in out
            assert out["description"]  # a note was added

    def test_does_not_touch_nested_property_level_anyof(self, shim):
        # Property-level anyOf (a value can be one of several primitive
        # types) is a DIFFERENT, Vertex-safe shape — must be left alone.
        params = {
            "type": "object",
            "properties": {
                "location": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                    "description": "City or postal code",
                }
            },
            "required": ["location"],
        }
        out = shim._sanitize_gemini_tool_schema(params)
        # No ROOT-level anyOf existed, so nothing should change.
        assert out == params
        assert "anyOf" in out["properties"]["location"]


class TestScrubToolsForGemini:
    def _body(self, model: str, tools: list[dict]) -> bytes:
        return json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": tools,
            }
        ).encode()

    def test_sanitizes_gemini_request_with_root_anyof(self, shim):
        body = self._body("gemini-3.5-flash", [ROOT_ANYOF_TOOL])
        out = shim._scrub_tools_for_gemini(body, "application/json")
        data = json.loads(out)
        params = data["tools"][0]["function"]["parameters"]
        assert "anyOf" not in params

    def test_leaves_claude_request_untouched(self, shim):
        # Claude accepts root-level anyOf natively — the shim must not
        # reshape (or even re-serialize) Claude-bound bodies.
        body = self._body("claude-opus-4-8", [ROOT_ANYOF_TOOL])
        out = shim._scrub_tools_for_gemini(body, "application/json")
        assert out == body

    def test_leaves_gemini_request_without_tools_untouched(self, shim):
        body = json.dumps(
            {"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        out = shim._scrub_tools_for_gemini(body, "application/json")
        assert out == body

    def test_leaves_gemini_request_with_clean_tools_untouched(self, shim):
        clean_tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
        body = self._body("gemini-3.1-pro-preview", [clean_tool])
        out = shim._scrub_tools_for_gemini(body, "application/json")
        assert out == body

    def test_fails_open_on_non_json_content_type(self, shim):
        body = b"not json at all"
        out = shim._scrub_tools_for_gemini(body, "text/plain")
        assert out == body

    def test_fails_open_on_malformed_json(self, shim):
        body = b'{"model": "gemini-3.5-flash", "tools": [BROKEN'
        out = shim._scrub_tools_for_gemini(body, "application/json")
        assert out == body

    def test_sanitized_output_is_valid_json_and_openai_shaped(self, shim):
        body = self._body("gemini-3.5-flash", [ROOT_ANYOF_TOOL])
        out = shim._scrub_tools_for_gemini(body, "application/json")
        data = json.loads(out)  # must not raise
        assert data["model"] == "gemini-3.5-flash"
        assert data["tools"][0]["function"]["name"] == "check_consent_status"

    def test_multiple_tools_only_sanitizes_the_offending_one(self, shim):
        clean_tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
        body = self._body("gemini-3.5-flash", [clean_tool, ROOT_ANYOF_TOOL])
        out = shim._scrub_tools_for_gemini(body, "application/json")
        data = json.loads(out)
        assert data["tools"][0] == clean_tool  # untouched
        assert "anyOf" not in data["tools"][1]["function"]["parameters"]
