"""Default brand profile for this Hermes distribution."""

from __future__ import annotations

BRAND_SLUG = "hussh-one"
BRAND_DISPLAY_NAME = "🤫 Hussh One"
BRAND_WHATSAPP_REPLY_PREFIX = f"{BRAND_DISPLAY_NAME}\n"


def default_brand_config() -> dict:
    return {
        "slug": BRAND_SLUG,
        "display_name": BRAND_DISPLAY_NAME,
        "display_skin": BRAND_SLUG,
        "dashboard_theme": BRAND_SLUG,
        "whatsapp_reply_prefix": BRAND_WHATSAPP_REPLY_PREFIX,
    }


def default_display_skin() -> str:
    return BRAND_SLUG


def default_dashboard_theme() -> str:
    return BRAND_SLUG


def default_whatsapp_reply_prefix() -> str:
    return BRAND_WHATSAPP_REPLY_PREFIX
