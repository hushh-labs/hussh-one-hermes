import pytest
from unittest.mock import MagicMock, patch

from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient, AsyncAnthropicAuxiliaryClient


class TestGoogleModelRuntimeNormalization:
    def test_claude_on_google_runtime_normalizes_to_vertex_claude(self):
        from agent.vertex_claude_runtime import normalize_google_model_runtime

        runtime = normalize_google_model_runtime(
            model="claude-opus-4-8",
            provider="google-vertex",
            api_key="gemini-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_mode="chat_completions",
            credential_pool=object(),
        )

        assert runtime["provider"] == "google-vertex-claude"
        assert runtime["api_mode"] == "anthropic_messages"
        assert runtime["api_key"] == "gcp-sdk"
        assert "aiplatform.googleapis.com" in runtime["base_url"]
        assert runtime["credential_pool"] is None

    def test_gemini_runtime_clears_stale_vertex_claude_state(self):
        from agent.vertex_claude_runtime import normalize_google_model_runtime

        runtime = normalize_google_model_runtime(
            model="gemini-3.5-flash",
            provider="google-vertex-claude",
            api_key="gcp-sdk",
            base_url="https://aiplatform.googleapis.com",
            api_mode="anthropic_messages",
            credential_pool=object(),
        )

        assert runtime["provider"] == "gemini"
        assert runtime["api_mode"] == "chat_completions"
        assert runtime["api_key"] == ""
        assert runtime["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
        assert runtime["credential_pool"] is None

    def test_switch_to_gemini_clears_stale_gcp_sdk_key(self):
        from agent.agent_runtime_helpers import switch_model

        agent = MagicMock()
        agent.model = "claude-opus-4-8"
        agent.provider = "google-vertex-claude"
        agent.base_url = "https://aiplatform.googleapis.com"
        agent.api_mode = "anthropic_messages"
        agent.api_key = "gcp-sdk"
        agent.client = None
        agent._anthropic_client = MagicMock()
        agent._anthropic_api_key = "gcp-sdk"
        agent._anthropic_base_url = "https://aiplatform.googleapis.com"
        agent._is_anthropic_oauth = False
        agent._config_context_length = None
        agent._client_kwargs = {}
        agent._credential_pool = object()
        agent._transport_cache = {}
        agent.context_compressor = None
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded.return_value = None
        agent._is_azure_openai_url.return_value = False
        agent._create_openai_client.return_value = MagicMock()

        switch_model(
            agent,
            "gemini-3.5-flash",
            "google-vertex-claude",
            api_key="",
            base_url="https://aiplatform.googleapis.com",
            api_mode="anthropic_messages",
        )

        assert agent.provider == "gemini"
        assert agent.api_mode == "chat_completions"
        assert agent.api_key == ""
        assert agent._client_kwargs["api_key"] == ""


class TestAuxiliaryClientVertexClaudeResolution:
    """Verify resolve_provider_client handles Vertex Claude's gcp_sdk auth type."""

    def test_vertex_claude_returns_client(self):
        """Vertex Claude should return a usable client with AnthropicAuxiliaryClient wrapping."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "test-project"
        mock_anthropic_vertex.region = "us-east5"

        with patch("agent.anthropic_adapter.build_anthropic_vertex_client",
                   return_value=mock_anthropic_vertex) as mock_builder:
            client, model = resolve_provider_client("google-vertex-claude", "")

        assert client is not None
        assert isinstance(client, AnthropicAuxiliaryClient)
        assert model == "claude-sonnet-4-6"
        assert client.api_key == "gcp-sdk"
        assert "us-east5" in client.base_url
        assert "test-project" in client.base_url
        mock_builder.assert_called_once_with(project_id=None, region=None)

    def test_vertex_claude_respects_main_runtime(self):
        """Vertex Claude should extract project_id and region from main_runtime context."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "custom-proj"
        mock_anthropic_vertex.region = "europe-west1"

        with patch("agent.anthropic_adapter.build_anthropic_vertex_client",
                   return_value=mock_anthropic_vertex) as mock_builder:
            main_rt = {"project_id": "custom-proj", "region": "europe-west1"}
            client, model = resolve_provider_client(
                "google-vertex-claude", "", main_runtime=main_rt
            )

        assert client is not None
        assert "europe-west1" in client.base_url
        assert "custom-proj" in client.base_url
        mock_builder.assert_called_once_with(
            project_id="custom-proj",
            region="europe-west1",
            base_url="https://europe-west1-aiplatform.googleapis.com/v1",
        )

    def test_vertex_claude_auxiliary_uses_global_endpoint(self):
        """Global Vertex Claude runtime should not be rewritten to a regional host."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "custom-proj"
        mock_anthropic_vertex.region = "global"

        with patch(
            "agent.anthropic_adapter.build_anthropic_vertex_client",
            return_value=mock_anthropic_vertex,
        ) as mock_builder:
            main_rt = {
                "project_id": "custom-proj",
                "region": "global",
                "base_url": "https://aiplatform.googleapis.com",
            }
            client, _ = resolve_provider_client(
                "google-vertex-claude", "", main_runtime=main_rt
            )

        assert client.base_url == (
            "https://aiplatform.googleapis.com/v1/projects/custom-proj/locations/global"
        )
        mock_builder.assert_called_once_with(
            project_id="custom-proj",
            region="global",
            base_url="https://aiplatform.googleapis.com/v1",
        )

    def test_vertex_claude_auxiliary_uses_multi_region_endpoint(self):
        """US/EU multi-region Vertex Claude hosts use Google's REP endpoint format."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "custom-proj"
        mock_anthropic_vertex.region = "us"

        with patch(
            "agent.anthropic_adapter.build_anthropic_vertex_client",
            return_value=mock_anthropic_vertex,
        ) as mock_builder:
            main_rt = {
                "project_id": "custom-proj",
                "base_url": "https://aiplatform.us.rep.googleapis.com",
            }
            client, _ = resolve_provider_client(
                "google-vertex-claude", "", main_runtime=main_rt
            )

        assert client.base_url == (
            "https://aiplatform.us.rep.googleapis.com/v1/projects/custom-proj/locations/us"
        )
        mock_builder.assert_called_once_with(
            project_id="custom-proj",
            region="us",
            base_url="https://aiplatform.us.rep.googleapis.com/v1",
        )

    def test_auto_uses_profile_aux_model_for_vertex_main_runtime(self):
        """Auto side tasks should not inherit an unavailable primary Opus SKU."""
        from agent.auxiliary_client import _resolve_auto

        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "custom-proj"
        mock_anthropic_vertex.region = "us-east5"

        with patch(
            "agent.anthropic_adapter.build_anthropic_vertex_client",
            return_value=mock_anthropic_vertex,
        ):
            client, model = _resolve_auto(
                main_runtime={
                    "provider": "google-vertex-claude",
                    "model": "claude-opus-4-8",
                    "api_key": "gcp-sdk",
                    "base_url": "https://us-east5-aiplatform.googleapis.com",
                    "api_mode": "anthropic_messages",
                    "project_id": "custom-proj",
                    "region": "us-east5",
                }
            )

        assert client is not None
        assert model == "claude-sonnet-4-6"

    def test_vertex_claude_respects_explicit_model(self):
        """When caller passes an explicit model, it should be used."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "test-project"
        mock_anthropic_vertex.region = "us-east5"

        with patch("agent.anthropic_adapter.build_anthropic_vertex_client",
                   return_value=mock_anthropic_vertex):
            _, model = resolve_provider_client("google-vertex-claude", "claude-opus-4-8")

        assert model == "claude-opus-4-8"

    def test_vertex_claude_async_mode(self):
        """Async mode should return an AsyncAnthropicAuxiliaryClient."""
        mock_anthropic_vertex = MagicMock()
        mock_anthropic_vertex.project_id = "test-project"
        mock_anthropic_vertex.region = "us-east5"

        with patch("agent.anthropic_adapter.build_anthropic_vertex_client",
                   return_value=mock_anthropic_vertex):
            client, model = resolve_provider_client("google-vertex-claude", "", async_mode=True)

        assert client is not None
        assert isinstance(client, AsyncAnthropicAuxiliaryClient)


class TestPrimaryRuntimeVertexClaude:
    def test_agent_init_builds_anthropic_vertex_client(self):
        from run_agent import AIAgent

        mock_vertex = MagicMock()
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "agent.anthropic_adapter.build_anthropic_vertex_client",
                return_value=mock_vertex,
            ) as mock_builder,
        ):
            agent = AIAgent(
                api_key="gcp-sdk",
                base_url="https://us-east5-aiplatform.googleapis.com",
                provider="google-vertex-claude",
                model="claude-opus-4-8",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        assert agent.api_mode == "anthropic_messages"
        assert agent.provider == "google-vertex-claude"
        assert agent._anthropic_client is mock_vertex
        assert agent._anthropic_api_key == "gcp-sdk"
        mock_builder.assert_called_once()
        assert mock_builder.call_args.kwargs["region"] == "us-east5"

    def test_agent_init_normalizes_stale_anthropic_vertex_runtime(self):
        from run_agent import AIAgent

        mock_vertex = MagicMock()
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.anthropic_adapter.build_anthropic_client") as mock_native,
            patch(
                "agent.anthropic_adapter.build_anthropic_vertex_client",
                return_value=mock_vertex,
            ) as mock_vertex_builder,
        ):
            agent = AIAgent(
                api_key="gcp-sdk",
                base_url="https://aiplatform.googleapis.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                model="claude-opus-4-8",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        assert agent.provider == "google-vertex-claude"
        assert agent._anthropic_client is mock_vertex
        mock_vertex_builder.assert_called_once()
        mock_native.assert_not_called()


class TestVertexClaudeRuntimeRecovery:
    def test_location_recovery_rebuilds_vertex_client(self, monkeypatch):
        from agent.vertex_claude_runtime import try_recover_vertex_claude_location

        old_client = MagicMock()
        new_client = MagicMock()
        agent = MagicMock()
        agent.provider = "google-vertex-claude"
        agent.api_mode = "anthropic_messages"
        agent.model = "claude-opus-4-8"
        agent.api_key = "gcp-sdk"
        agent.base_url = "https://aiplatform.googleapis.com"
        agent._anthropic_api_key = "gcp-sdk"
        agent._anthropic_base_url = "https://aiplatform.googleapis.com"
        agent._anthropic_client = old_client
        agent._client_kwargs = {"api_key": "stale"}
        agent._primary_runtime = {
            "provider": "google-vertex-claude",
            "api_mode": "anthropic_messages",
            "api_key": "gcp-sdk",
            "base_url": "https://aiplatform.googleapis.com",
            "anthropic_api_key": "gcp-sdk",
            "anthropic_base_url": "https://aiplatform.googleapis.com",
            "client_kwargs": {},
        }
        agent._transport_cache = {}

        class NotFound(Exception):
            status_code = 404

        error = NotFound(
            "Publisher Model projects/example/locations/global/"
            "publishers/anthropic/models/claude-opus-4-8 was not found"
        )

        monkeypatch.setattr(
            "agent.gemini_native_adapter._resolve_vertex_project",
            lambda: ("test-project", "test"),
        )
        mock_builder = MagicMock(return_value=new_client)
        monkeypatch.setattr(
            "agent.anthropic_adapter.build_anthropic_vertex_client",
            mock_builder,
        )

        recovered = try_recover_vertex_claude_location(agent, error, set())

        assert recovered is True
        old_client.close.assert_called_once()
        assert agent.provider == "google-vertex-claude"
        assert agent.base_url == "https://aiplatform.us.rep.googleapis.com"
        assert agent._anthropic_client is new_client
        assert agent._primary_runtime["region"] == "us"
        mock_builder.assert_called_once_with(
            project_id="test-project",
            region="us",
            base_url="https://aiplatform.us.rep.googleapis.com/v1",
            timeout=None,
        )
