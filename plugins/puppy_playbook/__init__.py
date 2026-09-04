# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Carry what an on-device model learned into the sessions it serves.

This is the edge that was missing. The measurement half of this fork -- the
exam, the oracles, the ledger -- was complete and disconnected, and the
improvement half -- memory, skills, background review -- was running with no
graded input. The learning loop grades on-device output offline and writes
tactics to a playbook; this plugin is what puts them in front of the model.

**It costs nothing at serve time.** The reflection that produced these tactics
happened offline and occasionally. What ships to the device is a text file, and
reading it is free forever after. That is the whole point of a $0 token budget:
pay once, in a place the customer never sees.

**Keyed by the model actually serving.** A playbook is per model and per suite,
never shared. The tactics that stop a 4B-active MoE truncating are not the ones
that stop a dense 27B duplicating a region, and handing each model the other's
advice is worse than handing it none.

Sections are frozen into the prompt once per session by core, so this renders on
a fresh session and never re-evaluates mid-conversation. That matters for the
prompt cache and it means a playbook edited during a session takes effect on the
next one, not this one.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SECTION_ID = "puppy-playbook"

# Suites whose learning is relevant to a live agent session. `replay` comes
# first: it is graded on the owner's real session turns, so its tactics are the
# closest thing to advice about the job the model is about to do. It was missing
# from this tuple until 2026-09-02, so every replay round's playbook was written
# and never read. `long_context` is deliberately absent: its findings are about
# budget and routing rather than anything a model can act on from its prompt.
LIVE_SUITES = ("replay", "file_edit", "terminal", "tool_select")

# Hard ceiling. The playbook is prepended to every call in the session, so it is
# a standing tax on both latency and the context the real task needs. Real
# prompts on this fork already reach a p90 of about 200k tokens, so there is
# very little room to give away.
MAX_CHARS = 4000


def render(session_info: Mapping[str, Any]) -> str:
    """Build the section for whichever model is serving this session.

    Fails open and silent. A missing, unreadable or empty playbook must return
    "" rather than raise: a learning system that can take down the agent it was
    meant to improve is a bad trade, and this runs on the founder's live
    WhatsApp path.
    """
    model = str(session_info.get("model") or "").strip()
    if not model:
        return ""

    try:
        from hermes_cli.hussh_one_routing import playbook as pb
    except Exception:  # noqa: BLE001
        logger.debug("playbook module unavailable", exc_info=True)
        return ""

    blocks = []
    for suite in LIVE_SUITES:
        try:
            book = pb.load(model, suite)
        except Exception:  # noqa: BLE001
            logger.debug("could not load playbook for %s/%s", model, suite)
            continue
        active = book.active_bullets
        if not active:
            continue
        lines = [f"## {suite}"]
        lines.extend(f"- {bullet.text}" for bullet in active)
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    header = (
        "# Learned from your own past mistakes\n\n"
        "Each line below was added because you previously failed a graded check "
        "on this exact kind of task. They are specific to you, not general "
        "advice.\n"
    )
    section = header + "\n" + "\n\n".join(blocks) + "\n"
    if len(section) > MAX_CHARS:
        # Truncate on a bullet boundary rather than mid-sentence: half a tactic
        # is worse than none, because the model will still try to follow it.
        kept, total = [], len(header) + 1
        for line in section[len(header) + 1:].splitlines():
            if total + len(line) + 1 > MAX_CHARS:
                break
            kept.append(line)
            total += len(line) + 1
        section = header + "\n" + "\n".join(kept) + "\n"
    return section


def register(ctx) -> None:
    """Register the section. Never raises into plugin discovery."""
    try:
        ctx.register_system_prompt_section(
            SECTION_ID,
            render,
            position="after_memory",
            max_chars=MAX_CHARS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("puppy-playbook section not registered: %s", exc)
