"""hussh 🤫 One — WhatsApp reply header (single source of truth).

This module owns the canonical hussh-one WhatsApp message header so the logic
lives in the brand/overlay layer instead of being hardcoded inside the large
gateway ``run.py``. Keeping it here means:

  * upstream merges to ``run.py`` cannot silently wipe the hussh-one standard
    (``run.py`` just calls ``build_whatsapp_header``);
  * the behaviour is unit-testable in isolation;
  * the override precedence is explicit and documented in one place.

CANONICAL STANDARD (stacked layout):

    hussh 🤫 One
    <Display Model> [S|A]
    ════════════════════
    <message body>

Where the mode token is:
  * ``[S]`` Select mode — the user manually pinned the model for this session
            (``/model ...`` or a natural-language switch).
  * ``[A]`` Auto mode   — the session is running the configured default model.

OVERRIDE PRECEDENCE (highest first):
  1. ``WHATSAPP_REPLY_PREFIX`` env var — emergency/explicit override; used
     verbatim (``\\n`` escapes expanded). Honoured even if empty string
     (empty disables the header entirely).
  2. ``whatsapp.reply_prefix`` in config.yaml — same verbatim semantics.
  3. The composed hussh-one standard (this module) — the default.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from hermes_cli.brand import BRAND_DISPLAY_NAME

# The horizontal rule under the header. 20 box-drawing chars renders as a clean
# full-width divider on mobile WhatsApp without wrapping.
DIVIDER = "════════════════════"

# Friendly display names for the models hussh-one routinely runs. Unknown
# models fall back to their short id so nothing ever renders blank.
_DISPLAY_MODEL_RULES: tuple[tuple[str, str], ...] = (
    ("gemini", "Gemini 3.5 Flash"),
    ("gemma", "Gemma 4"),
    ("qwen", "Qwen 3.6 35B"),
    ("opus", "Claude Opus"),
    ("sonnet", "Claude Sonnet"),
    ("haiku", "Claude Haiku"),
)


def display_model_name(model: Optional[str]) -> str:
    """Map a raw model id (possibly ``provider/model``) to a friendly label."""
    raw = (model or "").strip() or "gemini-3.5-flash"
    short = raw.rsplit("/", 1)[-1].lower()
    for needle, label in _DISPLAY_MODEL_RULES:
        if needle in short:
            return label
    # Unknown model: present the short id as-is (never blank).
    return raw.rsplit("/", 1)[-1]


def mode_token(is_select_mode: bool) -> str:
    """``[S]`` for Select (manual model pin) or ``[A]`` for Auto (config default)."""
    return "[S]" if is_select_mode else "[A]"


def _env_or_config_override(config_prefix: Optional[str]) -> Optional[str]:
    """Return a verbatim override prefix if one is configured, else None.

    Env wins over config. Both honour the empty string as "disable header".
    ``None`` means "no override configured -> compose the standard".
    """
    env_prefix = os.getenv("WHATSAPP_REPLY_PREFIX")
    if env_prefix is not None:
        return env_prefix.replace("\\n", "\n")
    if config_prefix is not None:
        return config_prefix.replace("\\n", "\n")
    return None


def build_whatsapp_header(
    model: Optional[str],
    *,
    is_select_mode: bool,
    brand_prefix: Optional[str] = None,
    config_prefix: Optional[str] = None,
) -> str:
    """Build the canonical hussh-one WhatsApp header (trailing newline included).

    Args:
        model: raw model id for this turn (``provider/model`` accepted).
        is_select_mode: True if the user manually pinned the model this session.
        brand_prefix: brand display name; defaults to the hussh-one brand.
        config_prefix: value of ``whatsapp.reply_prefix`` from config, if any.

    Returns:
        The header string ending in a newline, e.g.::

            hussh 🤫 One
            Gemini 3.5 Flash [A]
            ════════════════════

        Or "" when an override explicitly disables the header.
    """
    override = _env_or_config_override(config_prefix)
    if override is not None:
        # Explicit operator override wins verbatim (may be "").
        return override

    brand = (brand_prefix or BRAND_DISPLAY_NAME).strip()
    model_line = f"{display_model_name(model)} {mode_token(is_select_mode)}"
    # Stacked layout: brand line, model+mode line, divider, then body.
    return f"{brand}\n{model_line}\n{DIVIDER}\n"


# --- Contamination stripping -------------------------------------------------
# LLMs occasionally echo a hallucinated header into their own output (because
# they have seen prior headers in history). Strip any leading brand line,
# model+mode line, or divider so we never double-stamp.
_BRAND_LINE_RE = re.compile(
    r"^(?:hussh\s*🤫?\s*One|hussh One)\s*\n?", re.IGNORECASE
)
_MODEL_LINE_RE = re.compile(
    r"^(?:Gemini 3\.5 Flash|Gemma 4|Qwen 3\.6 35B|Claude (?:Opus|Sonnet|Haiku))"
    r"(?:\s*\[[SA]\])?\s*\n?",
    re.IGNORECASE,
)
_DIVIDER_RE = re.compile(r"^[═=─-]{5,}\s*\n?")
# Stray CJK contamination occasionally seen leading Gemini output.
_CJK_NOISE_RE = re.compile(r"^(?:高度)\s*", re.IGNORECASE)


def strip_contaminated_header(text: str) -> str:
    """Recursively remove any self-echoed header lines from the model output."""
    cleaned = (text or "").lstrip()
    while True:
        before = len(cleaned)
        for pat in (_CJK_NOISE_RE, _BRAND_LINE_RE, _MODEL_LINE_RE, _DIVIDER_RE):
            cleaned = pat.sub("", cleaned, count=1).lstrip()
        if len(cleaned) == before:
            break
    return cleaned


def apply_whatsapp_header(
    response: str,
    model: Optional[str],
    *,
    is_select_mode: bool,
    brand_prefix: Optional[str] = None,
    config_prefix: Optional[str] = None,
) -> str:
    """Strip any contaminated header, then prepend the canonical one."""
    header = build_whatsapp_header(
        model,
        is_select_mode=is_select_mode,
        brand_prefix=brand_prefix,
        config_prefix=config_prefix,
    )
    body = strip_contaminated_header(response)
    if not header:
        return body
    return header + body
