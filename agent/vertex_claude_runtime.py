"""Runtime guards for Anthropic Claude on Google Vertex AI."""

from __future__ import annotations

import logging
from typing import Any, MutableSet

from hermes_cli.vertex_ai_locations import (
    infer_vertex_location_from_base_url,
    vertex_aiplatform_base_url,
)
from hermes_cli.vertex_claude_access import candidate_locations_for_vertex_claude

logger = logging.getLogger(__name__)


def looks_like_vertex_claude_runtime(
    provider: str | None,
    api_key: Any = None,
    base_url: str | None = None,
    *,
    api_mode: str | None = "anthropic_messages",
) -> bool:
    """Return True when an Anthropic Messages runtime should use Vertex."""
    if api_mode and api_mode != "anthropic_messages":
        return False

    normalized_provider = (provider or "").strip().lower()
    if normalized_provider == "google-vertex-claude":
        return True

    try:
        from agent.anthropic_adapter import provider_uses_anthropic_vertex

        if provider_uses_anthropic_vertex(normalized_provider):
            return True
    except Exception:
        pass

    inferred_location = infer_vertex_location_from_base_url(base_url)
    if inferred_location and api_key == "gcp-sdk":
        return True
    return False


def vertex_claude_model_not_found(error: BaseException) -> bool:
    """Return True for Vertex Claude model-not-found/access 404s."""
    status = getattr(error, "status_code", None)
    if status != 404:
        return False
    text = str(error)
    return (
        "publishers/anthropic/models/" in text
        and ("not found" in text.lower() or "does not have access" in text.lower())
    )


def _set_primary_runtime_vertex_location(
    agent: Any,
    *,
    project_id: str,
    location: str,
    base_url: str,
) -> None:
    runtime = getattr(agent, "_primary_runtime", None)
    if not isinstance(runtime, dict):
        return
    runtime["provider"] = "google-vertex-claude"
    runtime["api_mode"] = "anthropic_messages"
    runtime["api_key"] = "gcp-sdk"
    runtime["base_url"] = base_url
    runtime["anthropic_api_key"] = "gcp-sdk"
    runtime["anthropic_base_url"] = base_url
    runtime["project_id"] = project_id
    runtime["region"] = location
    client_kwargs = runtime.get("client_kwargs")
    if isinstance(client_kwargs, dict):
        client_kwargs.clear()


def try_recover_vertex_claude_location(
    agent: Any,
    error: BaseException,
    attempted_locations: MutableSet[str],
) -> bool:
    """Rebuild Vertex Claude on another supported location after a 404.

    Vertex Claude can return a 404 for a region even when the same model is
    usable from another global/multi-region endpoint. Keep the recovery inside
    the Vertex provider instead of falling through to an unrelated fallback
    model, and normalize stale runtimes back onto the `google-vertex-claude`
    provider when their base URL/API key prove they are Vertex-backed.
    """
    if not vertex_claude_model_not_found(error):
        return False
    if not looks_like_vertex_claude_runtime(
        getattr(agent, "provider", None),
        getattr(agent, "_anthropic_api_key", None) or getattr(agent, "api_key", None),
        getattr(agent, "_anthropic_base_url", None) or getattr(agent, "base_url", None),
        api_mode=getattr(agent, "api_mode", None),
    ):
        return False

    model = getattr(agent, "model", "") or ""
    current_location = (
        infer_vertex_location_from_base_url(getattr(agent, "_anthropic_base_url", None))
        or infer_vertex_location_from_base_url(getattr(agent, "base_url", None))
        or (getattr(agent, "_primary_runtime", {}) or {}).get("region", "")
        or "global"
    )
    if current_location:
        attempted_locations.add(str(current_location).lower())

    try:
        from agent.anthropic_adapter import build_anthropic_vertex_client
        from agent.gemini_native_adapter import _resolve_vertex_project
        from hermes_cli.timeouts import get_provider_request_timeout

        project_id, _project_source = _resolve_vertex_project()
    except Exception as exc:
        logger.warning("Vertex Claude location recovery unavailable: %s", exc)
        return False

    for location in candidate_locations_for_vertex_claude(model, current_location):
        location = location.lower()
        if location in attempted_locations:
            continue
        attempted_locations.add(location)
        base_url = vertex_aiplatform_base_url(location)
        try:
            try:
                old_client = getattr(agent, "_anthropic_client", None)
                if old_client is not None:
                    old_client.close()
            except Exception:
                pass
            agent._anthropic_client = build_anthropic_vertex_client(
                project_id=project_id,
                region=location,
                base_url=vertex_aiplatform_base_url(location, with_version=True),
                timeout=get_provider_request_timeout("google-vertex-claude", model),
            )
            agent.provider = "google-vertex-claude"
            agent.api_mode = "anthropic_messages"
            agent.api_key = "gcp-sdk"
            agent.base_url = base_url
            agent._anthropic_api_key = "gcp-sdk"
            agent._anthropic_base_url = base_url
            agent._is_anthropic_oauth = False
            agent.client = None
            agent._client_kwargs = {}
            if hasattr(agent, "_transport_cache"):
                agent._transport_cache.clear()
            _set_primary_runtime_vertex_location(
                agent,
                project_id=project_id,
                location=location,
                base_url=base_url,
            )
            logger.info(
                "Recovered Vertex Claude runtime by switching location %s -> %s",
                current_location,
                location,
            )
            return True
        except Exception as exc:
            logger.warning(
                "Vertex Claude location recovery failed for %s: %s",
                location,
                exc,
            )
    return False
