#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Maintain VS Code custom-endpoint configuration for local LM Studio.

VS Code owns the Copilot-facing request contract.  The Custom Endpoint provider
can use the OpenAI Responses API, so local LM Studio entries must explicitly
declare ``apiType: responses`` and resolve to ``/v1/responses``.  This helper
keeps that migration separate from the Vertex BYOK configuration and is safe
to run repeatedly.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
# VS Code requires both token-limit hints on explicit custom-endpoint models.
# LM Studio's native catalog publishes the context window but not an output
# limit, so reserve a conservative slice for generation and give the remainder
# to the input window. These are Copilot bookkeeping hints; LM Studio remains
# authoritative for the actual request limits.
DEFAULT_LMSTUDIO_CONTEXT_WINDOW = 131_072
DEFAULT_LMSTUDIO_MAX_OUTPUT_TOKENS = 16_384
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


def _lmstudio_native_root(base_url: str) -> str:
    """Return the native LM Studio API root for an OpenAI-compatible base."""

    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def fetch_lmstudio_catalog(
    base_url: str = DEFAULT_LMSTUDIO_BASE_URL,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
    opener=urllib_request.urlopen,
) -> list[dict[str, Any]]:
    """Read LM Studio's local model catalog without invoking inference."""

    root = _lmstudio_native_root(base_url)
    headers = {"User-Agent": "Hussh-One-LMStudio-Sync/1"}
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    def read(url: str) -> Any:
        req = urllib_request.Request(url, headers=headers)
        with opener(req, timeout=timeout) as response:
            return json.load(response)

    try:
        payload = read(f"{root}/api/v1/models")
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(raw_models, list):
            return [item for item in raw_models if isinstance(item, dict)]
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        pass

    # Older LM Studio builds expose only the OpenAI-compatible inventory. It
    # lacks capability metadata, but still lets Copilot show every local LLM.
    payload = read(f"{base_url.rstrip('/')}/models")
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError("LM Studio returned no model catalog")
    return [
        {"type": "llm", "key": item.get("id"), "display_name": item.get("id")}
        for item in raw_models
        if isinstance(item, dict) and item.get("id")
    ]


def _catalog_model_id(raw: dict[str, Any]) -> str:
    return str(raw.get("key") or raw.get("id") or "").strip()


