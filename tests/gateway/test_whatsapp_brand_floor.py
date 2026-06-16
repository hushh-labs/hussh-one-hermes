"""Tests for the WhatsApp brand-floor (idempotent proactive-send branding).

The inbound agent-reply path (gateway/run.py) composes the full stacked header.
PROACTIVE sends (send_message tool, cron auto-delivery, restart notices) call
WhatsAppAdapter.send() directly and must still carry the hussh 🤫 One brand
line — but without double-stamping a reply that already has the full header.
"""
import os
from unittest.mock import patch

from gateway.platforms.base import PlatformConfig
from gateway.platforms.whatsapp import WhatsAppAdapter


def _adapter():
    return WhatsAppAdapter(PlatformConfig(enabled=True))


def test_bare_proactive_message_gets_branded():
    a = _adapter()
    with patch.dict(os.environ, {}, clear=True):
        out = a._ensure_brand_floor("Hello from cron")
    assert out == "🤫 Hussh One\nGemini 3.5 Flash [A]\n════════════════════\nHello from cron"


def test_full_header_reply_is_not_double_stamped():
    a = _adapter()
    reply = "🤫 Hussh One\nGemini 3.5 Flash [A]\n════════════════════\nbody"
    with patch.dict(os.environ, {}, clear=True):
        out = a._ensure_brand_floor(reply)
    assert out == reply


def test_legacy_emoji_middle_brand_is_not_double_stamped():
    a = _adapter()
    legacy = "hussh 🤫 One\nold style body"
    with patch.dict(os.environ, {}, clear=True):
         out = a._ensure_brand_floor(legacy)
    assert out == "🤫 Hussh One\nGemini 3.5 Flash [A]\n════════════════════\nold style body"


def test_empty_override_disables_branding():
    a = _adapter()
    with patch.dict(os.environ, {"WHATSAPP_REPLY_PREFIX": ""}, clear=True):
        out = a._ensure_brand_floor("no brand please")
    assert out == "no brand please"


def test_branding_is_case_insensitive_on_existing_header():
    a = _adapter()
    with patch.dict(os.environ, {}, clear=True):
        out = a._ensure_brand_floor("🤫 HUSSH ONE\nshouty body")
    # Already branded (case-insensitive) -> cleaned and standard 3-line header applied.
    assert out == "🤫 Hussh One\nGemini 3.5 Flash [A]\n════════════════════\nshouty body"


def test_empty_content_passthrough():
    a = _adapter()
    with patch.dict(os.environ, {}, clear=True):
        assert a._ensure_brand_floor("") == ""


def test_proactive_floor_reflects_configured_default_model():
    """Regression (Bug 3): proactive sends must NOT hardcode Gemini.

    Previously _ensure_brand_floor passed model=None to apply_whatsapp_header,
    so every cron / send_message / restart notice claimed "Gemini 3.5 Flash [A]"
    even when the gateway default was Claude Opus. The floor now resolves the
    real configured default via _resolve_gateway_model().
    """
    a = _adapter()
    with patch.dict(os.environ, {}, clear=True):
        with patch("gateway.run._resolve_gateway_model", return_value="claude-opus-4-8"):
            out = a._ensure_brand_floor("Heartbeat from Opus turn")
    assert out == (
        "🤫 Hussh One\nClaude Opus 4.8 [A]\n════════════════════\nHeartbeat from Opus turn"
    )
