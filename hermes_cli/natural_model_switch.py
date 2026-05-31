"""Deterministic natural-language model switch detection.

This intentionally does not use an LLM.  It only accepts short, direct,
user-authored switch requests so quoted webpages, tool output, copied
instructions, or prompt-injection text stay ordinary chat content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NaturalModelSwitchIntent:
    """Parsed natural-language switch request."""

    model: str
    provider: str
    raw_args: str


_DIRECT_SWITCH_RE = re.compile(
    r"^\s*(?:"
    r"(?:hey\s+)?(?:hermes|hussh(?:\s+one)?)[,\s]+|"
    r"please\s+|"
    r"(?:can|could|would|will)\s+you\s+|"
    r"let'?s\s+|"
    r"i\s+(?:want|need|would\s+like)\s+(?:you\s+)?(?:to\s+)?"
    r")?"
    r"(?:switch|change|set|move|route|use)\b",
    re.IGNORECASE,
)

_QUESTION_HELP_RE = re.compile(
    r"^\s*(?:how|what|why|when|where|who)\b|"
    r"\b(?:how\s+do\s+i|tell\s+me\s+how|show\s+me\s+how)\b",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|no)\s+"
    r"(?:switch|change|set|move|route|use)\b",
    re.IGNORECASE,
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

_PROVIDER_VERTEX_RE = re.compile(
    r"\b(?:google[-\s]*vertex(?:[-\s]*(?:claude|anthropic))?|"
    r"vertex(?:[-\s]*(?:claude|anthropic))?|"
    r"anthropic\s+vertex|gcp(?:[-\s]*sdk)?)\b",
    re.IGNORECASE,
)

_DANGEROUS_SHAPES_RE = re.compile(
    r"https?://|www\.|```|`|^>|^\s*(?:[-*]|\d+[.)])\s+",
    re.IGNORECASE | re.MULTILINE,
)


def _safe_direct_user_text(text: str) -> str:
    if not isinstance(text, str):
        return ""

    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return ""

    # Natural switching is deliberately for one short, direct utterance.  Longer
    # or structured text is more likely to be pasted context or injected content.
    if len(stripped) > 180 or stripped.count("\n") > 1:
        return ""

    if _DANGEROUS_SHAPES_RE.search(stripped):
        return ""

    if _QUESTION_HELP_RE.search(stripped):
        return ""

    if _NEGATION_RE.search(stripped):
        return ""

    if _INJECTION_CONTEXT_RE.search(stripped):
        return ""

    if not _DIRECT_SWITCH_RE.search(stripped):
        return ""

    return stripped


def _canonical_model(text: str) -> str:
    normalized = (
        text.lower()
        .replace("_", "-")
        .replace("point", ".")
        .replace("four", "4")
        .replace("eight", "8")
        .replace("six", "6")
        .replace("five", "5")
    )

    if re.search(r"\b(?:claude[-\s]*)?opus(?:[-\s]*4(?:[.\-\s]*8)?)?\b", normalized):
        return "claude-opus-4-8"

    if re.search(r"\b(?:claude[-\s]*)?sonnet(?:[-\s]*4(?:[.\-\s]*6)?)?\b", normalized):
        return "claude-sonnet-4-6"

    if re.search(r"\b(?:claude[-\s]*)?haiku(?:[-\s]*4(?:[.\-\s]*5)?)?\b", normalized):
        return "claude-haiku-4-5"

    if re.search(r"\bgemini(?:[-\s]*3(?:[.\-\s]*5)?)?[-\s]*flash\b", normalized):
        return "gemini-3.5-flash"

    if re.search(r"\bgemini[-\s]*3(?:[.\-\s]*5)\b", normalized):
        return "gemini-3.5-flash"

    return ""


def parse_natural_model_switch(text: str) -> NaturalModelSwitchIntent | None:
    """Return a session-scoped model-switch intent for clear user requests.

    Supported examples:
      - "switch to opus 4.8"
      - "can you use sonnet 4.6 on vertex?"
      - "switch back to gemini 3.5 flash"

    Rejected examples include quoted instructions, URLs, code blocks, lists,
    long pasted text, negations, and help questions.
    """
    safe_text = _safe_direct_user_text(text)
    if not safe_text:
        return None

    model = _canonical_model(safe_text)
    if not model:
        return None

    if model.startswith("claude-"):
        provider = "google-vertex-claude"
    elif model.startswith("gemini-"):
        provider = "gemini"
    else:
        return None

    # Keep explicit Vertex wording accepted, but do not require it for Claude
    # 4.x because this fork wires those Claude IDs through the Vertex adapter.
    if _PROVIDER_VERTEX_RE.search(safe_text) and not model.startswith("claude-"):
        return None

    raw_args = f"{model} --provider {provider}"
    return NaturalModelSwitchIntent(model=model, provider=provider, raw_args=raw_args)

