"""Vertex AI endpoint helpers shared by Gemini and Claude providers."""

from __future__ import annotations

from urllib.parse import urlparse


def vertex_aiplatform_base_url(location: str, *, with_version: bool = False) -> str:
    """Return the Vertex AI API base URL for a region or multi-region."""
    loc = (location or "").strip().lower() or "global"
    if loc == "global":
        base = "https://aiplatform.googleapis.com"
    elif loc == "us":
        base = "https://aiplatform.us.rep.googleapis.com"
    elif loc == "eu":
        base = "https://aiplatform.eu.rep.googleapis.com"
    else:
        base = f"https://{loc}-aiplatform.googleapis.com"
    return f"{base}/v1" if with_version else base


def infer_vertex_location_from_base_url(base_url: str | None) -> str:
    """Extract a Vertex location from standard regional/global hostnames."""
    if not base_url:
        return ""
    try:
        hostname = (urlparse(str(base_url)).hostname or "").lower()
    except Exception:
        return ""
    if hostname == "aiplatform.googleapis.com":
        return "global"
    if hostname == "aiplatform.us.rep.googleapis.com":
        return "us"
    if hostname == "aiplatform.eu.rep.googleapis.com":
        return "eu"
    suffix = "-aiplatform.googleapis.com"
    if hostname.endswith(suffix):
        return hostname[: -len(suffix)] or ""
    return ""
