# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The strong model that reads failures and writes tactics.

This is the teacher half of the loop, and it is the only part that leaves the
machine. That asymmetry is the whole economic argument for Puppy One: the device
serves every request for free, and a frontier model is consulted occasionally,
offline, to turn graded failures into a text file the device then reads forever.

**It proposes; it never grades.** The oracles decided what failed before this
runs. A reflector that could also mark things correct would be a model scoring
its own class of output, and it would rubber-stamp the way every LLM judge
rubber-stamps when nothing stops it. Here the worst it can do is suggest a bad
tactic, which the playbook rejects on specificity and the held-out split
retires within three rounds.

**It never sees the held-out split**, which is what makes a held-out gain mean
anything at all.

**It must not be local.** Reusing ``assert_auditor_is_not_local`` rather than
writing a second version, because a small model reflecting on a small model's
failures produces exactly the generic advice ACE calls brevity bias, and it
would be indistinguishable from the loop working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional, Sequence

from . import playbook as pb

logger = logging.getLogger(__name__)

# Its own auxiliary task, separate from `pkm_judge`. Reflection and judging are
# different jobs with different risk profiles and should be routable apart: a
# team might judge with one model and reflect with a cheaper one.
REFLECT_TASK = "puppy_reflect"

MAX_FAILURES_SHOWN = 25

_SYSTEM = (
    "You improve a small on-device model by writing tactics it can follow. "
    "You are given failures that deterministic checks already found, with the "
    "exact error text. You do not decide what is correct; that is settled."
)

_INSTRUCTION = """\
Below are failures from a small model on the `{suite}` task, each with the
oracle that caught it and the exact diagnostic.

{failures}

{existing}

Write tactics that would have prevented these specific failures.

Rules:
- Each tactic must be actionable by a model that only sees its own output before
  replying. "Be careful" is useless; "check that old_string does not appear
  inside new_string, or a retry duplicates the block" is not.
- Each tactic must cite the case_id it came from.
- Do not restate a tactic already listed above.
- Propose at most 4. Fewer good ones beat more weak ones.

Reply with JSON only:
{{"tactics": [{{"text": "...", "case_id": "...", "oracle": "..."}}]}}
"""


def build_prompt(failures: Sequence[dict], suite: str, existing: str = "") -> str:
    """The reflection prompt: diagnoses, not scores.

    The failure text is the payload. A score tells a reflector that something
    went wrong; ``line 620: unindent does not match any outer indentation
    level`` tells it what to write a tactic about.
    """
    lines = []
    for failure in list(failures)[:MAX_FAILURES_SHOWN]:
        oracles = ", ".join(failure.get("oracles") or []) or "unknown"
        lines.append(
            f"- case {failure.get('case_id')} failed [{oracles}]\n"
            f"    {failure.get('asi', '').strip()[:400]}"
        )
    prior = (
        f"Tactics already in the playbook (do not repeat):\n{existing.strip()}"
        if existing.strip()
        else "The playbook is currently empty."
    )
    return _INSTRUCTION.format(
        suite=suite, failures="\n".join(lines) or "(none)", existing=prior
    )


def parse_tactics(raw: str, suite: str) -> list:
    """Turn the reflector's reply into bullets, tolerantly.

    A reflector that wraps JSON in prose or a fence has still done the work, and
    discarding a whole round over formatting would make the loop look broken
    when it is not. What is *not* tolerated is a tactic with no case id: an
    uncited tactic cannot be audited or retired, and it is indistinguishable
    from one the reflector invented.
    """
    text = (raw or "").strip()
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        brace = text.find("{")
        if brace == -1:
            return []
        text = text[brace:]
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001
        logger.debug("reflector reply was not JSON", exc_info=True)
        return []

    bullets = []
    for entry in (payload.get("tactics") or []):
        if not isinstance(entry, dict):
            continue
        body = (entry.get("text") or "").strip()
        case_id = (entry.get("case_id") or "").strip()
        if not body or not case_id:
            continue
        bullets.append(
            pb.Bullet(
                text=body,
                case_id=case_id,
                suite=suite,
                oracle=(entry.get("oracle") or "").strip(),
            )
        )
    return bullets


def make_reflector(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    suite: str = "file_edit",
    timeout: float = 300.0,
    ask: Optional[Callable[[str], Any]] = None,
) -> Callable[[list, str], list]:
    """A ``reflect(failures, playbook_text)`` backed by a strong model.

    ``ask`` is injectable so the loop can be exercised without spending
    anything, which matters because most of what needs testing here is the
    parsing and the guards rather than the model.
    """
    if model:
        from hermes_cli.hussh_one_pkm.judge import assert_auditor_is_not_local

        assert_auditor_is_not_local(model, provider or "")

    def _ask_via_hermes(prompt: str) -> str:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task=REFLECT_TASK,
            provider=provider,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            timeout=timeout,
        )
        try:
            return response.choices[0].message.content
        except Exception:  # noqa: BLE001
            return str(response)

    caller = ask or _ask_via_hermes

    def reflect(failures: list, playbook_text: str) -> list:
        if not failures:
            return []
        prompt = build_prompt(failures, suite, playbook_text)
        try:
            raw = caller(prompt)
        except Exception as exc:  # noqa: BLE001
            # A reflector that cannot be reached is a round with no new tactics,
            # not a crashed run. The round still reports its held-out score.
            logger.warning("reflector unavailable: %s", exc)
            return []
        return parse_tactics(raw if isinstance(raw, str) else str(raw), suite)

    return reflect
