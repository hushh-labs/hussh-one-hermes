# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Deterministic natural-language detection for READ-ONLY status verbs.

Companion to :mod:`hermes_cli.natural_model_switch`.  It lets an owner ask,
over the chat bridge, "cron status" or "what are you doing" and get a
deterministic read instead of an LLM turn.  Like the model-switch detector it
uses NO LLM and only accepts short, direct, user-authored requests, so quoted
webpages, tool output, copied instructions, or prompt-injection text stay
ordinary chat.

Two deliberate differences from the model-switch detector:
  * These verbs are READ-ONLY — they never mutate runtime state — so the blast
    radius of a false positive is a status message, not a model change.
  * They are naturally phrased as questions ("what are you doing"), so the
    model-switch "reject anything starting with what/how/why" guard does NOT
    apply.  Instead each verb is matched by a tight positive allow-list regex.

The two injection guards below are copied verbatim from
``natural_model_switch`` (dangerous shapes + injection context).  Keep them in
sync with that module; they are the shared, canonical safety floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Allow-list of verbs this module can ever emit.  A caller maps each to a
# read-only slash command; anything outside this set is impossible by design.
READONLY_VERBS = frozenset({"cron_status", "on_device_compute"})


@dataclass(frozen=True)
class ReadonlyVerbIntent:
    """A parsed read-only status request.  ``verb`` is one of READONLY_VERBS."""

    verb: str


# --- Shared injection guards (keep in sync with natural_model_switch.py) ---
_DANGEROUS_SHAPES_RE = re.compile(
    r"https?://|www\.|```|`|^>|^\s*(?:[-*]|\d+[.)])\s+",
    re.IGNORECASE | re.MULTILINE,
)

_INJECTION_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"ignore\s+(?:all\s+)?(?:previous|prior)|"
    r"disregard\s+(?:all\s+)?(?:previous|prior)|"
    r"system\s+prompt|developer\s+message|prompt\s+injection|"
    r"tool\s+output|webpage\s+says|page\s+says|message\s+says|"
    r"quoted?|example|transcript"
    r")\b",
    re.IGNORECASE,
)

# Optional, permissive lead-in ("hey hermes,", "please", "can you", "show me").
_LEAD_IN = (
    r"(?:(?:hey\s+)?(?:hermes|hussh(?:\s+one)?)[,\s]+)?"
    r"(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"show\s+me\s+|tell\s+me\s+|give\s+me\s+|list\s+)?"
)

# cron_status — "cron status", "what's scheduled", "list your cron jobs",
# "your schedule", "scheduled jobs".
_CRON_STATUS_RE = re.compile(
    r"^\s*" + _LEAD_IN + r"(?:the\s+|your\s+)?(?:"
    r"cron(?:\s*[- ]?\s*(?:job|jobs)?\s*)?status|"
    r"cron(?:\s+jobs?)?|"
    r"scheduled\s+(?:jobs?|tasks?)|"
    r"schedule|"
    r"what(?:'?s|\s+is)\s+scheduled|"
    r"what\s+jobs?\s+(?:are\s+scheduled|do\s+you\s+have|are\s+running)"
    r")\b",
    re.IGNORECASE,
)

# on_device_compute — "what are you doing", "what's running", "your status",
# "your progress", "what agents are running", "on-device compute".
_ON_DEVICE_RE = re.compile(
    r"^\s*" + _LEAD_IN + r"(?:"
    r"what\s+are\s+you\s+(?:doing|working\s+on|running|up\s+to)|"
    r"what(?:'?s|\s+is)\s+running|"
    r"what(?:'?s|\s+is)\s+(?:going\s+on|happening)|"
    r"(?:your\s+)?(?:current\s+)?(?:status|progress|activity)|"
    r"(?:what|which)\s+agents?\s+(?:are\s+)?(?:running|active)|"
    r"active\s+agents?|"
    r"are\s+you\s+busy|"
    r"on[-\s]?device\s+(?:compute|status)"
    r")\b",
    re.IGNORECASE,
)


def _injection_safe(text: str) -> str:
    """Return a stripped, injection-safe utterance, or "" to reject.

    Mirrors ``natural_model_switch._safe_direct_user_text`` minus the
    switch-verb requirement and the question rejection (these verbs ARE
    questions).  Rejects slash commands, long/multiline pasted text, URLs,
    code blocks, lists, and injection-shaped context phrases.
    """
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return ""
    if len(stripped) > 180 or stripped.count("\n") > 1:
        return ""
    if _DANGEROUS_SHAPES_RE.search(stripped):
        return ""
    if _INJECTION_CONTEXT_RE.search(stripped):
        return ""
    return stripped


def parse_natural_readonly_verb(text: str) -> ReadonlyVerbIntent | None:
    """Return a read-only status verb for short, direct owner requests.

    Supported examples:
      - "cron status" / "what's scheduled" / "list your cron jobs"  -> cron_status
      - "what are you doing" / "what's running" / "your status"      -> on_device_compute

    Rejected examples include slash commands, URLs, code blocks, lists, long or
    multiline pasted text, and injection-shaped phrases ("ignore previous",
    "system prompt", "webpage says", ...).  READ-ONLY: never mutates state.
    """
    safe_text = _injection_safe(text)
    if not safe_text:
        return None

    # Cron is checked first: it is the more specific intent, and "what jobs are
    # running" reads as scheduling, not live compute.
    if _CRON_STATUS_RE.search(safe_text):
        return ReadonlyVerbIntent(verb="cron_status")
    if _ON_DEVICE_RE.search(safe_text):
        return ReadonlyVerbIntent(verb="on_device_compute")
    return None
