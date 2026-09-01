# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Running the Hussh One agent hierarchy on this machine.

The manifest is the portable unit: the same file describes an agent in the
cloud, in the pod, and here. These tests are about reading it correctly and
about never claiming parity that was not measured.
"""

from __future__ import annotations

import pytest

from hermes_cli import hussh_one_agent_manifest as am


MANIFEST = """\
manifest_version: 2
id: agent_memory_intent
name: Memory Intent Agent
version: 1.0.0
description: Determines whether a message should become PKM.
model: gemini-3.7-flash
system_instruction: |
  You are the Memory Intent Agent.

  - Never invent domains, paths, or values.
  Return JSON only.
capabilities:
  something: else
"""

WITH_PARENT = """\
id: agent_connections
parent: agent_nav
model: gemini-3.7-flash
"""

ROOT_MANIFEST = """\
id: agent_one
parent: null
model: gemini-3.7-flash
"""


class TestParsing:
    def test_it_reads_the_fields_the_runtime_needs(self):
        m = am.parse_manifest(MANIFEST)
        assert m.id == "agent_memory_intent"
        assert m.name == "Memory Intent Agent"
        assert m.model == "gemini-3.7-flash"

    def test_the_block_instruction_survives_intact(self):
        m = am.parse_manifest(MANIFEST)
        assert m.system_instruction.startswith("You are the Memory Intent Agent.")
        assert "Never invent domains" in m.system_instruction
        assert "Return JSON only." in m.system_instruction
        # The keys after the block must not bleed into it.
        assert "capabilities" not in m.system_instruction

    def test_the_default_parent_is_the_root(self):
        # 13 of 19 manifests never write `parent:`; a reader that required the
        # key would report a 6-node tree.
        assert am.parse_manifest(MANIFEST).parent == am.DEFAULT_ROOT_AGENT

    def test_an_explicit_parent_wins(self):
        assert am.parse_manifest(WITH_PARENT).parent == "agent_nav"

    def test_an_explicit_null_parent_is_the_root_itself(self):
        assert am.parse_manifest(ROOT_MANIFEST).parent is None

    def test_a_manifest_without_an_id_is_refused(self):
        with pytest.raises(am.ManifestError):
            am.parse_manifest("name: nameless\nmodel: x\n")


class TestProviderInference:
    def test_a_gemini_model_implies_the_gemini_provider(self):
        # Only three manifests write `provider:`. A loader trusting the field
        # alone would report nineteen agents as having no provider and treat
        # them as safe to run anywhere.
        assert am.parse_manifest(MANIFEST).declared_provider == "gemini"

    def test_an_explicit_provider_wins_over_inference(self):
        m = am.parse_manifest("id: a\nmodel: gemini-3.7-flash\nprovider: vertex\n")
        assert m.declared_provider == "vertex"

    @pytest.mark.parametrize(
        "model,provider",
        [("claude-opus-5", "anthropic"), ("gpt-5", "openai"), ("o1-mini", "openai")],
    )
    def test_other_families_are_inferred_too(self, model, provider):
        m = am.parse_manifest(f"id: a\nmodel: {model}\n")
        assert m.declared_provider == provider

    def test_an_unknown_model_infers_nothing_rather_than_guessing(self):
        assert am.parse_manifest("id: a\nmodel: mystery-7b\n").declared_provider == ""

    def test_a_cloud_agent_is_flagged_as_leaving_the_machine(self):
        assert am.parse_manifest(MANIFEST).leaves_machine_as_authored is True

    def test_a_local_provider_does_not_leave(self):
        m = am.parse_manifest("id: a\nmodel: gemma\nprovider: lmstudio\n")
        assert m.leaves_machine_as_authored is False


class TestHierarchy:
    def test_children_are_grouped_under_their_parent(self):
        manifests = [
            am.parse_manifest(MANIFEST),
            am.parse_manifest(WITH_PARENT),
            am.parse_manifest(ROOT_MANIFEST),
        ]
        tree = am.build_hierarchy(manifests)
        assert tree["agent_nav"] == ["agent_connections"]
        assert "agent_memory_intent" in tree[am.DEFAULT_ROOT_AGENT]

    def test_the_root_has_no_parent_entry(self):
        tree = am.build_hierarchy([am.parse_manifest(ROOT_MANIFEST)])
        assert tree == {}


class TestRuntimeResolution:
    def test_off_device_keeps_the_authored_binding(self):
        m = am.parse_manifest(MANIFEST)
        runtime = am.resolve_runtime(m, on_device=False)
        assert runtime["provider"] == "gemini"
        assert runtime["model"] == "gemini-3.7-flash"
        assert runtime["substituted"] is False

    def test_on_device_runs_the_same_instruction_on_the_local_model(self):
        m = am.parse_manifest(MANIFEST)
        runtime = am.resolve_runtime(
            m, on_device=True, local_model="google/gemma-4-26b-a4b-qat"
        )
        assert runtime["provider"] == "lmstudio"
        assert runtime["model"] == "google/gemma-4-26b-a4b-qat"

    def test_a_substituted_model_says_so(self):
        # An agent running on a different model is a different measurement.
        # Swapping it silently is how a benchmark starts describing a system
        # nobody deployed.
        m = am.parse_manifest(MANIFEST)
        runtime = am.resolve_runtime(m, on_device=True, local_model="gemma")
        assert runtime["substituted"] is True
        assert runtime["authored_model"] == "gemini-3.7-flash"

    def test_no_substitution_is_reported_when_the_model_is_unchanged(self):
        m = am.parse_manifest("id: a\nmodel: gemma\n")
        runtime = am.resolve_runtime(m, on_device=True, local_model="gemma")
        assert runtime["substituted"] is False


class TestAudit:
    def test_it_counts_what_leaves_as_authored(self):
        manifests = [am.parse_manifest(MANIFEST), am.parse_manifest(WITH_PARENT)]
        assert am.audit_hierarchy(manifests, on_device=False)["leaves_machine"] == 2

    def test_on_device_nothing_leaves(self):
        manifests = [am.parse_manifest(MANIFEST), am.parse_manifest(WITH_PARENT)]
        assert am.audit_hierarchy(manifests, on_device=True)["leaves_machine"] == 0


class TestRunning:
    def test_it_sends_the_instruction_as_the_system_message(self):
        seen = {}

        def _call(model, messages, temperature):
            seen["model"] = model
            seen["messages"] = messages
            return '{"intent": "health"}'

        m = am.parse_manifest(MANIFEST)
        result = am.run_agent(m, "I stopped eating dairy.", call=_call, model="gemma")
        assert result["ok"] is True
        assert seen["messages"][0]["role"] == "system"
        assert "Memory Intent Agent" in seen["messages"][0]["content"]
        assert seen["messages"][1]["content"] == "I stopped eating dairy."

    def test_a_failing_call_is_reported_not_raised(self):
        def _explode(**_kw):
            raise TimeoutError("model is down")

        result = am.run_agent(
            am.parse_manifest(MANIFEST), "x", call=_explode, model="gemma"
        )
        assert result["ok"] is False
        assert "TimeoutError" in result["error"]


class TestJsonParsing:
    def test_fenced_json_is_read(self):
        # The manifests all say "never return markdown"; models fence anyway.
        value, error = am.parse_agent_json('```json\n{"intent": "health"}\n```')
        assert value == {"intent": "health"}
        assert error == ""

    def test_plain_json_is_read(self):
        assert am.parse_agent_json('{"a": 1}')[0] == {"a": 1}

    def test_an_empty_reply_is_distinguished_from_a_bad_one(self):
        # An agent that answered nothing and one that answered badly need
        # different fixes.
        assert am.parse_agent_json("")[1] == "empty reply"
        assert "unparseable" in am.parse_agent_json("not json")[1]
