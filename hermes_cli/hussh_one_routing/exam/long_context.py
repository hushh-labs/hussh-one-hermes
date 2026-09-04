# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Does the model still work when the prompt is the size a real one is?

The median real request here is about 24,500 tokens and the p90 is roughly
200,000. Those are measured with the actual Gemma-4 tokenizer on this machine
rather than estimated, because the usual ``chars/4`` rule undercounts this
corpus by 31%: code, JSON and base64 run about 3.05 characters per token.

Three of the 122 real requests exceed the 262,144 maximum window of every model
on the ladder. Some real work cannot be tested at full fidelity by anything we
have, and that belongs in the report rather than in a footnote.

Nothing measured any of this before. A model that is fine on a 6k prompt and
collapses at 85k is useless in practice, and the merge suite ran at under a
thousand tokens, which is 4% of the median real load.

Two designs here, and the second is the one that matters:

**Needle recall** places a verifiable fact at a controlled depth in real session
content and asks for it back. It is easy to grade and easy to over-trust: recall
is necessary for long-context competence and nowhere near sufficient.

**Paired degradation** runs the *same task* at two context sizes and compares.
That is the question the product actually asks: not "can it find a sentence"
but "does it still pick the right tool and produce valid output when the prompt
is thirty times longer". The pair shares everything except padding, so a
difference is attributable to length rather than to difficulty.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional, Sequence

from .model import FAIL, PASS, SKIP, Outcome, Verdict

SUITE_ID = "long_context"

# Depths at which a needle is planted, as a fraction through the filler.
DEPTHS = (0.1, 0.5, 0.9)

# Measured on this corpus with the real Gemma-4 tokenizer. Not the generic 4.0.
CHARS_PER_TOKEN = 3.05

# A model repeating itself is a distinct long-context failure from getting the
# answer wrong, and it is the one that burns a whole budget.
_REPEAT_RUN = 6

CANNOT_CATCH = (
    "Whether recall implies reasoning. A model can return a planted sentence "
    "verbatim and still be unable to use anything else in the window.",
    "The largest real prompts. Three of 122 exceed 262,144 tokens, which is the "
    "maximum window of every model on this ladder, so they cannot be run at all.",
    "Attention position effects beyond the three depths sampled, which is a "
    "coarse grid over a window that reaches 260k tokens.",
)


def make_needle(seed: str) -> tuple:
    """A fact that cannot be guessed, inferred, or already known.

    Derived from a hash so it is stable across runs and impossible to answer
    from training data. A memorable-looking needle ("the capital is Paris") can
    be answered without reading the context at all, which is how a needle test
    accidentally measures nothing.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    token = digest[:10].upper()
    sentence = (
        f"Internal audit reference for this session is {token}. "
        "Quote it exactly if asked."
    )
    return sentence, token


def plant(filler: str, needle: str, depth: float) -> str:
    """Insert the needle at a fractional depth, on a line boundary.

    Line boundaries matter: splitting a JSON blob or a code line mid-token
    produces a malformed context, and then a failure means "we corrupted the
    input" rather than "the model could not find it".
    """
    lines = filler.splitlines()
    if not lines:
        return needle
    index = max(0, min(len(lines), int(len(lines) * depth)))
    return "\n".join(lines[:index] + [needle] + lines[index:])


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def check_needle_recalled(answer: str, token: str) -> Outcome:
    """The planted token comes back, exactly."""
    if not token:
        return Outcome("needle_recalled", SKIP, "no needle planted")
    if token.lower() in (answer or "").lower():
        return Outcome("needle_recalled", PASS)
    return Outcome(
        "needle_recalled", FAIL,
        f"planted reference {token} not present in the answer",
    )


def check_no_false_needle(answer: str, token: str, decoy: str) -> Outcome:
    """The negative control: a model must not report a needle that is not there.

    Without this, a model that emits a plausible-looking reference on every
    query scores as a perfect recaller. The decoy is a token of the same shape
    that was never planted.
    """
    if not decoy:
        return Outcome("needle_negative_control", SKIP, "no decoy configured")
    if decoy.lower() in (answer or "").lower():
        return Outcome(
            "needle_negative_control", FAIL,
            f"reported {decoy}, which was never in the context",
        )
    return Outcome("needle_negative_control", PASS)


def check_not_degenerate(answer: str) -> Outcome:
    """No collapse into repetition.

    A long-context failure mode of its own: the model stops answering and starts
    looping, consuming the entire budget. Distinct from a wrong answer, and it
    needs a different fix.
    """
    text = (answer or "").strip()
    if not text:
        return Outcome("no_degenerate_output", FAIL, "empty answer")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= _REPEAT_RUN:
        for index in range(len(lines) - _REPEAT_RUN + 1):
            window = lines[index : index + _REPEAT_RUN]
            if len(set(window)) == 1:
                return Outcome(
                    "no_degenerate_output", FAIL,
                    f"the same line repeated {_REPEAT_RUN} times",
                )
    words = text.split()
    if len(words) >= 40:
        tail = words[-30:]
        if len(set(tail)) <= 2:
            return Outcome(
                "no_degenerate_output", FAIL, "output collapsed into a repeated token"
            )
    return Outcome("no_degenerate_output", PASS)


def grade_needle(
    *, case_id: str, answer: str, token: str, decoy: str = ""
) -> Verdict:
    """Grade one needle-recall case."""
    verdict = Verdict(case_id=case_id, suite=SUITE_ID)
    verdict.outcomes = [
        check_needle_recalled(answer, token),
        check_no_false_needle(answer, token, decoy),
        check_not_degenerate(answer),
    ]
    return verdict


def degradation(short: Sequence[Verdict], long: Sequence[Verdict]) -> dict:
    """Compare the same tasks at two context sizes.

    The pair is the point. An absolute score at 200k tokens says little on its
    own, because a hard task is hard at any length; the delta against the same
    task at 6k isolates what length itself cost.
    """
    def rate(verdicts):
        gradeable = [v for v in verdicts if not v.indeterminate]
        if not gradeable:
            return None
        return sum(1 for v in gradeable if v.ok) / len(gradeable)

    short_rate, long_rate = rate(short), rate(long)
    if short_rate is None or long_rate is None:
        return {
            "comparable": False,
            "reason": "one side produced no gradeable turns",
            "short": short_rate,
            "long": long_rate,
        }

    short_ids = {v.case_id for v in short}
    long_ids = {v.case_id for v in long}
    if short_ids != long_ids:
        # Different tasks at different lengths is not a paired design, and the
        # delta would mix task difficulty into a length effect.
        return {
            "comparable": False,
            "reason": (
                "the two sides ran different cases; a degradation delta needs "
                "the same task at both lengths"
            ),
            "short": short_rate,
            "long": long_rate,
        }

    truncated_long = sum(1 for v in long if v.indeterminate)
    return {
        "comparable": True,
        "short": round(short_rate, 4),
        "long": round(long_rate, 4),
        "delta": round(long_rate - short_rate, 4),
        "indeterminate_long": truncated_long,
        "caveat": (
            "A drop here is length plus everything that scales with it, "
            "including a bigger tool catalog and more conversation history."
        ),
    }
