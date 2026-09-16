#!/usr/bin/env python3
"""Maintain VS Code custom-endpoint configuration for local LM Studio.

VS Code owns the Copilot-facing request contract.  The Custom Endpoint provider
can use the OpenAI Responses API, so local LM Studio entries must explicitly
declare ``apiType: responses`` and resolve to ``/v1/responses``.  This helper
keeps that migration separate from the Vertex BYOK configuration and is safe
to run repeatedly.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any


DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _lmstudio_base_url(raw_url: Any) -> str | None:
    """Return the normalized LM Studio base URL for a local endpoint."""

    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    try:
        parsed = urlsplit(raw_url.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if host not in _LOOPBACK_HOSTS or port != 1234:
        return None
    # Use the configured origin and retain a version prefix if one was given.
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _responses_url(raw_url: Any, *, base_url: str) -> str:
    """Resolve a model URL to the explicit LM Studio Responses endpoint."""

    candidate = raw_url if isinstance(raw_url, str) and raw_url.strip() else base_url
    parsed = urlsplit(candidate.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = f"{path[:-len('/chat/completions')]}/responses"
    elif not path.endswith("/responses"):
        if not path.endswith("/v1"):
            path = f"{path}/v1" if path else "/v1"
        path = f"{path}/responses"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _is_local_provider(provider: dict[str, Any]) -> bool:
    if _lmstudio_base_url(provider.get("url")):
        return True
    return any(
        isinstance(model, dict) and _lmstudio_base_url(model.get("url"))
        for model in provider.get("models", [])
    )


def normalize_lmstudio_provider(provider: dict[str, Any]) -> bool:
    """Migrate one provider in place; return whether it is an LM Studio entry."""

    if not _is_local_provider(provider):
        return False

    provider_base = _lmstudio_base_url(provider.get("url"))
    if provider_base:
        provider["apiType"] = "responses"
        provider["url"] = provider_base

    models = [model for model in provider.get("models", []) if isinstance(model, dict)]
    local_models = [model for model in models if _lmstudio_base_url(model.get("url"))]
    # A provider made entirely of local models can safely use one provider-wide
    # default. In a mixed provider, keep the existing default and override only
    # the local model entries below so remote models do not change wire format.
    if local_models and len(local_models) == len(models) and not provider_base:
        provider["apiType"] = "responses"

    for model in models:
        if not isinstance(model, dict):
            continue
        model_base = _lmstudio_base_url(model.get("url")) or provider_base
        if model_base:
            model["apiType"] = "responses"
            model["url"] = _responses_url(model.get("url"), base_url=model_base)
    return True


def merge_model_config(
    existing: Any,
    *,
    vertex_block: dict[str, Any],
    add_lmstudio_discovery: bool = True,
) -> list[dict[str, Any]]:
    """Replace the generated Vertex block and migrate local endpoints."""

    blocks = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    merged: list[dict[str, Any]] = []
    found_local = False
    for block in blocks:
        if block.get("name") == vertex_block.get("name"):
            continue
        found_local = normalize_lmstudio_provider(block) or found_local
        merged.append(block)

    if add_lmstudio_discovery and not found_local:
        merged.append(
            {
                "name": "Hussh One LM Studio",
                "vendor": "customendpoint",
                "apiType": "responses",
                "url": DEFAULT_LMSTUDIO_BASE_URL,
                "toolCalling": True,
                "vision": True,
                "thinking": True,
                "streaming": True,
            }
        )
    merged.append(vertex_block)
    return merged


def _vertex_block(shim_port: str, master_key: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{shim_port}/v1"
    vertex_models = [
        {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash (Vertex ADC)", "maxInputTokens": 1048576, "maxOutputTokens": 65536},
        {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash (Vertex ADC)", "maxInputTokens": 1048576, "maxOutputTokens": 65536},
        {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash (Vertex ADC)", "maxInputTokens": 1048576, "maxOutputTokens": 65536},
        {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview (Vertex ADC)", "maxInputTokens": 2097152, "maxOutputTokens": 65536},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Vertex ADC)", "maxInputTokens": 1000000, "maxOutputTokens": 128000},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8 (Vertex ADC)", "maxInputTokens": 1000000, "maxOutputTokens": 128000},
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5 (Vertex ADC)", "maxInputTokens": 1000000, "maxOutputTokens": 128000},
        {"id": "claude-fable-5", "name": "Claude Fable 5 (Vertex ADC)", "maxInputTokens": 1000000, "maxOutputTokens": 128000},
    ]
    for model in vertex_models:
        model.update(
            {
                "url": url,
                "apiKey": master_key,
                "headers": {"Authorization": f"Bearer {master_key}"},
                "toolCalling": True,
                "vision": True,
                "thinking": True,
                "streaming": True,
            }
        )
    return {
        "name": "Hussh One Vertex ADC",
        "vendor": "customendpoint",
        "apiKey": master_key,
        "headers": {"Authorization": f"Bearer {master_key}"},
        "apiType": "chat-completions",
        "models": vertex_models,
    }


def update_file(target: Path, *, shim_port: str, master_key: str) -> None:
    try:
        existing = (
            json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
        )
    except (OSError, json.JSONDecodeError):
        existing = []
    merged = merge_model_config(
        existing,
        vertex_block=_vertex_block(shim_port, master_key),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--shim-port", default="8644")
    parser.add_argument("--master-key", default=os.environ.get("MASTER_KEY", ""))
    args = parser.parse_args()
    update_file(args.target, shim_port=args.shim_port, master_key=args.master_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
