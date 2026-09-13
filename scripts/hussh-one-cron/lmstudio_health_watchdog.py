#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Low-power LM Studio reachability watchdog.

The scheduled path performs only a bounded GET of LM Studio's model
inventory. It never invokes a model, so a background health tick cannot load
or generate with an on-device model. Use ``--deep`` only for an intentional
manual inference probe.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_TIMEOUT_SECONDS = 3.0


def _base_url() -> str:
    return (os.environ.get("LMSTUDIO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _timeout_seconds() -> float:
    try:
        configured = os.environ.get(
            "LMSTUDIO_WATCHDOG_TIMEOUT", DEFAULT_TIMEOUT_SECONDS
        )
        return max(0.5, float(configured))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _expected_model() -> str | None:
    value = os.environ.get("LMSTUDIO_WATCHDOG_MODEL", "").strip()
    return value or None


def _read_models(*, opener=urllib.request.urlopen) -> list[str]:
    request = urllib.request.Request(f"{_base_url()}/models", method="GET")
    with opener(request, timeout=_timeout_seconds()) as response:
        payload = json.load(response)
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("LM Studio returned no model inventory")
    model_ids = [
        str(item.get("id"))
        for item in models
        if isinstance(item, dict) and item.get("id")
    ]
    if not model_ids:
        raise RuntimeError("LM Studio has no loaded models")
    return model_ids


def _deep_probe(model: str, *, opener=urllib.request.urlopen) -> None:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{_base_url()}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=_timeout_seconds()) as response:
        json.load(response)


def check(
    *, deep: bool = False, opener=urllib.request.urlopen
) -> tuple[bool, str | None]:
    """Return ``(healthy, reason)`` without inference unless ``deep`` is set."""
    try:
        models = _read_models(opener=opener)
        expected = _expected_model()
        if expected and expected not in models:
            return False, f"expected model {expected!r} is not loaded (available: {models})"
        if deep:
            _deep_probe(expected or models[0], opener=opener)
        return True, None
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        return False, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="also issue a one-token inference probe; never used by the scheduled job",
    )
    args = parser.parse_args(argv)
    healthy, reason = check(deep=args.deep)
    if healthy:
        return 0

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print("*🤫 Hussh One* · *LM Studio Watchdog*")
    print("=" * 22)
    print(f"\n• Health check failed at {timestamp}")
    print(f"• {reason or 'LM Studio did not answer'}")
    print("• Cron jobs using the on-device model may time out")
    return 1


if __name__ == "__main__":
    sys.exit(main())
