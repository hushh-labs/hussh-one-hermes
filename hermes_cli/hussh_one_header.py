# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
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
    <Display Model> · <Safe Route> · [S|A]
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
from hermes_cli.hussh_one_identity import (
    display_model_name,
    mode_token,
    resolve_runtime_identity,
)

# The horizontal rule under the header. 20 box-drawing chars renders as a clean
# full-width divider on mobile WhatsApp without wrapping.
DIVIDER = "════════════════════"

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
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
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
            Gemini 3.6 Flash · [A]
            ════════════════════

        Or "" when an override explicitly disables the header.
    """
    override = _env_or_config_override(config_prefix)
    if override is not None:
        # Explicit operator override wins verbatim (may be "").
        return override

    brand = (brand_prefix or BRAND_DISPLAY_NAME).strip()
    model_line = resolve_runtime_identity(
        model,
        provider=provider,
        base_url=base_url,
        selection_mode=is_select_mode,
    ).label
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
    r"^(?:"
    # Family-prefixed model lines (always safe to strip as a header echo).
    # Greedy to end-of-line so the whole "Gemini 3.5 Flash [A]" is consumed,
    # not just the family token.
    r"(?:Gemini|Gemma|Qwen|Claude|Anthropic|Llama|Mistral|OpenRouter|Whizbang|DeepSeek)"
    r"[^\n]*"
    r"|"
    # Bare variant tokens the model emits WITHOUT the family prefix
    # (e.g. "Opus 4.8 [A]", "Sonnet 4.6", "Flash [A]"). Guard against eating
    # real prose ("Pro tip: ...") by requiring the variant be followed by a
    # version number and/or the [S]/[A] mode token — never arbitrary words.
    r"(?:Opus|Sonnet|Haiku|Flash|Pro)\s*(?:\d[\d.\- ]*)?\s*\[[SA]\]"
    r"|(?:Opus|Sonnet|Haiku|Flash|Pro)\s+\d[\d.\-]*\s*(?:\[[SA]\])?"
    r")\s*\n?",
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
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    brand_prefix: Optional[str] = None,
    config_prefix: Optional[str] = None,
) -> str:
    """Strip any contaminated header, then prepend the canonical one."""
    header = build_whatsapp_header(
        model,
        is_select_mode=is_select_mode,
        provider=provider,
        base_url=base_url,
        brand_prefix=brand_prefix,
        config_prefix=config_prefix,
    )
    body = strip_contaminated_header(response)
    if not header:
        return body
    return header + body


def _split_leading_stacked_header(text: str) -> tuple[str, str] | None:
    """Return an existing Hussh stacked header and its body, when present.

    This is deliberately narrower than :func:`strip_contaminated_header`: it
    recognizes only a complete three-line delivery header.  The final adapter
    can therefore retain a gateway-composed `[S]` identity while removing any
    duplicate header that follows it.
    """
    content = (text or "").lstrip()
    lines = content.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    brand_line, _model_line, divider_line = lines[:3]
    if not _BRAND_LINE_RE.match(brand_line):
        return None
    if not _DIVIDER_RE.match(divider_line):
        return None
    return "".join(lines[:3]), "".join(lines[3:])


def ensure_single_whatsapp_header(
    response: str,
    model: Optional[str],
    *,
    is_select_mode: bool,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    brand_prefix: Optional[str] = None,
    config_prefix: Optional[str] = None,
) -> str:
    """Return a delivery-ready WhatsApp message with exactly one SOP header.

    Gateway replies arrive with their session-specific header already composed.
    Preserve that header (and therefore its model and `[S]`/`[A]` provenance),
    while removing any later echo.  Direct and proactive sends have no header,
    so compose the canonical one here at the final Python delivery boundary.
    """
    # An explicit operator override (including an empty "disable" value) must
    # take precedence over an earlier gateway header.  Otherwise changing the
    # config would leave stale identity text in replies already composed by a
    # long-lived gateway process.
    if _env_or_config_override(config_prefix) is not None:
        return apply_whatsapp_header(
            response,
            model,
            is_select_mode=is_select_mode,
            provider=provider,
            base_url=base_url,
            brand_prefix=brand_prefix,
            config_prefix=config_prefix,
        )

    existing = _split_leading_stacked_header(response)
    if existing is not None:
        header, body = existing
        return header + strip_contaminated_header(body)
    return apply_whatsapp_header(
        response,
        model,
        is_select_mode=is_select_mode,
        provider=provider,
        base_url=base_url,
        brand_prefix=brand_prefix,
        config_prefix=config_prefix,
    )
