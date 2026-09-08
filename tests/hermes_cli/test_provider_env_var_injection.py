"""Provider env-var injection must survive a partial registry.

``_inject_profile_env_vars`` used to latch a module flag BEFORE doing its work,
inside a bare ``except: pass``. Because ``hermes_cli.config`` is imported during
provider-package initialisation on some entry paths, the first call could
legitimately see a partial registry — and the latch then made that partial view
permanent for the life of the process, silently.

The symptom was an order-dependent test failure (``DEEPINFRA_API_KEY`` missing
from ``OPTIONAL_ENV_VARS`` when ``agent.model_metadata`` was imported first),
but the defect was real in production: a provider registered after config import
never appeared in ``hermes setup``, and — because
``tools/environments/local.py`` reads this dict to decide which env vars are
secrets — its API key was not recognised as one to redact.
"""

import hermes_cli.config as config


def _reset(monkeypatch, profiles):
    monkeypatch.setattr(config, "_injected_profile_providers", set())
    monkeypatch.setattr(
        "providers.list_providers", lambda: profiles, raising=False
    )


class _Profile:
    def __init__(self, name, env_vars, auth_type="api_key"):
        self.name = name
        self.display_name = name.title()
        self.env_vars = env_vars
        self.auth_type = auth_type
        self.signup_url = None


class TestLateRegistrationHeals:
    def test_provider_registered_after_first_pass_is_picked_up(self, monkeypatch):
        early = _Profile("early", ["EARLY_API_KEY"])
        late = _Profile("late", ["LATE_API_KEY"])
        opt = dict(config.OPTIONAL_ENV_VARS)
        monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", opt)

        _reset(monkeypatch, [early])
        config._inject_profile_env_vars()
        assert "EARLY_API_KEY" in opt
        assert "LATE_API_KEY" not in opt

        # The registry grows — the old one-shot latch made this impossible.
        monkeypatch.setattr(
            "providers.list_providers", lambda: [early, late], raising=False
        )
        config._inject_profile_env_vars()
        assert "LATE_API_KEY" in opt

    def test_a_failed_pass_is_not_latched(self, monkeypatch):
        opt = dict(config.OPTIONAL_ENV_VARS)
        monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", opt)
        monkeypatch.setattr(config, "_injected_profile_providers", set())

        def _boom():
            raise RuntimeError("registry not ready")

        monkeypatch.setattr("providers.list_providers", _boom, raising=False)
        config._inject_profile_env_vars()  # must not raise

        recovered = _Profile("recovered", ["RECOVERED_API_KEY"])
        monkeypatch.setattr(
            "providers.list_providers", lambda: [recovered], raising=False
        )
        config._inject_profile_env_vars()
        assert "RECOVERED_API_KEY" in opt

    def test_repeat_calls_do_not_rebuild_known_providers(self, monkeypatch):
        calls = []
        p = _Profile("counted", ["COUNTED_API_KEY"])

        class _Counting(_Profile):
            @property
            def env_vars(self):
                calls.append(1)
                return ["COUNTED_API_KEY"]

            @env_vars.setter
            def env_vars(self, v):
                pass

        opt = dict(config.OPTIONAL_ENV_VARS)
        monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", opt)
        _reset(monkeypatch, [_Counting("counted", ["COUNTED_API_KEY"])])
        config._inject_profile_env_vars()
        config._inject_profile_env_vars()
        config._inject_profile_env_vars()
        assert len(calls) == 1, "already-seen providers must be skipped"

    def test_non_api_key_providers_contribute_nothing(self, monkeypatch):
        oauth = _Profile("oauthy", ["OAUTHY_API_KEY"], auth_type="oauth")
        opt = dict(config.OPTIONAL_ENV_VARS)
        monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", opt)
        _reset(monkeypatch, [oauth])
        config._inject_profile_env_vars()
        assert "OAUTHY_API_KEY" not in opt


class TestTheOriginalSymptom:
    def test_deepinfra_key_is_present_however_the_modules_were_imported(self):
        """The exact assertion that failed, order-independently."""
        import agent.model_metadata  # noqa: F401  (the import that poisoned it)

        assert config.ensure_provider_env_vars()["DEEPINFRA_API_KEY"]["password"] is True

    def test_every_api_key_provider_is_exposed(self):
        from providers import list_providers

        exposed = config.ensure_provider_env_vars()
        missing = [
            var
            for p in list_providers()
            if p.auth_type == "api_key"
            for var in p.env_vars
            if var not in exposed
        ]
        assert not missing, f"providers not exposed as env vars: {missing}"


class TestSecretRedactionReader:
    def test_local_environment_reconciles_before_redacting(self):
        """The consequence that is not cosmetic: an unredacted API key."""
        import inspect
        import tools.environments.local as local

        src = inspect.getsource(local)
        assert "ensure_provider_env_vars" in src, (
            "local.py must reconcile the provider registry before deciding "
            "which env vars are secrets"
        )
