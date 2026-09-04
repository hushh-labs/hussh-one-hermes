"""Regression tests for `config set` value coercion + key validation (round-3 CFG).

Covers:
- CFG-02: negative / whitespace-padded numerics coerce to int/float on
  numeric-typed keys (old code used str.isdigit() and stored strings).
- CFG-05: null/none/~ coerce to None so a nullable field can be cleared.
- CFG-04: malformed dotted keys with empty segments are rejected.
- Guard: string-typed enum keys (approvals.mode) are NOT coerced.
"""

import pytest

from hermes_cli import config as cfg


def _read(tmp_path, *path):
    """Read a nested value straight from the on-disk config.yaml."""
    import yaml
    data = yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}
    node = data
    for seg in path:
        node = node[seg]
    return node


class TestNumericCoercion:
    def test_negative_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("agent.max_turns", "-5")
        v = _read(tmp_path, "agent", "max_turns")
        assert v == -5 and isinstance(v, int)

    def test_whitespace_padded_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("agent.max_turns", " 42 ")
        v = _read(tmp_path, "agent", "max_turns")
        assert v == 42 and isinstance(v, int)

    def test_negative_float(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("agent.max_turns", "-2.5")
        v = _read(tmp_path, "agent", "max_turns")
        assert v == -2.5 and isinstance(v, float)


class TestNullCoercion:
    @pytest.mark.parametrize("token", ["null", "none", "None", "~"])
    def test_null_tokens_coerce_to_none(self, tmp_path, monkeypatch, token):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("agent.run_budget_seconds", token)
        assert _read(tmp_path, "agent", "run_budget_seconds") is None


class TestMalformedKey:
    @pytest.mark.parametrize("bad", ["agent.", ".agent", "agent..max_turns", "  "])
    def test_empty_segment_rejected(self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            cfg.set_config_value(bad, "5")
        assert exc.value.code == 1


class TestStringTypedGuardPreserved:
    def test_enum_off_stays_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("approvals.mode", "off")
        v = _read(tmp_path, "approvals", "mode")
        assert v == "off" and isinstance(v, str)  # not bool False


class TestCanonicalFallbackProviderConfig:
    def test_canonical_fallback_chain_is_recognized_and_parsed(self, tmp_path, monkeypatch, capsys):
        """The chain written by ``hermes fallback`` must also be safe to script."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        cfg.set_config_value(
            "fallback_providers",
            '[{"provider":"gemini","model":"gemini-3.5-flash"}]',
        )

        assert _read(tmp_path, "fallback_providers") == [
            {"provider": "gemini", "model": "gemini-3.5-flash"},
        ]
        assert "not a recognized config key" not in capsys.readouterr().out


class TestModelRouteIdentity:
    def test_provider_change_clears_stale_endpoint_and_transport(self, tmp_path, monkeypatch):
        """A cloud model must never inherit an old LM Studio endpoint."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg.set_config_value("model.provider", "lmstudio")
        cfg.set_config_value("model.base_url", "http://127.0.0.1:1234/v1")
        cfg.set_config_value("model.api_mode", "chat_completions")
        cfg.set_config_value("model.api_key", "local-placeholder")

        cfg.set_config_value("model.provider", "gemini")

        import yaml

        model = (yaml.safe_load((tmp_path / "config.yaml").read_text()) or {})["model"]
        assert model["provider"] == "gemini"
        assert "base_url" not in model
        assert "api_mode" not in model
        assert "api_key" not in model
