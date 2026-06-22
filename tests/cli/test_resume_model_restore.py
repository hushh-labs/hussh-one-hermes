"""Tests for per-session model restore on resume (#hussh-one session-model).

When a chat session is resumed (dashboard Chat tab refresh, ``hermes --resume``)
without an explicit ``--model``, the session's last-used model — persisted in
``sessions.model`` — must be rehydrated instead of silently reverting to
config.yaml's default. Claude models are always re-pinned to GCP Vertex (ADC)
so resume never falls back to an Anthropic-direct OAuth token.

The logic lives in ``HermesCLI._restore_session_model`` and is invoked from both
``_init_agent`` (no preload) and ``_preload_resumed_session`` (dashboard path).
"""

from unittest.mock import patch

from cli import HermesCLI


def _make_cli(*, model_arg=None, current_model="gemini-3.5-flash",
              provider="gemini"):
    cli = HermesCLI.__new__(HermesCLI)
    cli.model = current_model
    cli.provider = provider
    cli.requested_provider = provider
    cli.api_mode = "chat_completions"
    cli.api_key = "AIzaTESTKEY"
    cli.base_url = "https://generativelanguage.googleapis.com/v1beta"
    cli._explicit_api_key = None
    cli._explicit_base_url = None
    cli._credential_pool = "sentinel"
    cli._model_arg_explicit = bool(model_arg)
    return cli


class TestRestoreSessionModel:
    def test_claude_session_restores_to_vertex_adc(self):
        cli = _make_cli()
        cli._restore_session_model({"model": "claude-opus-4-8"})
        assert cli.model == "claude-opus-4-8"
        assert cli.provider == "google-vertex-claude"
        assert cli.api_mode == "anthropic_messages"
        # ADC sentinel, NOT a leaked sk-ant OAuth token.
        assert cli.api_key == "gcp-sdk"
        assert "aiplatform.googleapis.com" in cli.base_url
        # Vertex path must clear any inherited credential pool.
        assert cli._credential_pool is None

    def test_explicit_model_arg_is_not_overridden(self):
        # User passed --model explicitly: honour it, never restore.
        cli = _make_cli(model_arg="gemini-3.5-flash")
        cli._restore_session_model({"model": "claude-opus-4-8"})
        assert cli.model == "gemini-3.5-flash"
        assert cli.provider == "gemini"

    def test_no_stored_model_is_a_noop(self):
        cli = _make_cli()
        cli._restore_session_model({"model": None})
        assert cli.model == "gemini-3.5-flash"
        cli._restore_session_model({})
        assert cli.model == "gemini-3.5-flash"
        cli._restore_session_model(None)
        assert cli.model == "gemini-3.5-flash"

    def test_same_model_is_a_noop(self):
        cli = _make_cli(current_model="claude-opus-4-8", provider="google-vertex-claude")
        before = (cli.model, cli.provider, cli.api_key)
        cli._restore_session_model({"model": "claude-opus-4-8"})
        # No change attempted because stored == current.
        assert (cli.model, cli.provider, cli.api_key) == before

    def test_non_claude_model_uses_switch_model(self):
        cli = _make_cli()

        class _SW:
            success = True
            new_model = "gpt-5.3-codex"
            target_provider = "openai"
            api_key = "sk-openai-test"
            base_url = "https://api.openai.com/v1"
            api_mode = "chat_completions"

        with patch("hermes_cli.model_switch.switch_model", return_value=_SW()):
            cli._restore_session_model({"model": "gpt-5.3-codex"})
        assert cli.model == "gpt-5.3-codex"
        assert cli.provider == "openai"
        assert cli.api_key == "sk-openai-test"

    def test_failure_is_fail_safe(self):
        # If runtime resolution blows up, keep the stored model name and never
        # raise out of the resume path.
        cli = _make_cli()
        with patch(
            "hermes_cli.hussh_one_router._vertex_claude_runtime",
            side_effect=RuntimeError("boom"),
        ):
            cli._restore_session_model({"model": "claude-opus-4-8"})
        # Fell back to honouring the stored model name.
        assert cli.model == "claude-opus-4-8"
