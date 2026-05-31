"""Runtime access checks for Anthropic Claude models on Vertex AI."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from hermes_cli.vertex_ai_locations import (
    infer_vertex_location_from_base_url,
    vertex_aiplatform_base_url,
)


@dataclass(frozen=True)
class VertexClaudeAccessCheck:
    ok: bool
    message: str = ""
    location: str = ""
    base_url: str = ""


def _append_unique(values: list[str], value: str) -> None:
    value = (value or "").strip().lower()
    if value and value not in values:
        values.append(value)


def candidate_locations_for_vertex_claude(model: str, preferred: str = "") -> list[str]:
    """Return endpoint locations worth trying for a Vertex Claude model."""
    normalized = (model or "").strip().lower()
    preferred = (preferred or "").strip().lower()
    candidates: list[str] = []

    if "opus-4-8" in normalized:
        # Google lists Opus 4.8 on global plus US/EU multi-region endpoints.
        supported = ["global", "us", "eu"]
    elif "sonnet-4-6" in normalized:
        supported = ["global", "us-east5", "europe-west1", "asia-southeast1"]
    else:
        supported = ["global", "us-east5", "europe-west1", "asia-southeast1"]

    if preferred in supported:
        _append_unique(candidates, preferred)
    for loc in supported:
        _append_unique(candidates, loc)
    if preferred and preferred not in supported:
        _append_unique(candidates, preferred)
    return candidates


def _vertex_claude_preflight_enabled() -> bool:
    raw = os.environ.get("HERMES_VERTEX_CLAUDE_PREFLIGHT", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def check_vertex_claude_model_access(
    model: str,
    *,
    base_url: str = "",
    timeout: float = 12.0,
) -> VertexClaudeAccessCheck:
    """Make a minimal live request to verify this project can use *model*.

    Vertex AI does not expose an OpenAI-style models listing for Anthropic
    Claude. A one-token request is the only reliable local check that catches
    "model exists but this project is not enabled for it" before the session is
    switched into a broken runtime.
    """
    if not _vertex_claude_preflight_enabled():
        return VertexClaudeAccessCheck(ok=True)

    try:
        from agent.gemini_native_adapter import (
            _resolve_vertex_location,
            _resolve_vertex_project,
        )

        project_id, _ = _resolve_vertex_project()
        configured_location = _resolve_vertex_location()
    except Exception as exc:
        return VertexClaudeAccessCheck(
            ok=False,
            message=f"Could not resolve Vertex AI project/location: {exc}",
        )

    preferred = (
        infer_vertex_location_from_base_url(base_url)
        or configured_location
        or os.environ.get("GOOGLE_CLOUD_LOCATION", "")
        or "global"
    )
    tried: list[str] = []
    last_error = ""

    for location in candidate_locations_for_vertex_claude(model, preferred):
        tried.append(location)
        try:
            from run_agent import AIAgent

            agent = AIAgent(
                model=model,
                provider="google-vertex-claude",
                base_url=vertex_aiplatform_base_url(location),
                api_key="gcp-sdk",
                api_mode="anthropic_messages",
                quiet_mode=True,
                max_iterations=1,
                max_tokens=4096,
                enabled_toolsets=[],
                fallback_model={},
                skip_context_files=True,
                skip_memory=True,
                platform="vertex-preflight",
            )
            with agent._anthropic_client.messages.stream(
                model=model,
                max_tokens=4096,
                system=agent._build_system_prompt(None),
                messages=[{"role": "user", "content": "Reply with ok."}],
            ) as stream:
                for _ in stream.text_stream:
                    break
            return VertexClaudeAccessCheck(
                ok=True,
                location=location,
                base_url=vertex_aiplatform_base_url(location),
            )
        except Exception as exc:
            raw = str(exc)
            raw = re.sub(r"projects/[^/`'\"\s]+", "projects/<project>", raw)
            if project_id and len(str(project_id)) > 3:
                raw = raw.replace(str(project_id), "<project>")
            last_error = f"{type(exc).__name__}: {raw[:240]}"

    return VertexClaudeAccessCheck(
        ok=False,
        message=(
            f"Google Vertex AI Claude could not access `{model}` for this project. "
            f"Tried locations: {', '.join(tried)}. "
            "Google may list the model as available, but Vertex returned not-found "
            "or access-denied for the active project. Enable the model in Model "
            "Garden and accept the Anthropic partner terms, or switch to "
            "`claude-sonnet-4-6`. "
            f"Last error: {last_error}"
        ),
    )
