# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the low-power LM Studio watchdog."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hussh-one-cron" / "lmstudio_health_watchdog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lmstudio_health_watchdog_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if request.get_method() == "GET":
            return _Response(json.dumps({"data": [{"id": "local-model"}]}).encode())
        return _Response(b'{"choices":[{"message":{"content":"pong"}}]}')


def test_scheduled_check_reads_inventory_without_inference(monkeypatch):
    module = _load_module()
    opener = _Opener()
    monkeypatch.delenv("LMSTUDIO_WATCHDOG_MODEL", raising=False)

    assert module.check(opener=opener) == (True, None)
    assert [request.get_method() for request, _ in opener.requests] == ["GET"]
    assert opener.requests[0][0].full_url.endswith("/v1/models")


def test_deep_probe_is_explicit_and_bounded(monkeypatch):
    module = _load_module()
    opener = _Opener()
    monkeypatch.setenv("LMSTUDIO_WATCHDOG_MODEL", "local-model")

    assert module.check(deep=True, opener=opener) == (True, None)
    assert [request.get_method() for request, _ in opener.requests] == ["GET", "POST"]
    payload = json.loads(opener.requests[1][0].data)
    assert payload["max_tokens"] == 1


def test_expected_model_mismatch_is_reported_without_inference(monkeypatch):
    module = _load_module()
    opener = _Opener()
    monkeypatch.setenv("LMSTUDIO_WATCHDOG_MODEL", "missing-model")

    healthy, reason = module.check(opener=opener)

    assert not healthy
    assert "missing-model" in (reason or "")
    assert [request.get_method() for request, _ in opener.requests] == ["GET"]


def test_manifest_keeps_watchdog_no_agent_and_low_frequency():
    manifest = json.loads(
        (ROOT / "scripts" / "hussh-one-cron" / "jobs.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    job = next(
        item for item in manifest["jobs"] if item["name"] == "LM Studio Health Watchdog"
    )

    assert job == {
        "name": "LM Studio Health Watchdog",
        "schedule": "every 30m",
        "script": "lmstudio_health_watchdog.py",
        "no_agent": True,
        "default_deliver": "local",
        "purpose": (
            "Low-power metadata-only reachability check; emits only on failure. "
            "Use --deep manually for an inference probe."
        ),
    }
