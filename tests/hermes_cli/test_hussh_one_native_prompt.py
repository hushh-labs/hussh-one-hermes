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
    assert captured["args"] == ["/usr/bin/osascript", "-"]
    assert captured["kwargs"] == {
        "capture_output": True,
        "check": False,
        "input": captured["kwargs"]["input"],
        "text": True,
        "timeout": 120,
    }
    assert "native-value" not in str(captured["kwargs"]["input"])


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


def test_new_vault_prompt_rejects_mismatched_values_without_echoing_them(monkeypatch):
    monkeypatch.setattr(native_prompt.sys, "platform", "darwin")
    monkeypatch.setattr(
        native_prompt.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, stdout="__HUSSH_ONE_PROMPT_MISMATCH__\n", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="did not match"):
        native_prompt.prompt_for_new_vault_passphrase()


def test_recovery_disclosure_uses_stdin_not_process_arguments(monkeypatch):
    monkeypatch.setattr(native_prompt.sys, "platform", "darwin")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="confirmed\n", stderr="")

    monkeypatch.setattr(native_prompt.subprocess, "run", fake_run)

    assert native_prompt.disclose_recovery_key("HRK-ABCD-1234-EFGH-5678") is True
    assert captured["args"] == ["/usr/bin/osascript", "-"]
    assert "HRK-ABCD-1234-EFGH-5678" not in str(captured["args"])
    assert "HRK-ABCD-1234-EFGH-5678" in str(captured["kwargs"]["input"])
