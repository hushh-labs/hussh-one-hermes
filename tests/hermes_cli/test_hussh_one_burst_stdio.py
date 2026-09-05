# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The burst bridge spoken to the way a client actually speaks to it.

Every other test in this package calls the tool handlers in-process. That
misses a whole class of failure, because a stdio MCP server is a *subprocess
that owns its stdout*: a stray `print` from any import breaks the transport
outright, and any probe whose first call in a process differs from its second
answers differently here than it does under pytest.

Both have already happened. `hussh_burst_device_status` reported `0.0` CPU load
on its first call — `psutil.cpu_percent(interval=None)` has no baseline until
it has been called once, and pytest had always called it long before the
in-process tests ran.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from hermes_cli.mcp_config import HUSSH_ONE_BURST_MCP_TOOLS

pytest.importorskip("mcp.server.fastmcp", reason="needs the Hermes MCP extra")

_TIMEOUT_S = 60


class _Client:
    """A minimal MCP stdio client: enough to prove the real transport works."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.hussh_one_burst.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"server closed stdout; stderr={self._drain_stderr()}")
        # A bare `print` anywhere in the import graph lands here and fails to
        # parse — which is exactly the breakage this test exists to catch.
        return json.loads(line)

    def _drain_stderr(self) -> str:
        assert self.proc.stderr is not None
        self.proc.stderr.close()
        return ""

    def request(self, method: str, params: dict | None = None, *, id_: int = 1) -> dict:
        self._send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
        return self._read()

    def handshake(self) -> dict:
        reply = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-test", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung server
            self.proc.kill()
            self.proc.wait(timeout=_TIMEOUT_S)


@pytest.fixture
def client():
    c = _Client()
    try:
        yield c
    finally:
        c.close()


def test_the_server_completes_a_real_mcp_handshake(client):
    result = client.handshake()["result"]
    assert result["serverInfo"]["name"] == "hussh-one-burst"
    assert result["protocolVersion"]


def test_the_tools_it_serves_are_the_tools_it_advertises(client):
    """The registration entry names five tools; the process must serve those five.

    Registration is a promise written into `config.yaml`. This is the only place
    the promise is checked against the running server.
    """
    client.handshake()
    served = {t["name"] for t in client.request("tools/list", id_=2)["result"]["tools"]}
    assert served == set(HUSSH_ONE_BURST_MCP_TOOLS)


def test_a_placement_decision_comes_back_over_the_wire(client):
    client.handshake()
    reply = client.request(
        "tools/call",
        {"name": "hussh_burst_decide", "arguments": {"preset_id": "photos-model"}},
        id_=3,
    )
    result = reply["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["target"] in {"device", "cloud"}
    assert payload["reason"]
    assert payload["measured_device"]["cpu_cores"] >= 1


def test_the_first_cpu_reading_of_a_fresh_process_is_not_a_fabricated_zero(client):
    """The regression that only a real subprocess can catch.

    Under pytest, psutil's counter is long since primed, so an in-process test
    sees a plausible number and passes. A person starting the bridge gets a
    genuinely first call.
    """
    client.handshake()
    reply = client.request(
        "tools/call", {"name": "hussh_burst_device_status", "arguments": {}}, id_=4
    )
    device = reply["result"]["structuredContent"]
    assert device["cpu_load_pct"] is None, (
        "the first sample in a process has no baseline; reporting 0.0 shows a "
        "pegged machine as idle"
    )


def test_an_unknown_preset_is_an_error_the_client_can_read(client):
    """It must fail as an MCP error, not by killing the transport."""
    client.handshake()
    reply = client.request(
        "tools/call",
        {"name": "hussh_burst_decide", "arguments": {"preset_id": "not-a-preset"}},
        id_=5,
    )
    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert "not-a-preset" in text
    # The message must name the alternatives, or a person is stuck.
    assert "photos-model" in text
