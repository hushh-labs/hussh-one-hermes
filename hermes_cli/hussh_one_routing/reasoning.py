# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Turn a model's thinking up, using the control it actually honours.

The obvious lever does not work. On this LM Studio build ``reasoning_effort`` on
``/v1/chat/completions`` is inert: ``none``, ``low``, ``minimal`` and ``high``
all return byte-identical reasoning-token counts, and so does
``chat_template_kwargs`` with ``enable_thinking: false``. Measured on two models:

    gemma-4-26b-a4b-qat   1484 tokens at every setting
    gemma-4-31b-qat        345 tokens at every setting

LM Studio documents ``reasoning.effort`` for ``/v1/responses`` on one specific
model, and its bug tracker carries an open report that the parameter is dropped
on the chat-completions route while the GUI setting wins instead. ``lms load``
exposes no reasoning option either. So there is no scriptable API-side control
on this stack at all.

**What does work is embedded in the prompt**, because the server has no way to
strip it out of message text:

  * Gemma 4 uses a ``<|think|>`` token in the system prompt. Google documents
    this as binary on/off; no ``thinking_budget`` parameter exists.
  * Qwen 3 honours ``/think`` and ``/no_think`` soft switches, and
    ``qwen3.8-27b`` additionally supports ``reasoning_effort`` natively -- the
    only model on this ladder that does.

One counter-intuitive measurement shapes the defaults: **adding a plain system
prompt roughly doubled reasoning on both models** (1484 to 2468, and 345 to
586). Only the family-specific token with an explicit instruction brought it
down. So a shared suite prompt is not neutral, and the control belongs here,
per model, rather than in any suite's prompt text.

The goal is *more* thinking, not less. Accuracy beats latency for this product,
and the measured headroom is real: qwen3.8-27b answered 20 of 20 merge cases at
a 24s median with zero truncations, so it can afford to think considerably
harder. Raising thinking only works if the budget rises with it, which is why
``max_tokens`` here is derived from measured reasoning spend rather than chosen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX = "max"
BRIEF = "brief"
OFF = "off"

# Prompt-embedded controls, keyed by the family substring in the model id.
# Substring matching is deliberate: LM Studio ids carry publisher prefixes and
# quantization suffixes, and the family is the part that decides the syntax.
GEMMA_THINK = "<|think|>"
QWEN_THINK = "/think"
QWEN_NO_THINK = "/no_think"

_FAMILY_CONTROLS = {
    "gemma": {
        MAX: GEMMA_THINK + " Think carefully and completely before answering.",
        BRIEF: GEMMA_THINK + " Use minimal reasoning. Answer directly after a short check.",
        OFF: "",
    },
    "qwen": {
        MAX: QWEN_THINK,
        BRIEF: QWEN_THINK + " Keep reasoning short.",
        OFF: QWEN_NO_THINK,
    },
}

# Models that honour reasoning_effort as a real API parameter. Kept as an
# explicit list rather than inferred, and still verified by probing, because a
# published capability is a claim about someone else's build.
NATIVE_EFFORT_FAMILIES = ("qwen3.8",)

# Headroom multiplier over measured reasoning spend. Generous on purpose: an
# over-budget call costs seconds, an under-budget one reports a truncation as a
# model failure. At a 1600-token budget one model returned truncated on 12 merge
# cases out of 12.
BUDGET_HEADROOM = 2.5
MIN_BUDGET = 4000


