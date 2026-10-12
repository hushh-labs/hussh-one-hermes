# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Registration: the difference between "importable" and "reachable".

The burst module was written, tested and green for a while before anything could
call it — the scorecard tracked that as its own KPI. Building the MCP server
proves it *runs*; these tests prove Hermes would actually *load* it, which is a
different claim and the one that matters to a person.
"""

from __future__ import annotations

import pytest

from hermes_cli.mcp_config import (
    HUSSH_ONE_BURST_MCP_NAME,
    HUSSH_ONE_BURST_MCP_TOOLS,
    burst_mcp_server_entry,
    register_hussh_one_burst_mcp,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir so registration cannot touch a real profile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_the_entry_runs_this_interpreter_not_a_guessed_python():
    entry = burst_mcp_server_entry()
    assert entry["args"] == ["-m", "hermes_cli.hussh_one_burst.mcp_server"]
    assert entry["enabled"] is True
    assert entry["tools"] == HUSSH_ONE_BURST_MCP_TOOLS
    assert "python" in entry["command"].lower()


def test_registration_persists_and_survives_a_config_round_trip(isolated_home):
    from hermes_cli.config import load_config

    assert register_hussh_one_burst_mcp() is True
    entry = (load_config().get("mcp_servers") or {}).get(HUSSH_ONE_BURST_MCP_NAME)
    assert entry, "registration did not survive load_config()"
    assert entry["args"] == ["-m", "hermes_cli.hussh_one_burst.mcp_server"]
    assert len(entry["tools"]) == 5


def test_the_entry_passes_the_repo_s_own_security_validator(isolated_home):
    """`_save_mcp_server` rejects exfiltration-shaped stdio commands.

    Asserted directly as well, so a future change to the entry fails here with a
    reason rather than as a silent `False` return from registration.
    """
    from hermes_cli.mcp_security import validate_mcp_server_entry

    assert validate_mcp_server_entry(
        HUSSH_ONE_BURST_MCP_NAME, burst_mcp_server_entry()
    ) == []


def test_registering_twice_is_idempotent(isolated_home):
    from hermes_cli.config import load_config

    register_hussh_one_burst_mcp()
    first = load_config()["mcp_servers"][HUSSH_ONE_BURST_MCP_NAME]
    register_hussh_one_burst_mcp()
    assert load_config()["mcp_servers"][HUSSH_ONE_BURST_MCP_NAME] == first


def test_registration_does_not_disturb_other_servers(isolated_home):
    from hermes_cli.config import load_config, save_config

    config = load_config()
    config.setdefault("mcp_servers", {})["someone-elses"] = {
        "command": "/usr/bin/python3",
        "args": ["-m", "their.server"],
        "enabled": True,
    }
    save_config(config)

    register_hussh_one_burst_mcp()
    servers = load_config()["mcp_servers"]
    assert "someone-elses" in servers
    assert servers["someone-elses"]["args"] == ["-m", "their.server"]
    assert HUSSH_ONE_BURST_MCP_NAME in servers


def test_the_declared_tools_are_the_tools_the_server_actually_serves():
    """A config entry naming tools the server does not expose is a lie a person
    only discovers when a tool call fails."""
    import asyncio

    from hermes_cli.hussh_one_burst.mcp_server import _build_server

    served = {t.name for t in asyncio.run(_build_server().list_tools())}
    assert served == set(HUSSH_ONE_BURST_MCP_TOOLS)


def test_only_one_declared_tool_can_spend_money():
    """Four read-only tools and one gated action. If that ratio changes, the
    approval story changed with it and should be re-read, not assumed."""
    spending = [t for t in HUSSH_ONE_BURST_MCP_TOOLS if t.endswith("_run")]
    assert spending == ["hussh_burst_run"]
    assert len(HUSSH_ONE_BURST_MCP_TOOLS) == 5
