# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The file a model reads to be better at a job than it was yesterday.

This is where the learning lands. The on-device model generates, deterministic
oracles verify, a stronger model reflects on the failures, and what it learns
becomes bullets in here. Serving stays free: the reflection happens offline and
occasionally, and the device then reads a text file forever after.

The design follows ACE (arXiv 2510.04618), which names the two ways a
self-editing context rots, and both are worth stating because the obvious
implementation hits both:

**Context collapse.** Rewriting the whole file each round lets a summarising
model quietly drop detail that took many rounds to earn. So updates are
append-only deltas. Nothing here ever regenerates the file from scratch.

**Brevity bias.** A reflector asked to "improve the playbook" tends toward
shorter, more general advice, and general advice is exactly what a small model
already has. So a bullet must be specific enough to cite the case that produced
it, and one that cannot is rejected.

A third rule is ours, from watching this fail elsewhere: a bullet that does not
earn its place is retired. Without that the playbook only grows, and a growing
context is itself a cost the model pays on every single call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A bullet must survive this many evaluated rounds without helping before it is
# retired. One bad round is noise; three is a pattern.
RETIREMENT_ROUNDS = 3

# Hard ceiling on playbook size. The playbook is prepended to every call, so it
# is a tax on latency and on the context the real task needs. Measured: real
# prompts already reach p90 of 200k tokens, so there is not much room to spare.
MAX_BULLETS = 40


@dataclass
class Bullet:
    """One learned tactic, and the evidence that earned it."""

    text: str
    # The exam case whose failure produced this. An uncited bullet is
    # indistinguishable from a hallucinated one, and cannot be audited later.
    case_id: str
    suite: str
    oracle: str = ""
    added_round: int = 0
    # Rounds this bullet was in the playbook and the held-out score did not
    # improve. Reset whenever it does.
    idle_rounds: int = 0
    retired: bool = False
    retired_reason: str = ""

    @property
    def active(self) -> bool:
        return not self.retired


@dataclass
class Playbook:
    """Per model, per suite. Never shared between them.

    Shared would be worse than useless: the tactics that help a 4B MoE stop
    truncating are not the tactics that help a dense 27B stop duplicating a
    region, and merging them gives every model advice aimed at someone else.
    """

    model: str
    suite: str
    bullets: list = field(default_factory=list)
    round_number: int = 0
    history: list = field(default_factory=list)

    @property
    def active_bullets(self) -> list:
        return [b for b in self.bullets if b.active]

    def render(self) -> str:
        """The text injected into the model's system prompt."""
        active = self.active_bullets
        if not active:
            return ""
        lines = [
            f"# Learned tactics for {self.suite}",
            "",
            "These come from your own previous mistakes on this exact task.",
            "",
        ]
        lines.extend(f"- {b.text}" for b in active)
        return "\n".join(lines) + "\n"

    def add(self, bullet: Bullet) -> bool:
        """Append one bullet. Returns False when it was rejected.

        Rejection is not failure. A reflector that proposes a duplicate or a
        vague platitude should have that dropped quietly rather than allowed to
        dilute what is already there.
        """
        if not is_specific(bullet.text):
            logger.debug("rejected vague bullet: %s", bullet.text[:60])
            return False
        if any(_normalise(b.text) == _normalise(bullet.text) for b in self.bullets):
            return False
        if len(self.active_bullets) >= MAX_BULLETS:
            logger.debug("playbook full at %d bullets", MAX_BULLETS)
            return False
        bullet.added_round = self.round_number
        self.bullets.append(bullet)
        return True

    def record_round(self, *, held_out_score: float, improved: bool) -> list:
        """Close a round, and retire whatever stopped earning its place.

        Only the held-out score decides. A bullet that lifts the training split
        and not the held-out one has taught the model this corpus, not this job,
        which is the failure mode Pioneer Agent names for exactly this loop.
        """
        self.round_number += 1
        self.history.append(
            {"round": self.round_number, "held_out": held_out_score,
             "improved": improved}
        )
        retired = []
        for bullet in self.active_bullets:
            if improved:
                bullet.idle_rounds = 0
                continue
            bullet.idle_rounds += 1
            if bullet.idle_rounds >= RETIREMENT_ROUNDS:
                bullet.retired = True
                bullet.retired_reason = (
                    f"held-out score did not improve in {RETIREMENT_ROUNDS} "
                    "rounds while this was active"
                )
                retired.append(bullet)
        return retired

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "suite": self.suite,
            "round_number": self.round_number,
            "bullets": [asdict(b) for b in self.bullets],
            "history": list(self.history),
        }


# Words that make a bullet a platitude. A model already knows to "be careful";
# what it does not know is that its own last patch left old_string inside
# new_string.
_VAGUE = re.compile(
    r"^\W*(be |try to |make sure to |remember to |always |never )?"
    r"(careful|accurate|correct|precise|thorough|mindful|attentive)\W*$",
    re.I,
)

_MIN_BULLET_CHARS = 25


def is_specific(text: str) -> bool:
    """Reject advice a model could have written without seeing any evidence.

    This is the brevity-bias guard. A reflector under pressure to be concise
    drifts toward general advice, and general advice is what a small model
    already has plenty of.
    """
    stripped = (text or "").strip()
    if len(stripped) < _MIN_BULLET_CHARS:
        return False
    if _VAGUE.match(stripped):
        return False
    return True


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def playbook_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "puppy-playbooks"


def path_for(model: str, suite: str) -> Path:
    """Where one model's playbook for one suite lives.

    The model id is slugified because it carries a publisher prefix and a
    slash, and a slash would silently create a directory tree that a later
    lookup would not find.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    return playbook_dir() / slug / f"{suite}.json"


def load(model: str, suite: str) -> Playbook:
    """Read a playbook, or start an empty one."""
    path = path_for(model, suite)
    if not path.exists():
        return Playbook(model=model, suite=suite)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("unreadable playbook at %s; starting fresh", path)
        return Playbook(model=model, suite=suite)
    return Playbook(
        model=payload.get("model", model),
        suite=payload.get("suite", suite),
        round_number=payload.get("round_number", 0),
        bullets=[Bullet(**b) for b in payload.get("bullets", [])],
        history=payload.get("history", []),
    )


def save(playbook: Playbook) -> Path:
    """Write a playbook, keeping retired bullets.

    Retired bullets stay in the file. Deleting them would let the loop re-learn
    and re-retire the same tactic forever, and the record of what was tried and
    did not work is worth as much as the record of what did.
    """
    path = path_for(playbook.model, playbook.suite)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(playbook.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def render_markdown(playbook: Playbook) -> str:
    """A human-readable view, including what was retired and why.

    The founder has to be able to read what his agent taught itself and veto it.
    A learning loop whose output nobody can inspect is one nobody should trust.
    """
    lines = [f"# {playbook.model} / {playbook.suite}", ""]
    lines.append(f"Round {playbook.round_number}. "
                 f"{len(playbook.active_bullets)} active tactics.")
    lines.append("")
    for bullet in playbook.active_bullets:
        lines.append(f"- {bullet.text}")
        lines.append(f"  - learned round {bullet.added_round} "
                     f"from `{bullet.case_id}` ({bullet.oracle or 'no oracle'})")
    retired = [b for b in playbook.bullets if b.retired]
    if retired:
        lines.extend(["", "## Retired", ""])
        for bullet in retired:
            lines.append(f"- ~~{bullet.text}~~")
            lines.append(f"  - {bullet.retired_reason}")
    return "\n".join(lines) + "\n"
