from __future__ import annotations

import subprocess

import pytest

from hermes_cli.hussh_one_pkm import native_prompt


def test_native_prompt_returns_masked_response_without_logging(monkeypatch):
    monkeypatch.setattr(native_prompt.sys, "platform", "darwin")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="native-value\n", stderr="")

    monkeypatch.setattr(native_prompt.subprocess, "run", fake_run)

    assert native_prompt.prompt_for_vault_passphrase() == "native-value"
    assert captured["kwargs"] == {
        "capture_output": True,
        "check": False,
        "text": True,
        "timeout": 120,
    }


def test_native_prompt_returns_none_when_cancelled(monkeypatch):
    monkeypatch.setattr(native_prompt.sys, "platform", "darwin")
    monkeypatch.setattr(
        native_prompt.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, stdout=f"{native_prompt._CANCELLED}\n", stderr=""
        ),
    )

    assert native_prompt.prompt_for_vault_passphrase() is None


def test_native_prompt_fails_closed_outside_macos(monkeypatch):
    monkeypatch.setattr(native_prompt.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="macOS only"):
        native_prompt.prompt_for_vault_passphrase()
