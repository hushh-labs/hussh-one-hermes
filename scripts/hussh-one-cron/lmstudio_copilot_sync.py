#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Synchronize discovered LM Studio models into VS Code Copilot config.

This is a metadata-only maintenance job. It never sends a prompt to a model,
loads/unloads a model, or changes Hermes' provider choice. Copilot points at the
same loopback LM Studio Responses endpoint that Hermes uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _helper_module():
    helper_path = _hermes_home() / "scripts" / "vscode_model_config.py"
    if not helper_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("hussh_one_vscode_model_config", helper_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_paths() -> list[Path]:
    home = Path.home()
    return [
        home / "Library/Application Support/Code - Insiders/User/chatLanguageModels.json",
        home / "Library/Application Support/Code/User/chatLanguageModels.json",
        home / ".config/Code - Insiders/User/chatLanguageModels.json",
        home / ".config/Code/User/chatLanguageModels.json",
    ]


def run() -> int:
    helper = _helper_module()
    if helper is None:
        # The Copilot bridge has not been installed on this machine. This job
        # remains harmless and silent until setup materializes the helper.
        return 0

    configured = []
    for path in _config_paths():
        try:
            data = helper.json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, helper.json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        providers = [
            provider for provider in data
            if isinstance(provider, dict) and helper._is_local_provider(provider)
        ]
        if providers:
            configured.append((path, providers))
    if not configured:
        # No local Copilot provider means the user has not enabled this lane.
        return 0

    base_url = next(
        (
            helper._lmstudio_base_url(provider.get("url"))
            or next(
                (
                    helper._lmstudio_base_url(model.get("url"))
                    for model in provider.get("models", [])
                    if isinstance(model, dict) and helper._lmstudio_base_url(model.get("url"))
                ),
                None,
            )
            for _, providers in configured
            for provider in providers
        ),
        helper.DEFAULT_LMSTUDIO_BASE_URL,
    )
    try:
        catalog = helper.fetch_lmstudio_catalog(
            base_url,
            api_key=os.environ.get("LM_API_KEY"),
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001 - LM Studio may be offline
        print(f"LM Studio Copilot sync skipped: {type(exc).__name__}", file=sys.stderr)
        return 0

    changed = 0
    discovered = 0
    for path, _ in configured:
        was_changed, provider_count = helper.sync_lmstudio_file(path, catalog)
        if was_changed:
            changed += 1
        discovered = max(discovered, provider_count)
    if changed:
        print(
            f"LM Studio Copilot sync: {len(catalog)} models across "
            f"{discovered} provider(s), updated {changed} VS Code profile(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
