"""Google Vertex AI Claude provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


class GoogleVertexClaudeProfile(ProviderProfile):
    """Vertex Claude uses Google's SDK credential chain, not a REST model API."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Vertex Claude has no OpenAI-style /models endpoint."""
        return None


google_vertex_claude = GoogleVertexClaudeProfile(
    name="google-vertex-claude",
    aliases=("vertex-claude", "gcp-claude", "google-claude"),
    api_mode="anthropic_messages",
    display_name="Google Vertex AI Claude",
    description="Google Vertex AI Claude via Application Default Credentials.",
    signup_url="https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude/use-claude",
    env_vars=(
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ),
    base_url="https://us-east5-aiplatform.googleapis.com",
    auth_type="gcp_sdk",
    supports_health_check=False,
    default_aux_model="claude-sonnet-4-6",
    fallback_models=(
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)

register_provider(google_vertex_claude)
