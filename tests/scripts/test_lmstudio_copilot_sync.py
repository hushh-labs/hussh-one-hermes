"""Regression tests for metadata-only LM Studio → VS Code synchronization."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "copilot-byok" / "vscode_model_config.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("vscode_model_config_sync_test", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_catalog_fetch_is_metadata_only() -> None:
    helper = _load_helper()
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return _Response(
            json.dumps(
                {
                    "models": [
                        {"type": "llm", "key": "local-test-model"},
                        {"type": "embedding", "key": "embedding-model"},
                    ]
                }
            ).encode()
        )

    catalog = helper.fetch_lmstudio_catalog(opener=opener)

    assert [item["key"] for item in catalog] == ["local-test-model", "embedding-model"]
    assert requests[0][0].get_method() == "GET"
    assert requests[0][0].full_url.endswith("/api/v1/models")
    assert requests[0][1] == 5.0


def test_catalog_fetch_falls_back_to_openai_inventory() -> None:
    helper = _load_helper()
    requests = []

    def opener(request, *, timeout):
        requests.append(request.full_url)
        if request.full_url.endswith("/api/v1/models"):
            raise OSError("native endpoint unavailable")
        return _Response(json.dumps({"data": [{"id": "fallback-model"}]}).encode())

    catalog = helper.fetch_lmstudio_catalog(opener=opener)

    assert [item["key"] for item in catalog] == ["fallback-model"]
    assert requests == [
        "http://127.0.0.1:1234/api/v1/models",
        "http://127.0.0.1:1234/v1/models",
    ]


def test_sync_adds_new_models_and_preserves_remote_entries(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "chatLanguageModels.json"
    target.write_text(
        json.dumps(
            [
                {
                    "name": "Local",
                    "vendor": "customendpoint",
                    "apiType": "chat-completions",
                    "url": "http://127.0.0.1:1234/v1",
                    "models": [
                        {
                            "id": "existing-model",
                            "name": "Pinned name",
                            "url": "http://127.0.0.1:1234/v1",
                            "maxInputTokens": 1234,
                        },
                        {
                            "id": "ignored-embedding",
                            "url": "http://127.0.0.1:1234/v1",
                        },
                    ],
                },
                {
                    "name": "Remote",
                    "vendor": "customendpoint",
                    "apiType": "chat-completions",
                    "models": [{"id": "remote", "url": "https://api.example/v1"}],
                },
            ]
        ),
        encoding="utf-8",
    )

    changed, provider_count = helper.sync_lmstudio_file(
        target,
        [
            {
                "type": "llm",
                "key": "existing-model",
                "display_name": "Catalog name",
                "max_context_length": 9999,
                "capabilities": {
                    "vision": True,
                    "trained_for_tool_use": True,
                    "reasoning": {"allowed_options": ["low", "high"]},
                },
            },
            {"type": "llm", "key": "new-model", "display_name": "New local model"},
            {"type": "embedding", "key": "ignored-embedding"},
        ],
    )

    assert changed
    assert provider_count == 1
    data = json.loads(target.read_text(encoding="utf-8"))
    local, remote = data
    assert local["apiType"] == "responses"
    assert "url" not in local
    assert [model["id"] for model in local["models"]] == [
        "existing-model",
        "new-model",
    ]
    existing, new = local["models"]
    assert existing["name"] == "Catalog name"
    assert existing["maxInputTokens"] == 1234
    assert existing["url"] == "http://127.0.0.1:1234/v1/responses"
    assert existing["toolCalling"] is True
    assert existing["vision"] is True
    assert existing["thinking"] is True
    assert new["name"] == "New local model"
    assert new["url"] == "http://127.0.0.1:1234/v1/responses"
    assert remote["apiType"] == "chat-completions"
    assert remote["models"][0]["url"] == "https://api.example/v1"


def test_sync_is_idempotent_after_catalog_is_applied(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "chatLanguageModels.json"
    target.write_text(
        json.dumps(
            [
                {
                    "name": "Local",
                    "vendor": "customendpoint",
                    "apiType": "responses",
                    "url": "http://127.0.0.1:1234/v1",
                    "models": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    catalog = [{"type": "llm", "key": "local-test-model"}]

    first = helper.sync_lmstudio_file(target, catalog)
    before = target.read_bytes()
    second = helper.sync_lmstudio_file(target, catalog)

    assert first == (True, 1)
    assert second == (False, 1)
    assert target.read_bytes() == before


def test_sync_preserves_explicit_remote_models_in_a_mixed_provider(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "chatLanguageModels.json"
    target.write_text(
        json.dumps(
            [
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
        ),
        encoding="utf-8",
    )

    changed, provider_count = helper.sync_lmstudio_file(
        target,
        [
            {"type": "llm", "key": "local", "display_name": "Local"},
            {"type": "llm", "key": "new-local", "display_name": "New local"},
        ],
    )

    assert changed and provider_count == 1
    provider = json.loads(target.read_text(encoding="utf-8"))[0]
    assert provider["apiType"] == "chat-completions"
    assert [m["id"] for m in provider["models"]] == ["local", "remote", "new-local"]
    assert provider["models"][0]["apiType"] == "responses"
    assert provider["models"][0]["url"].endswith("/v1/responses")
    assert provider["models"][1] == {"id": "remote", "url": "https://api.example/v1"}


def test_setup_migration_omits_discovery_url_when_models_are_explicit() -> None:
    helper = _load_helper()
    migrated = helper.merge_model_config(
        [
            {
                "name": "Local",
                "vendor": "customendpoint",
                "apiType": "chat-completions",
                "url": "http://127.0.0.1:1234/v1",
                "models": [
                    {"id": "local", "url": "http://127.0.0.1:1234/v1"},
                ],
            }
        ],
        vertex_block={"name": "Vertex", "apiType": "chat-completions"},
    )
    local = migrated[0]
    assert "url" not in local
    assert local["models"][0]["url"].endswith("/v1/responses")