@dataclass
class ReasoningProfile:
    """How to make one model think as hard as it can, measured."""

    model: str
    family: str = ""
    mode: str = MAX
    prefix: str = ""
    native_effort: bool = False
    measured: dict = field(default_factory=dict)
    prompt_inflates_reasoning: Optional[bool] = None

    @property
    def max_tokens(self) -> int:
        """A budget sized from what this model actually spends thinking."""
        observed = max(
            [v for v in self.measured.values() if isinstance(v, int)] or [0]
        )
        return max(MIN_BUDGET, int(observed * BUDGET_HEADROOM))

    def apply(self, messages: list) -> list:
        """Prepend the control to the system message, or add one.

        Returns a new list. Mutating the caller's messages would let one case
        contaminate the next, and a suite that runs the same corpus twice would
        accumulate control tokens.
        """
        if not self.prefix:
            return list(messages)
        out = [dict(m) for m in messages]
        for message in out:
            if message.get("role") == "system":
                message["content"] = f"{self.prefix}\n{message.get('content', '')}"
                return out
        return [{"role": "system", "content": self.prefix}] + out

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "family": self.family,
            "mode": self.mode,
            "prefix": self.prefix,
            "native_effort": self.native_effort,
            "max_tokens": self.max_tokens,
            "measured": dict(self.measured),
            "prompt_inflates_reasoning": self.prompt_inflates_reasoning,
        }


def family_of(model: str) -> str:
    """Which control syntax this model speaks."""
    lowered = model.lower()
    for family in _FAMILY_CONTROLS:
        if family in lowered:
            return family
    return ""


def control_for(model: str, mode: str) -> str:
    """The prompt-embedded control string, or empty when the family is unknown.

    An unknown family gets no control rather than a guessed one. Injecting
    Gemma's token into a model that does not parse it puts a literal
    ``<|think|>`` in the visible output, which the oracles would then score as
    the model's mistake.
    """
    return _FAMILY_CONTROLS.get(family_of(model), {}).get(mode, "")


def supports_native_effort(model: str) -> bool:
    lowered = model.lower()
    return any(marker in lowered for marker in NATIVE_EFFORT_FAMILIES)


# A prompt with enough structure to make a reasoning model reason. A trivial
# question answers straight through on every model here, so probing with one
# would compare two zeros and conclude the control does nothing.
PROBE_PROMPT = (
    "A file has 40 lines. Lines 12 through 19 are replaced by 3 new lines, and "
    "5 lines are appended at the end. How many lines does the file have? "
    "Answer with the number only."
)


def probe(
    model: str,
    *,
    ask: Callable[..., Any],
    modes: tuple = (MAX, BRIEF, OFF),
) -> ReasoningProfile:
    """Measure what each control actually does to this model's thinking.

    ``ask(messages, max_tokens) -> Turn`` is injected so this is testable
    without a server and so a caller can point it at a specific host.

    Also measures whether a bare system prompt inflates reasoning, because it
    did on both models tested and that is the opposite of what a prompt is
    usually assumed to do.
    """
    profile = ReasoningProfile(
        model=model,
        family=family_of(model),
        native_effort=supports_native_effort(model),
    )

    baseline = _reasoning_tokens(
        ask([{"role": "user", "content": PROBE_PROMPT}], 8000)
    )
    if baseline is not None:
        profile.measured["no_system_prompt"] = baseline

    plain = _reasoning_tokens(
        ask(
            [
                {"role": "system", "content": "You answer questions."},
                {"role": "user", "content": PROBE_PROMPT},
            ],
            8000,
        )
    )
    if plain is not None:
        profile.measured["plain_system_prompt"] = plain
        if baseline is not None:
            profile.prompt_inflates_reasoning = plain > baseline

    for mode in modes:
        control = control_for(model, mode)
        if not control and mode != OFF:
            continue
        messages = [{"role": "user", "content": PROBE_PROMPT}]
        if control:
            messages = [{"role": "system", "content": control}] + messages
        spent = _reasoning_tokens(ask(messages, 8000))
        if spent is not None:
            profile.measured[mode] = spent

    profile.mode = MAX
    profile.prefix = control_for(model, MAX)
    return profile


def _reasoning_tokens(turn: Any) -> Optional[int]:
    value = getattr(turn, "reasoning_tokens", None)
    return value if isinstance(value, int) else None
