import pytest
from unittest.mock import MagicMock, patch

from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient, AsyncAnthropicAuxiliaryClient


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
        mock_builder.assert_called_once_with(project_id="custom-proj", region="europe-west1")

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
        mock_builder.assert_called_once_with(project_id="custom-proj", region="global")

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
        mock_builder.assert_called_once_with(project_id="custom-proj", region="us")

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