def _positive_int(value: Any) -> int | None:
    """Return a positive integer metadata value, if present."""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _local_token_limits(
    raw: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[int, int]:
    """Resolve the required VS Code input/output hints for a local model.

    LM Studio exposes ``max_context_length`` but not a separate output cap.
    Preserve explicit user values, derive a missing side from the live context
    window, and keep the sum within that window as required by VS Code.
    """

    current = existing if isinstance(existing, dict) else {}
    context = (
        _positive_int(raw.get("max_context_length"))
        or _positive_int(raw.get("context_window"))
        or _positive_int(current.get("contextWindow"))
        or DEFAULT_LMSTUDIO_CONTEXT_WINDOW
    )
    explicit_output = (
        _positive_int(current.get("maxOutputTokens"))
        or _positive_int(raw.get("max_output_tokens"))
        or _positive_int(raw.get("max_completion_tokens"))
    )
    output = explicit_output or DEFAULT_LMSTUDIO_MAX_OUTPUT_TOKENS
    input_tokens = (
        _positive_int(current.get("maxInputTokens"))
        or _positive_int(raw.get("max_input_tokens"))
    )

    # The VS Code contract is maxInputTokens + maxOutputTokens <= contextWindow.
    # When one side was explicit, retain it and fit the derived side. When both
    # were absent, reserve the default output slice and use the remainder.
    if input_tokens is None:
        # Without an input hint, reserve a bounded slice of the live context
        # for generation. An explicit output hint wins, but can never consume
        # the entire context window.
        output = min(
            output,
            max(1, context - 1)
            if explicit_output is not None
            else min(DEFAULT_LMSTUDIO_MAX_OUTPUT_TOKENS, max(1, context // 4)),
        )
        input_tokens = max(1, context - output)
    elif input_tokens + output > context:
        output = max(1, context - input_tokens)
    if input_tokens + output > context:
        input_tokens = max(1, context - output)
    return input_tokens, output


def _catalog_model_entry(
    raw: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
    base_url: str,
) -> dict[str, Any] | None:
    model_id = _catalog_model_id(raw)
    if not model_id or str(raw.get("type") or "").lower() == "embedding":
        return None
    entry = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    entry.update(
        {
            "id": model_id,
            "name": str(raw.get("display_name") or entry.get("name") or model_id),
            "url": _responses_url(None, base_url=base_url),
            "apiType": "responses",
            "streaming": True,
        }
    )
    max_input_tokens, max_output_tokens = _local_token_limits(raw, existing)
    entry["maxInputTokens"] = max_input_tokens
    entry["maxOutputTokens"] = max_output_tokens
    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict):
        if "trained_for_tool_use" in capabilities:
            entry["toolCalling"] = bool(capabilities["trained_for_tool_use"])
        if "vision" in capabilities:
            entry["vision"] = bool(capabilities["vision"])
        if "reasoning" in capabilities:
            entry["thinking"] = bool(capabilities["reasoning"])
    elif existing is None:
        entry.update({"toolCalling": False, "vision": False, "thinking": False})
    return entry


def sync_lmstudio_provider(
    provider: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> bool:
    """Add/update catalog models in one configured local provider."""

    if not _is_local_provider(provider):
        return False
    provider_base = _lmstudio_base_url(provider.get("url"))
    models = [model for model in provider.get("models", []) if isinstance(model, dict)]
    if provider_base is None:
        provider_base = next(
            (
                _lmstudio_base_url(model.get("url"))
                for model in models
                if _lmstudio_base_url(model.get("url"))
            ),
            DEFAULT_LMSTUDIO_BASE_URL,
        )
    local_models = [
        model for model in models
        if _model_is_local(model, provider_base)
    ]
    existing_by_id = {
        str(model.get("id")): model
        for model in local_models
        if str(model.get("id") or "").strip()
    }
    catalog_by_id = {
        _catalog_model_id(raw): raw
        for raw in catalog
        if _catalog_model_id(raw)
    }
    synced: list[dict[str, Any]] = []
    seen: set[str] = set()
    embedding_ids = {
        _catalog_model_id(raw)
        for raw in catalog
        if _catalog_model_id(raw)
        and str(raw.get("type") or "").lower() == "embedding"
    }
    for model in models:
        model_id = str(model.get("id") or "").strip()
        if not _model_is_local(model, provider_base):
            # An explicit remote entry in a mixed provider keeps its original
            # URL and API type; the local catalog must never rewrite it.
            synced.append(model)
            if model_id:
                seen.add(model_id)
            continue
        if model_id in embedding_ids:
            continue
        raw = catalog_by_id.get(model_id)
        entry = (
            _catalog_model_entry(raw, existing=model, base_url=provider_base)
            if raw is not None
            else model
        )
        if entry is not None:
            synced.append(entry)
        if model_id:
            seen.add(model_id)
    for raw in catalog:
        model_id = _catalog_model_id(raw)
        if not model_id or model_id in seen or model_id in embedding_ids:
            continue
        entry = _catalog_model_entry(raw, existing=None, base_url=provider_base)
        if entry is not None:
            synced.append(entry)
            seen.add(model_id)
    all_local = bool(provider_base) and all(
        _model_is_local(model, provider_base) for model in models
    )
    if all_local:
        provider["apiType"] = "responses"
    # An explicit models array is authoritative in VS Code. A provider-level
    # URL switches Custom Endpoint back to automatic discovery, which can hide
    # the catalog's per-model capabilities and explicit Responses paths.
    if synced and any(_model_is_local(model, provider_base) for model in synced):
        provider.pop("url", None)
    provider["models"] = synced
    return True


def sync_lmstudio_file(
    target: Path,
    catalog: list[dict[str, Any]],
) -> tuple[bool, int]:
    """Synchronize every configured LM Studio provider in one VS Code file."""

    try:
        existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    except (OSError, json.JSONDecodeError):
        return False, 0
    if not isinstance(existing, list):
        return False, 0
    merged = copy.deepcopy(existing)
    changed = False
    local_count = 0
    for provider in merged:
        if not isinstance(provider, dict) or not _is_local_provider(provider):
            continue
        before = copy.deepcopy(provider)
        sync_lmstudio_provider(provider, catalog)
        local_count += 1
        changed = changed or provider != before
    if not changed:
        return False, local_count
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    temp = target.with_name(f".{target.name}.new")
    temp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, target)
    return True, local_count


def _is_local_provider(provider: dict[str, Any]) -> bool:
    if _lmstudio_base_url(provider.get("url")):
        return True
    return any(
        isinstance(model, dict) and _lmstudio_base_url(model.get("url"))
        for model in provider.get("models", [])
    )


def _model_is_local(model: dict[str, Any], provider_base: str | None) -> bool:
    """Return whether a model inherits or declares the loopback endpoint."""

    if _lmstudio_base_url(model.get("url")):
        return True
    return provider_base is not None and not model.get("url")


def normalize_lmstudio_provider(provider: dict[str, Any]) -> bool:
    """Migrate one provider in place; return whether it is an LM Studio entry."""

    if not _is_local_provider(provider):
        return False

    models = [model for model in provider.get("models", []) if isinstance(model, dict)]
    provider_base = _lmstudio_base_url(provider.get("url"))
    if provider_base:
        provider["apiType"] = "responses"
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
            max_input_tokens, max_output_tokens = _local_token_limits({}, model)
            model["maxInputTokens"] = max_input_tokens
            model["maxOutputTokens"] = max_output_tokens
    # Keep URL-only providers discoverable. Once explicit model entries exist,
    # omit the provider URL so VS Code uses those entries and their metadata.
    if provider_base and models and all(_lmstudio_base_url(model.get("url")) for model in models):
        provider.pop("url", None)
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
