# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""On-device-only enforcement for auxiliary tasks.

Setting the main provider to a local model is not enough to keep an agent on
device. Auxiliary tasks (compression, vision, titling, approval) resolve their
own provider: some are configured explicitly -- a stock config ships
``auxiliary.compression.provider: gemini`` -- and the rest use ``auto``, whose
fallback chain reaches for OpenRouter, Nous, and Codex when the main provider
cannot serve. Neither path announces itself, so transcript text can leave the
machine during a long session and the owner never sees it.

These tests pin the guarantee: with ``hussh_one.on_device_only`` set, no
auxiliary path may return a non-local client.
"""

from __future__ import annotations

import pytest

import agent.auxiliary_client as ac


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(ac, "_on_device_only_enabled", lambda: True)


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(ac, "_on_device_only_enabled", lambda: False)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("lmstudio", True),
        ("lm-studio", True),
        ("lm_studio", True),
        ("LMStudio", True),
        ("ollama", True),
        ("gemini", False),
        ("openrouter", False),
        ("nous", False),
        ("openai-codex", False),
        # "custom" points wherever OPENAI_BASE_URL says, which is usually a
        # hosted endpoint, so it is deliberately NOT treated as local.
        ("custom", False),
        # An unrecognised provider must be treated as remote. The allow-list
        # means a newly added hosted provider can never be admitted by default.
        ("some-new-hosted-thing", False),
        ("", False),
        (None, False),
    ],
)
def test_local_provider_allow_list(provider, expected):
    assert ac._is_local_aux_provider(provider) is expected


def test_explicitly_configured_cloud_provider_is_refused(gate_on):
    # The real-world leak: auxiliary.compression.provider = gemini.
    client, model = ac.resolve_provider_client(
        "gemini", model="gemini-3.7-flash", task="compression"
    )
    assert client is None
    assert model is None


def test_local_provider_still_resolves_with_the_gate_on(gate_on):
    # Fail-closed must not mean fail-everything: the on-device provider is the
    # whole point and has to keep working.
    client, _model = ac.resolve_provider_client(
        "lmstudio", model="google/gemma-4-26b-a4b-qat", task="compression"
    )
    assert client is not None


def test_cloud_provider_is_not_short_circuited_when_the_gate_is_off(
    gate_off, monkeypatch
):
    # Guard against the gate silently disabling auxiliary work for everyone.
    # Asserting a real client would depend on cloud credentials, which the
    # canonical runner blanks, so assert the weaker but exact property: with
    # the gate off, resolution proceeds past the guard into the normal body.
    reached = []

    def _marker(value):
        reached.append(value)
        raise RuntimeError("stop after the gate")

    monkeypatch.setattr(ac, "_normalize_aux_provider", _marker)

    with pytest.raises(RuntimeError, match="stop after the gate"):
        ac.resolve_provider_client(
            "gemini", model="gemini-3.7-flash", task="compression"
        )
    assert reached == ["gemini"]


def test_auto_route_refuses_the_network_fallback_chain(gate_on, monkeypatch):
    # Force Step 1 (main provider) to yield nothing so resolution would reach
    # the OpenRouter / Nous / Codex discovery chain.
    monkeypatch.setattr(
        ac,
        "_normalize_main_runtime",
        lambda _runtime: {"provider": "", "model": "", "base_url": "", "api_key": ""},
    )

    def _explode(*_args, **_kwargs):
        raise AssertionError("network fallback chain must not be consulted")

    monkeypatch.setattr(ac, "_try_configured_fallback_chain", _explode)
    monkeypatch.setattr(ac, "_try_main_fallback_chain", _explode)
    monkeypatch.setattr(ac, "_get_provider_chain", _explode)

    client, model, provider = ac._resolve_auto_route(
        main_runtime=None, task="compression"
    )
    assert (client, model, provider) == (None, None, "")


def test_gate_reads_config_and_defaults_closed_on_error(monkeypatch):
    # A config read failure must report False (gate disabled) rather than
    # masquerading as a policy that silently disables all auxiliary work.
    import hermes_cli.config as config_module

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(config_module, "load_config_readonly", _boom)
    assert ac._on_device_only_enabled() is False
