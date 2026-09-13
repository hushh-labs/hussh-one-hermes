# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The consent gate, driven by a real MCP client that answers the prompt.

Every other test of `hussh_burst_run` mocks the elicitation away, which proves
the handler branches correctly and proves nothing about whether a person is ever
actually asked. This runs the server as a subprocess and answers as a real
client would, through the SDK's `elicitation_callback`.

The gate is the only thing standing between a tool call and money leaving
somebody's cloud account, so "we believe it prompts" is not a good enough
standard for it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp", reason="needs the Hermes MCP extra")
from mcp import ClientSession, StdioServerParameters, types  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

def _params(home: Path) -> StdioServerParameters:
    """Run the server against a throwaway profile.

    `hussh_burst_run` records every receipt, so driving the real tool writes to
    whatever `HERMES_HOME` resolves to. Without this the suite appends mock
    receipts to the owner's `burst-receipts.jsonl` on every run — which is an
    audit trail, and salting it with simulated bursts is precisely what it must
    not contain. Found the hard way: an exploratory run of these paths put two
    of them there before this fixture existed.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "hermes_cli.hussh_one_burst.mcp_server"],
        env=env,
    )


def _ask(home: Path, answer: str, confirm: bool | None = None, **args):
    """Call the run tool with a client that answers the consent prompt."""
    seen: dict = {}

    async def on_elicit(_context, params):
        seen["message"] = params.message
        seen["schema"] = params.requestedSchema
        if answer == "accept":
            return types.ElicitResult(action="accept", content={"confirm": confirm})
        return types.ElicitResult(action=answer)

    async def drive():
        async with stdio_client(_params(home)) as (r, w):
            async with ClientSession(r, w, elicitation_callback=on_elicit) as s:
                await s.initialize()
                res = await s.call_tool(
                    "hussh_burst_run",
                    {"preset_id": "photos-model", "minutes": 1.0, "provider": "mock", **args},
                )
                return res.structuredContent or json.loads(res.content[0].text)

    return seen, asyncio.run(drive())


def test_the_person_is_actually_asked_before_anything_is_provisioned(tmp_path):
    seen, payload = _ask(tmp_path, "accept", confirm=True)
    assert seen, "no elicitation ever reached the client"
    assert payload["success"] is True


def test_the_prompt_names_the_hardware_the_rate_and_the_total(tmp_path):
    """Consent to an unnamed price is not consent."""
    seen, _payload = _ask(tmp_path, "accept", confirm=True)
    message = seen["message"]
    assert "NVIDIA T4" in message
    assert "$0.35/hour" in message
    assert "$" in message.split("Estimated:", 1)[1], "no total shown next to the estimate"
    assert "torn down" in message, "the teardown promise is part of what is consented to"


def test_declining_provisions_nothing(tmp_path):
    _seen, payload = _ask(tmp_path, "decline")
    assert payload["success"] is False
    assert payload["status"] == "declined"
    assert "instance_id" not in payload, "something was provisioned despite a refusal"


def test_cancelling_provisions_nothing(tmp_path):
    _seen, payload = _ask(tmp_path, "cancel")
    assert payload["success"] is False
    assert payload["status"] == "declined"
    assert "instance_id" not in payload


def test_accepting_the_form_with_the_box_unticked_is_still_a_refusal(tmp_path):
    """The subtle path: the client returns `accept` for the form, and `false`
    for the field inside it. That is a person who opened the dialog and said no,
    and it must not read as approval."""
    _seen, payload = _ask(tmp_path, "accept", confirm=False)
    assert payload["success"] is False
    assert payload["status"] == "declined"
    assert "instance_id" not in payload


def test_a_refused_burst_is_pre_flighted_but_never_reaches_the_provider(tmp_path):
    """A job too large is refused before the person is asked at all."""
    seen, payload = _ask(tmp_path, "accept", confirm=True, preset_id=None, vram_gb=5000.0)
    assert payload["status"] == "does_not_fit"
    assert not seen, "asked for approval of hardware that cannot hold the job"


def test_an_approved_burst_writes_its_receipt_to_the_profile_it_was_told_to(tmp_path):
    """And nowhere else. This is the guard on the fixture above.

    A receipt has to land somewhere findable, and it has to land in the profile
    the server was pointed at rather than the owner's. It also has to be marked
    `simulated`, because a mock receipt otherwise reads exactly like a real
    burst — same `status: completed`, same `success: true` — in the one file
    somebody opens to ask what a burst cost.
    """
    _seen, payload = _ask(tmp_path, "accept", confirm=True)
    assert payload["success"] is True

    ledger = tmp_path / "burst-receipts.jsonl"
    assert ledger.exists(), "an approved burst left no receipt"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["simulated"] is True
    assert rows[0]["destination"].startswith("mock://")


def test_a_refusal_writes_no_receipt_at_all(tmp_path):
    """Nothing was provisioned, so there is nothing to account for."""
    _seen, payload = _ask(tmp_path, "decline")
    assert payload["status"] == "declined"
    assert not (tmp_path / "burst-receipts.jsonl").exists()
