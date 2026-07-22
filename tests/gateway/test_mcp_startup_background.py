# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Gateway MCP discovery must never block channel startup."""

from gateway import run as gateway_run


class _Future:
    def __init__(self):
        self.callback = None

    def add_done_callback(self, callback):
        self.callback = callback

    def result(self):
        return None


class _Loop:
    def __init__(self, future):
        self.future = future
        self.calls = []

    def run_in_executor(self, executor, function):
        self.calls.append((executor, function))
        return self.future


def test_gateway_mcp_discovery_is_started_without_awaiting(monkeypatch):
    """A slow optional MCP server cannot delay the listener bind path."""
    from tools import mcp_tool

    future = _Future()
    loop = _Loop(future)
    discovered = []

    def discover():
        discovered.append(True)

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", discover)

    gateway_run._start_background_gateway_mcp_discovery(loop)

    assert loop.calls == [(None, discover)]
    assert future.callback is not None
    assert discovered == []

    future.callback(future)
    assert discovered == []
