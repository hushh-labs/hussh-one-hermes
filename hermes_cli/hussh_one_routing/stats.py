# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Confidence intervals, because a point estimate is not a purchasing decision.

A model that scores 0.62 on fifty cases and one that scores 0.58 look like a
ranking and are not one. At n=50 the 95% interval on 0.62 runs from roughly 0.48
to 0.74, which overlaps almost everything. Shipping hardware on that difference
would be shipping on noise.

Wilson rather than the textbook normal approximation, for two reasons that both
bite at exactly the sample sizes available here: the normal interval goes below
zero and above one near the edges, and it collapses to zero width at a perfect
score. A model that got 20 of 20 has not been shown to be perfect; it has been
shown to be somewhere above about 0.83.
"""

from __future__ import annotations

import math
from typing import Optional

# 95% two-sided.
Z = 1.959963984540054


def wilson(successes: int, total: int, *, z: float = Z) -> tuple:
    """A confidence interval for a proportion that behaves at the edges."""
    if total <= 0:
        return (0.0, 1.0)
    successes = max(0, min(successes, total))
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def separated(a_successes: int, a_total: int, b_successes: int, b_total: int) -> bool:
    """True when two rates' intervals do not overlap.

    Deliberately conservative. Non-overlapping intervals is a stricter bar than
    a significance test, so calling two models separated by this rule is a claim
    that survives scrutiny. Calling them *not* separated when a test might have
    found a difference is the safe direction to be wrong in when the output is a
    hardware recommendation.
    """
    a_low, a_high = wilson(a_successes, a_total)
    b_low, b_high = wilson(b_successes, b_total)
    return a_low > b_high or b_low > a_high


def cases_needed(rate: float, margin: float, *, z: float = Z) -> int:
    """How many cases to pin a rate to within ``margin``.

    Answers the question that decides whether a run was long enough. At a rate
    near 0.5, holding the margin to five points needs about 385 cases; to ten
    points, about 97. Worth knowing before a run rather than after.
    """
    rate = min(max(rate, 0.0), 1.0)
    if margin <= 0:
        return 0
    return math.ceil((z * z * rate * (1 - rate)) / (margin * margin))


def describe(successes: int, total: int) -> dict:
    """A rate with its interval and the honest width of what it proves."""
    low, high = wilson(successes, total)
    return {
        "rate": round(successes / total, 4) if total else None,
        "n": total,
        "ci95": [round(low, 4), round(high, 4)],
        "width": round(high - low, 4),
    }
