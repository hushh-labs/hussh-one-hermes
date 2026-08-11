# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from toolsets import TOOLSETS
from tools.registry import registry

import tools.hussh_one_source_library_tool as source_tools


def test_source_library_tools_are_isolated_in_local_toolset() -> None:
    expected = {
        "hussh_one_source_scan",
        "hussh_one_source_browse",
        "hussh_one_source_read",
        "hussh_one_source_propose_knowledge",
        "hussh_one_source_propose_memory_sync",
        "hussh_one_source_propose_file_operation",
        "hussh_one_source_share",
    }
    assert {
        name
        for name in registry.get_all_tool_names()
        if registry.get_toolset_for_tool(name) == "hussh_one_sources"
    } == expected
    assert registry.get_toolset_for_tool("ask_file_steward") == "hussh_one"
    assert registry.get_toolset_for_tool("ask_source_library_steward") == "hussh_one"
    for name in {
        "hussh_one_source_bind",
        "hussh_one_source_commit_knowledge",
        "hussh_one_source_commit_file_operation",
        "hussh_one_source_share_admin",
    }:
        assert registry.get_toolset_for_tool(name) == "hussh_one"
    assert "terminal" not in expected
    assert "read_file" not in expected
    assert "delegate_task" not in expected


def test_file_steward_schema_does_not_expose_internal_toolset_override() -> None:
    schema = registry.get_schema("ask_file_steward")
    assert schema is not None
    assert set(schema["parameters"]["properties"]) == {"request"}
    assert "toolsets" not in schema["parameters"]["properties"]
    assert "source-derived preferences" in schema["description"]
    assert "provider ACL/permission changes" in schema["description"]


def test_source_library_steward_naturally_covers_management_and_sharing() -> None:
    schema = registry.get_schema("ask_source_library_steward")
    assert schema is not None
    description = schema["description"]
    assert "find, organize, manage, or share" in description
    assert "iCloud Drive" in description
    assert "Google Drive" in description


def test_locked_or_unbound_profiles_fail_source_availability_gate(monkeypatch) -> None:
    monkeypatch.setattr(source_tools, "_local_source_surface", lambda: True)
    monkeypatch.setattr(source_tools, "_connector_enrolled", lambda: True)
    monkeypatch.setattr(
        source_tools,
        "get_profile_bridge",
        lambda: SimpleNamespace(vault_status=lambda: {"unlocked": False}),
    )
    assert source_tools._vault_unlocked() is False
    assert source_tools._source_library_ready() is False

    monkeypatch.setattr(source_tools, "_local_source_surface", lambda: False)
    assert source_tools._vault_unlocked() is False
    monkeypatch.setattr(source_tools, "_local_source_surface", lambda: True)

    monkeypatch.setattr(source_tools, "_vault_unlocked", lambda: True)
    monkeypatch.setattr(
        source_tools,
        "_library",
        lambda: SimpleNamespace(list_sources=lambda: {"sources": []}),
    )
    assert source_tools._source_library_ready() is False


def test_messaging_platform_toolsets_do_not_include_source_library() -> None:
    source_names = {
        "hussh_one_source_bind",
        "hussh_one_source_scan",
        "hussh_one_source_browse",
        "hussh_one_source_read",
        "hussh_one_source_propose_knowledge",
        "hussh_one_source_commit_knowledge",
    }
    for toolset_name in (
        "hermes-telegram",
        "hermes-discord",
        "hermes-whatsapp",
        "hermes-slack",
        "hermes-signal",
    ):
        tools = TOOLSETS[toolset_name]["tools"]
        assert isinstance(tools, list)
        assert source_names.isdisjoint(tools)


def test_hand_edited_messaging_config_cannot_enable_source_toolset() -> None:
    from hermes_cli.tools_config import _get_platform_tools

    config = {"platform_toolsets": {"telegram": ["hussh_one_sources"]}}
    assert "hussh_one_sources" not in _get_platform_tools(config, "telegram")
