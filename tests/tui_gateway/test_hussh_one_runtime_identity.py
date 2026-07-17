from types import SimpleNamespace

from tui_gateway.server import _session_info


def _agent():
    return SimpleNamespace(
        base_url="https://us-east5-aiplatform.googleapis.com/v1",
        model="claude-opus-4",
        provider="google-vertex-claude",
        reasoning_config=None,
        service_tier=None,
        session_id="session-identity",
    )


def test_session_info_reports_vertex_identity_for_an_explicit_selection():
    info = _session_info(
        _agent(),
        {"session_key": "session-identity", "selection_mode": "select"},
    )

    assert info["hussh_identity"] == {
        "display_model": "Claude Opus 4.8",
        "route_label": "Vertex ADC",
        "selection_mode": "select",
        "mode_token": "[S]",
        "label": "Claude Opus 4.8 · Vertex ADC · [S]",
    }


def test_session_info_keeps_automatic_vertex_routing_in_auto_mode():
    info = _session_info(_agent(), {"session_key": "session-identity"})

    assert info["hussh_identity"]["mode_token"] == "[A]"
