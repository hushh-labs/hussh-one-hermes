"""Tests for the hussh 🤫 One canonical WhatsApp header builder.

Covers:
- stacked layout composition (brand / model+mode / divider)
- [S] Select vs [A] Auto mode token
- display-model mapping + unknown fallback
- env > config > standard override precedence (empty string disables)
- contamination stripping (no double-stamping self-echoed headers)
"""

import importlib
import os

import pytest

MOD = "hermes_cli.hussh_one_header"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WHATSAPP_REPLY_PREFIX", raising=False)
    yield


def _h():
    return importlib.import_module(MOD)


class TestComposition:
    def test_stacked_auto_mode(self):
        h = _h()
        out = h.build_whatsapp_header("gemini-3.5-flash", is_select_mode=False)
        assert out == "hussh 🤫 One\nGemini 3.5 Flash [A]\n════════════════════\n"

    def test_stacked_select_mode(self):
        h = _h()
        out = h.build_whatsapp_header("anthropic/claude-opus-4", is_select_mode=True)
        assert out == "hussh 🤫 One\nClaude Opus [S]\n════════════════════\n"

    def test_provider_prefix_stripped_in_model_label(self):
        h = _h()
        assert h.display_model_name("custom:lmstudio/qwen/qwen3.6-35b") == "Qwen 3.6 35B"

    def test_unknown_model_falls_back_to_short_id(self):
        h = _h()
        assert h.display_model_name("acme/whizbang-7b") == "whizbang-7b"

    def test_none_model_defaults_to_gemini(self):
        h = _h()
        assert h.display_model_name(None) == "Gemini 3.5 Flash"

    def test_mode_token(self):
        h = _h()
        assert h.mode_token(True) == "[S]"
        assert h.mode_token(False) == "[A]"


class TestOverridePrecedence:
    def test_env_override_wins_verbatim(self, monkeypatch):
        h = _h()
        monkeypatch.setenv("WHATSAPP_REPLY_PREFIX", "ACME\\n---\\n")
        out = h.build_whatsapp_header("gemini", is_select_mode=False,
                                      config_prefix="ignored")
        assert out == "ACME\n---\n"

    def test_empty_env_override_disables_header(self, monkeypatch):
        h = _h()
        monkeypatch.setenv("WHATSAPP_REPLY_PREFIX", "")
        out = h.build_whatsapp_header("gemini", is_select_mode=False)
        assert out == ""

    def test_config_override_when_no_env(self):
        h = _h()
        out = h.build_whatsapp_header("gemini", is_select_mode=False,
                                      config_prefix="Cfg\\n")
        assert out == "Cfg\n"

    def test_empty_config_disables_header(self):
        h = _h()
        out = h.build_whatsapp_header("gemini", is_select_mode=False,
                                      config_prefix="")
        assert out == ""


class TestContaminationStripping:
    def test_strips_self_echoed_full_header(self):
        h = _h()
        contaminated = (
            "hussh 🤫 One\nGemini 3.5 Flash [S]\n════════════════════\n"
            "Real answer here."
        )
        assert h.strip_contaminated_header(contaminated) == "Real answer here."

    def test_strips_cjk_noise(self):
        h = _h()
        assert h.strip_contaminated_header("高度 hello") == "hello"

    def test_apply_does_not_double_stamp(self):
        h = _h()
        already = "Gemma 4 [A]\n════════════════════\nBody"
        out = h.apply_whatsapp_header(already, "gemma", is_select_mode=False)
        # Exactly one header, body preserved, no leftover divider duplication.
        assert out == "hussh 🤫 One\nGemma 4 [A]\n════════════════════\nBody"

    def test_apply_with_disabled_header_returns_clean_body(self, monkeypatch):
        h = _h()
        monkeypatch.setenv("WHATSAPP_REPLY_PREFIX", "")
        out = h.apply_whatsapp_header("Just text", "gemini", is_select_mode=False)
        assert out == "Just text"
