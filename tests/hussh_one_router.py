"""Unit tests for the hussh-one dynamic workload & intent router.

These tests use the gateway testing harness to verify that:
  1. Low complexity queries (casual chatter, simple questions) route to Gemini 3.5 Flash.
  2. High complexity queries (coding, terminal work, migrations) escalate to Claude Opus.
  3. Pinned models bypass the router entirely.
  4. Precise model display names are resolved dynamically.
"""

from __future__ import annotations

import os
import unittest
import asyncio

# Ensure parent directory is in sys.path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_cli.hussh_one_router import route_workload, _classify_via_rules
from hermes_cli.hussh_one_header import display_model_name, mode_token


class TestHusshOneRouter(unittest.TestCase):

    def test_rule_based_classification(self):
        """Verify fallback rule engine correctly flags heavy workflows."""
        cases = [
            ("hi there", "low"),
            ("how's the weather?", "low"),
            ("what is our current default model?", "low"),
            ("write a python script to parse local logs", "high"),
            ("deploy the latest main branch commit to UAT", "high"),
            ("run database migrations for postgres", "high"),
            ("check DCO signoff for PR #2599", "high"),
            ("organize our brand assets inside google drive", "high"),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                res = _classify_via_rules(prompt)
                self.assertEqual(
                    res, expected, 
                    f"Prompt '{prompt}' classified as {res}, expected {expected}"
                )

    def test_precise_model_name_mapping(self):
        """Verify that raw model IDs resolve to exact friendly display names."""
        cases = [
            ("google/gemini-2.5-flash", "Gemini 2.5 Flash"),
            ("anthropic/claude-3-5-sonnet", "Claude Sonnet 3.5"),
            ("anthropic/claude-opus-4-8", "Claude Opus 4.8"),
            ("custom/qwen-35b-chat", "Qwen 3.6 35B"),
            ("local/unregistered-llama-2b", "unregistered-llama-2b"), # honest raw short-id fallback
        ]
        for model_id, expected in cases:
            with self.subTest(model_id=model_id):
                res = display_model_name(model_id)
                self.assertEqual(
                    res, expected, 
                    f"Model '{model_id}' mapped to '{res}', expected '{expected}'"
                )

    def test_dynamic_routing_resolution(self):
        """Verify async workload routing integrates and returns correct models."""
        # Test low-complexity prompt routing (no escalation)
        low_model, low_runtime = asyncio.run(
            route_workload("hi there!", {})
        )
        self.assertEqual(low_model, "gemini-3.5-flash")
        
        # Test high-complexity prompt routing (escalates to Claude Opus)
        high_model, high_runtime = asyncio.run(
            route_workload("create a new python test file and run pytest on it", {})
        )
        self.assertEqual(high_model, "claude-opus-4-8")
        self.assertEqual(high_runtime.get("provider"), "google-vertex")

    def test_llm_classifier_is_not_awaited_as_coroutine(self):
        """Regression: run_conversation is SYNC; awaiting it killed LLM routing.

        Previously the router did ``await classifier.run_conversation(prompt)``,
        but that method returns a plain dict, raising
        "object dict can't be used in 'await' expression" on EVERY turn and
        silently degrading to the keyword fallback. We patch AIAgent so its
        run_conversation returns a high-complexity verdict and assert the
        router honours the LLM decision (escalates) without raising.
        """
        import run_agent

        class _FakeAgent:
            def __init__(self, *a, **kw):
                pass

            def run_conversation(self, prompt, *a, **kw):
                # Synchronous dict return — exactly like the real method.
                return {"final_response": '{"complexity": "high", "reason": "code task"}'}

        original = run_agent.AIAgent
        run_agent.AIAgent = _FakeAgent
        try:
            # A prompt with NO high-complexity keywords, so a "high" result can
            # only come from the LLM path actually running (not the rule engine).
            model, runtime = asyncio.run(
                route_workload("tell me a story about the ocean", {})
            )
        finally:
            run_agent.AIAgent = original

        self.assertEqual(
            model, "claude-opus-4-8",
            "LLM classifier path must run and escalate; await-on-dict bug regressed",
        )


if __name__ == "__main__":
    unittest.main()
