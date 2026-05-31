from unittest.mock import MagicMock

from hermes_cli.model_switch import switch_model
from hermes_cli.providers import determine_api_mode, resolve_provider_full


def _patch_vertex_runtime(monkeypatch):
    monkeypatch.setattr(
        "agent.gemini_native_adapter._resolve_vertex_project",
        lambda: ("test-project", "test"),
    )
    monkeypatch.setattr(
        "agent.gemini_native_adapter._resolve_vertex_location",
        lambda: "us-east5",
    )
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *a, **k: None)


def test_google_vertex_claude_resolves_from_provider_profile():
    pdef = resolve_provider_full("google-vertex-claude")

    assert pdef is not None
    assert pdef.id == "google-vertex-claude"
    assert pdef.auth_type == "gcp_sdk"
    assert pdef.transport == "anthropic_messages"
    assert determine_api_mode("google-vertex-claude") == "anthropic_messages"


def test_switch_model_explicit_google_vertex_claude_provider(monkeypatch):
    _patch_vertex_runtime(monkeypatch)

    result = switch_model(
        "claude-opus-4-8",
        current_provider="gemini",
        current_model="gemini-3.5-flash",
        explicit_provider="google-vertex-claude",
    )

    assert result.success is True
    assert result.target_provider == "google-vertex-claude"
    assert result.new_model == "claude-opus-4-8"
    assert result.api_mode == "anthropic_messages"
    assert result.api_key == "gcp-sdk"


def test_switch_model_provider_qualified_slug_does_not_route_to_gemini(monkeypatch):
    _patch_vertex_runtime(monkeypatch)

    result = switch_model(
        "google-vertex-claude/claude-opus-4-8",
        current_provider="gemini",
        current_model="gemini-3.5-flash",
    )

    assert result.success is True
    assert result.target_provider == "google-vertex-claude"
    assert result.new_model == "claude-opus-4-8"
    assert result.api_mode == "anthropic_messages"


def test_opus_alias_resolves_to_highest_vertex_profile_model(monkeypatch):
    _patch_vertex_runtime(monkeypatch)

    result = switch_model(
        "opus",
        current_provider="google-vertex-claude",
        current_model="claude-sonnet-4-6",
        current_base_url="https://us-east5-aiplatform.googleapis.com",
        current_api_key="gcp-sdk",
    )

    assert result.success is True
    assert result.target_provider == "google-vertex-claude"
    assert result.new_model == "claude-opus-4-8"


def test_agent_switch_model_builds_anthropic_vertex(monkeypatch):
    from types import SimpleNamespace

    from agent.agent_runtime_helpers import switch_model as apply_switch

    mock_vertex = MagicMock()
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_vertex_client",
        lambda **kwargs: mock_vertex,
    )
    agent = SimpleNamespace(
        model="gemini-3.5-flash",
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_mode="chat_completions",
        api_key="gemini-key",
        client=MagicMock(),
        _anthropic_client=None,
        _anthropic_api_key="",
        _anthropic_base_url="",
        _is_anthropic_oauth=False,
        _config_context_length=None,
        _client_kwargs={},
        _transport_cache={},
        context_compressor=None,
        _fallback_activated=False,
        _fallback_index=0,
        _fallback_chain=[],
        _fallback_chain_base=[],
        _fallback_requested=False,
        _cached_system_prompt=None,
    )
    agent._anthropic_prompt_cache_policy = lambda **kwargs: (True, True)
    agent._ensure_lmstudio_runtime_loaded = lambda: None
    agent._create_openai_client = lambda *a, **k: MagicMock()

    apply_switch(
        agent,
        "claude-opus-4-8",
        "google-vertex-claude",
        api_key="gcp-sdk",
        base_url="https://us-east5-aiplatform.googleapis.com",
        api_mode="anthropic_messages",
    )

    assert agent._anthropic_client is mock_vertex
    assert agent._primary_runtime["provider"] == "google-vertex-claude"
    assert agent._primary_runtime["anthropic_api_key"] == "gcp-sdk"
