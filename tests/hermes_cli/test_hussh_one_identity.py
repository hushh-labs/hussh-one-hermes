# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from hermes_cli.hussh_one_identity import (
    AUTO_MODE,
    SELECT_MODE,
    display_model_name,
    resolve_runtime_identity,
    selection_mode_from_override,
)
from hermes_cli.models import get_default_model_for_provider


def test_missing_model_identity_uses_current_hussh_default():
    assert display_model_name(None) == "Gemini 3.6 Flash"


def test_native_gemini_catalog_uses_current_hussh_default():
    assert get_default_model_for_provider("gemini") == "gemini-3.6-flash"


def test_vertex_identity_includes_safe_route_and_explicit_selection():
    identity = resolve_runtime_identity(
        "claude-opus-4",
        provider="google-vertex-claude",
        selection_mode=SELECT_MODE,
    )

    assert identity.display_model == "Claude Opus 4.8"
    assert identity.route_label == "Vertex ADC"
    assert identity.mode_token == "[S]"
    assert identity.label == "Claude Opus 4.8 · Vertex ADC · [S]"


def test_automatic_vertex_escalation_remains_auto():
    identity = resolve_runtime_identity(
        "claude-opus-4-8",
        provider="google-vertex-claude",
        selection_mode=AUTO_MODE,
    )

    assert identity.mode_token == "[A]"
    assert identity.label.endswith("Vertex ADC · [A]")


def test_selection_provenance_ignores_legacy_or_automatic_overrides():
    assert selection_mode_from_override(None) == AUTO_MODE
    assert selection_mode_from_override({"model": "claude-opus-4"}) == AUTO_MODE
    assert selection_mode_from_override({"selection_mode": "select"}) == SELECT_MODE
