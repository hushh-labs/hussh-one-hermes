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
    """Parse and map a raw model id dynamically to its precise name, version, and size.

    Handles standard formats (e.g. claude-opus-4-8, gemma-4-e2b, qwen3.6-35b-a3b)
    to output precise canonical names including sub-versions and sizes.
    """
    raw = (model or "").strip() or "gemini-3.5-flash"
    short = raw.rsplit("/", 1)[-1].lower()

    # 1. Gemini Family
    if "gemini" in short:
        # Extract version like 3.5
        v_match = re.search(r"gemini-(\d+(?:\.\d+)?)", short)
        v = v_match.group(1) if v_match else "3.5"
        variant = "Flash" if "flash" in short else "Pro" if "pro" in short else ""
        return f"Gemini {v} {variant}".strip()

    # 2. Claude Family
    if "claude" in short or any(w in short for w in ["opus", "sonnet", "haiku"]):
        variant = "Opus" if "opus" in short else "Sonnet" if "sonnet" in short else "Haiku" if "haiku" in short else ""
        # Extract version like 4.8 or 3.5
        v_match = re.search(r"(\d+)[-.](\d+)", short)
        if v_match:
            v = f"{v_match.group(1)}.{v_match.group(2)}"
        else:
            v_match_single = re.search(r"claude-(\d+)", short)
            v = v_match_single.group(1) if v_match_single else "4.8" if variant == "Opus" else "3.5"
        return f"Claude {variant} {v}".strip()

    # 3. Gemma Family
    if "gemma" in short:
        # Extract version like 4
        v_match = re.search(r"gemma-(\d+)", short)
        v = v_match.group(1) if v_match else "4"
        # Extract size/variant like e2b or 31b
        size_match = re.search(r"-(\d+b|e2b|a4b|a3b)(?:-|$)", short)
        size = size_match.group(1).upper() if size_match else ""
        # Clean up common tags like a4b
        if size == "E2B":
            size = "e2b"
        elif size == "A4B":
            size = "26B a4b"
        elif size == "31B":
            size = "31B"
        return f"Gemma {v} {size}".strip()

    # 4. Qwen Family
    if "qwen" in short:
        # Extract version like 3.6
        v_match = re.search(r"qwen(\d+(?:\.\d+)?)", short)
        v = v_match.group(1) if v_match else "3.6"
        # Extract size/variant like 35b-a3b or 27b
        size_match = re.search(r"-(\d+b)(?:-|$)", short)
        size = size_match.group(1).upper() if size_match else ""
        variant_match = re.search(r"-(a3b|a4b)(?:-|$)", short)
        var = variant_match.group(1) if variant_match else ""
        return f"Qwen {v} {size} {var}".replace("  ", " ").strip()

    # Fallback to normalized short id with capitalized tokens
    tokens = [t.capitalize() for t in short.replace("-", " ").split() if t]
    return "".join(tokens)


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
    r"^(?:🤫\s*Hussh One|hussh\s*🤫?\s*One|hussh One)\s*\n?", re.IGNORECASE
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
