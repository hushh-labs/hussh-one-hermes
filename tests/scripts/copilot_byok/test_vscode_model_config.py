# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the VS Code LM Studio Responses configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "copilot-byok"
    / "vscode_model_config.py"
)
_SPEC = importlib.util.spec_from_file_location("vscode_model_config", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
merge_model_config = _MODULE.merge_model_config


def _vertex() -> dict:
    return {"name": "Hussh One Vertex ADC", "apiType": "chat-completions"}


def test_migrates_existing_lmstudio_model_to_responses() -> None:
    existing = [
        {
            "name": "Custom Endpoint",
            "vendor": "customendpoint",
            "apiType": "chat-completions",
            "models": [
                {
                    "id": "local-test-model",
                    "url": "http://127.0.0.1:1234/v1",
                }
            ],
        }
    ]

    merged = merge_model_config(existing, vertex_block=_vertex())

    local = merged[0]
    assert local["apiType"] == "responses"
    assert local["models"][0]["apiType"] == "responses"
    assert local["models"][0]["url"] == "http://127.0.0.1:1234/v1/responses"
    assert local["models"][0]["maxInputTokens"] == 114688
    assert local["models"][0]["maxOutputTokens"] == 16384
    assert [b["name"] for b in merged] == ["Custom Endpoint", "Hussh One Vertex ADC"]


def test_migrates_explicit_chat_endpoint_without_losing_query() -> None:
    existing = [
        {
            "name": "Local",
            "vendor": "customendpoint",
            "models": [
                {
                    "id": "local-test-model",
                    "url": "http://localhost:1234/v1/chat/completions?profile=fast",
                }
            ],
        }
    ]

    local = merge_model_config(existing, vertex_block=_vertex())[0]
    assert local["models"][0]["url"] == (
        "http://localhost:1234/v1/responses?profile=fast"
    )


def test_adds_discovery_provider_when_no_local_endpoint_exists() -> None:
    merged = merge_model_config(
        [{"name": "Remote", "vendor": "customendpoint", "apiType": "messages"}],
        vertex_block=_vertex(),
    )

    local = merged[1]
    assert local["name"] == "Hussh One LM Studio"
    assert local["apiType"] == "responses"
    assert local["url"] == "http://127.0.0.1:1234/v1"
    assert "models" not in local


def test_does_not_rewrite_remote_custom_endpoints() -> None:
    existing = [
        {
            "name": "Remote",
            "vendor": "customendpoint",
            "apiType": "chat-completions",
            "models": [{"id": "remote", "url": "https://api.example/v1"}],
        }
    ]

    merged = merge_model_config(existing, vertex_block=_vertex())
    assert merged[0] == existing[0]


def test_mixed_provider_overrides_only_the_local_model() -> None:
    existing = [
        {
            "name": "Mixed",
            "vendor": "customendpoint",
            "apiType": "chat-completions",
            "models": [
                {"id": "local", "url": "http://127.0.0.1:1234/v1"},
                {"id": "remote", "url": "https://api.example/v1"},
            ],
        }
    ]

    local = merge_model_config(existing, vertex_block=_vertex())[0]
    assert local["apiType"] == "chat-completions"
    assert local["models"][0]["apiType"] == "responses"
    assert "apiType" not in local["models"][1]
    assert local["models"][1]["url"] == "https://api.example/v1"


def test_rerun_replaces_one_generated_vertex_block_and_no_duplicate_local() -> None:
    existing = [
        {
            "name": "Hussh One LM Studio",
            "vendor": "customendpoint",
            "apiType": "responses",
            "url": "http://127.0.0.1:1234/v1",
        },
        _vertex(),
    ]

    merged = merge_model_config(existing, vertex_block=_vertex())
    assert [b["name"] for b in merged] == [
        "Hussh One LM Studio",
        "Hussh One Vertex ADC",
    ]


def test_local_catalog_limits_fit_live_context_window() -> None:
    entry = _MODULE._catalog_model_entry(
        {
            "type": "llm",
            "key": "meta/muse-glimmer",
            "display_name": "Muse Glimmer",
            "max_context_length": 131072,
        },
        existing=None,
        base_url="http://127.0.0.1:1234/v1",
    )

    assert entry is not None
    assert entry["maxInputTokens"] == 114688
    assert entry["maxOutputTokens"] == 16384
    assert entry["maxInputTokens"] + entry["maxOutputTokens"] <= 131072
