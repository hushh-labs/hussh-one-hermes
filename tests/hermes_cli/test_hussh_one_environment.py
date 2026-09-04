# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Choosing which One a device links to.

Every enrollment hardcoded the UAT hosts at the two places that matter, the
authorize URL and the code exchange, so a person on ``one.hushh.ai`` could not
link Puppy One at all, although the identity state already recorded
``environment``, ``api_base`` and ``web_base`` and every later call already
read them back. Only the choice at enrollment time was missing.

These pin the three halves of that choice: the name resolves to exactly one
of two immutable bundles (config default, alias, refusal by name), the choice
made when an approval starts is the one the exchange is posted to, and a
repair stays in the environment the identity already lives in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import x25519

import hermes_cli.config as hermes_config
from hermes_cli.hussh_one_pkm.client import (
    ENVIRONMENTS,
    PRODUCTION_API_BASE,
    PRODUCTION_WEB_BASE,
    UAT_API_BASE,
    UAT_WEB_BASE,
    HusshIdentityClient,
    HusshIdentityError,
    IdentityState,
    default_environment,
    resolve_environment,
)


class FakeKeychain:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, account: str) -> bytes | None:
        return self.values.get(account)

    def set(self, account: str, secret: bytes) -> None:
        self.values[account] = secret

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """Answers the exchange and the Firebase sign-in; records every URL."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post(self, url: str, **_kwargs: object) -> FakeResponse:
        self.posted.append(url)
        if url.endswith("/trusted-device-authorizations/exchange"):
            return FakeResponse(
                {
                    "firebase_custom_token": "custom-token",
                    "user_id": "user-1",
                    "device_id": "tdv_production",
                    "account_email": "owner@example.com",
                }
            )
        return FakeResponse(
            {"refreshToken": "refresh-token", "idToken": "id-token", "expiresIn": "3600"}
        )


class _FakeServer:
    """Stands in for the loopback listener once the browser has answered."""

    timeout = 0
    result = {"code": "authorization-code", "state": "state-1"}
    closed = False

    def handle_request(self) -> None:
        return

    def server_close(self) -> None:
        self.closed = True


def _config(monkeypatch: pytest.MonkeyPatch, config: object) -> None:
    """Make ``load_config_readonly`` answer with ``config`` (or raise it)."""

    def _load() -> object:
        if isinstance(config, Exception):
            raise config
        return config

    monkeypatch.setattr(hermes_config, "load_config_readonly", _load)


def _client(tmp_path: Path, *, http: FakeHttp | None = None) -> HusshIdentityClient:
    return HusshIdentityClient(
        profile_home=tmp_path,
        keychain=FakeKeychain(),  # type: ignore[arg-type]
        http=http,  # type: ignore[arg-type]
    )


class TestResolveEnvironment:
    def test_the_two_bundles_are_the_only_environments(self) -> None:
        assert ENVIRONMENTS == {
            "uat": (UAT_API_BASE, UAT_WEB_BASE),
            "production": (PRODUCTION_API_BASE, PRODUCTION_WEB_BASE),
        }
        assert PRODUCTION_API_BASE == "https://api.hushh.ai"
        assert PRODUCTION_WEB_BASE == "https://one.hushh.ai"

    def test_blank_falls_back_to_the_config_default_then_uat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(monkeypatch, {})
        assert default_environment() == "uat"
        assert resolve_environment(None) == "uat"
        assert resolve_environment("   ") == "uat"

        _config(monkeypatch, {"hussh_one": {"environment": "production"}})
        assert default_environment() == "production"
        assert resolve_environment(None) == "production"

    def test_an_unreadable_or_empty_config_never_blocks_enrollment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(monkeypatch, RuntimeError("config.yaml is unreadable"))
        assert resolve_environment(None) == "uat"

        _config(monkeypatch, {"hussh_one": {"environment": ""}})
        assert resolve_environment(None) == "uat"

        _config(monkeypatch, {"hussh_one": "not a section"})
        assert resolve_environment(None) == "uat"

    def test_names_are_case_insensitive_and_prod_is_an_alias(self) -> None:
        assert resolve_environment("production") == "production"
        assert resolve_environment("PROD") == "production"
        assert resolve_environment(" Production ") == "production"
        assert resolve_environment("UAT") == "uat"

    def test_anything_else_is_refused_naming_both_accepted_values(self) -> None:
        with pytest.raises(HusshIdentityError) as refused:
            resolve_environment("mars")
        message = str(refused.value)
        assert "mars" in message
        assert '"uat"' in message
        assert '"production"' in message

    def test_a_misspelt_config_default_is_refused_rather_than_becoming_uat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silently enrolling against UAT when the owner asked for something
        # else by name is how a machine ends up linked to the wrong One.
        _config(monkeypatch, {"hussh_one": {"environment": "staging"}})
        with pytest.raises(HusshIdentityError, match="staging"):
            resolve_environment(None)


class TestTheChoiceTravelsWithTheApproval:
    def test_production_builds_the_authorize_url_on_one_hushh_ai(
        self, tmp_path: Path
    ) -> None:
        client = _client(tmp_path)
        try:
            result = client.start_authorization(device_name="Mac", environment="PROD")

            assert result["authorization_url"].startswith(
                f"{PRODUCTION_WEB_BASE}/one/profile/security/devices/authorize?"
            )
            assert result["environment"] == "production"
            assert result["web_base"] == PRODUCTION_WEB_BASE
            # Resolved once, here; the exchange reads these rather than
            # resolving again (a config edit mid-approval must not move it).
            assert client._pending["environment"] == "production"
            assert client._pending["api_base"] == PRODUCTION_API_BASE
            assert client._pending["web_base"] == PRODUCTION_WEB_BASE
        finally:
            client.cancel_pending_authorization()

    def test_a_bare_start_uses_the_config_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(monkeypatch, {})
        client = _client(tmp_path)
        try:
            result = client.start_authorization(device_name="Mac")
            assert result["authorization_url"].startswith(
                f"{UAT_WEB_BASE}/one/profile/security/devices/authorize?"
            )
            assert result["environment"] == "uat"
            assert client._pending["api_base"] == UAT_API_BASE
        finally:
            client.cancel_pending_authorization()

    def test_an_unknown_environment_is_refused_before_a_listener_opens(
        self, tmp_path: Path
    ) -> None:
        client = _client(tmp_path)
        with pytest.raises(HusshIdentityError, match="mars"):
            client.start_authorization(device_name="Mac", environment="mars")
        assert client._pending is None

    def test_the_exchange_is_posted_to_the_environment_the_approval_started_in(
        self, tmp_path: Path
    ) -> None:
        http = FakeHttp()
        client = _client(tmp_path, http=http)
        started = client.start_authorization(device_name="Mac", environment="production")
        assert started["authorization_url"].startswith(PRODUCTION_WEB_BASE)

        # The browser has answered: hand the pending approval to a fake
        # listener and retire the real one so its thread ends at once.
        server = _FakeServer()
        with client._pending_lock:
            listener = client._pending["server"]
            client._pending["server"] = server
            verifier = client._pending["verifier"]
        listener.server_close()

        client._serve_authorization(server, verifier)  # type: ignore[arg-type]

        assert client._pending["status"] == "connected"
        assert http.posted[0] == (
            f"{PRODUCTION_API_BASE}/api/account/trusted-device-authorizations/exchange"
        )
        assert UAT_API_BASE not in "".join(http.posted)
        state = client.read_state()
        assert state is not None
        assert state.environment == "production"
        assert state.api_base == PRODUCTION_API_BASE
        assert state.web_base == PRODUCTION_WEB_BASE

        # A fresh client over the same profile restores the choice from disk,
        # which is what every later call (status, heartbeat, vault) reads.
        restored = _client(tmp_path).read_state()
        assert restored is not None
        assert restored.environment == "production"
        assert restored.api_base == PRODUCTION_API_BASE
        assert restored.web_base == PRODUCTION_WEB_BASE

    def test_a_pending_record_without_a_choice_still_exchanges_on_uat(
        self, tmp_path: Path
    ) -> None:
        # The shape written before the choice existed: the defaults keep it
        # on UAT, which is the only place such a record could have come from.
        http = FakeHttp()
        client = _client(tmp_path, http=http)
        server = _FakeServer()
        client._pending = {
            "server": server,
            "status": "waiting",
            "error": None,
            "on_connected": None,
            "vault_handoff_private_key": x25519.X25519PrivateKey.generate(),
            "expected_account_email": "",
        }

        client._serve_authorization(server, "v" * 43)  # type: ignore[arg-type]

        assert client._pending["status"] == "connected"
        assert http.posted[0].startswith(UAT_API_BASE)
        state = client.read_state()
        assert state is not None
        assert state.environment == "uat"


class TestARepairStaysWhereTheIdentityLives:
    def _bridge(self, tmp_path: Path, *, state: IdentityState | None):
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        bridge = HusshVaultBridge(profile_home=tmp_path)
        calls: list[dict] = []

        class _Identity:
            profile_id = "p1"

            def lock_identity(self) -> None:
                return

            def read_state(self):
                return state

            def open_authorization(self, **kwargs):
                calls.append(kwargs)
                return {
                    "status": "waiting",
                    "authorization_url": "https://example",
                    "expires_in": 300,
                    "environment": kwargs.get("environment") or "uat",
                    "web_base": "https://example",
                }

        bridge.identity = _Identity()  # type: ignore[assignment]
        return bridge, calls

    def test_reconnect_on_a_uat_identity_ignores_a_production_argument(
        self, tmp_path: Path
    ) -> None:
        bridge, calls = self._bridge(
            tmp_path,
            state=IdentityState(
                user_id="user-1",
                device_id="tdv_original",
                profile_id="p1",
                account_email="owner@example.com",
                environment="uat",
            ),
        )

        result = bridge.begin_onboarding(
            device_name="Mac", reconnect=True, environment="production"
        )

        assert result["mode"] == "reconnect"
        assert result["environment"] == "uat"
        (call,) = calls
        # The device row being replaced, the vault envelope and the replica all
        # live on UAT; an argument cannot move them.
        assert call["environment"] == "uat"
        assert call["replaces_device_id"] == "tdv_original"

    def test_a_fresh_connect_passes_the_environment_through(
        self, tmp_path: Path
    ) -> None:
        bridge, calls = self._bridge(tmp_path, state=None)

        result = bridge.begin_onboarding(device_name="Mac", environment="production")

        assert result["mode"] == "connect"
        assert result["environment"] == "production"
        (call,) = calls
        assert call["environment"] == "production"
        assert call["replaces_device_id"] is None

    def test_a_fresh_connect_without_a_choice_leaves_it_to_the_client(
        self, tmp_path: Path
    ) -> None:
        bridge, calls = self._bridge(tmp_path, state=None)

        bridge.begin_onboarding(device_name="Mac")

        (call,) = calls
        # None, not "uat": the client applies hussh_one.environment itself.
        assert call["environment"] is None


class _FakeConnectBridge:
    """A profile with nothing linked yet; records what connect asks for."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def identity_status(self) -> dict:
        return {"connected": False, "account_email": None, "onboarding_status": "idle"}

    def vault_status(self) -> dict:
        return {"enrolled": False, "unlocked": False}

    def begin_onboarding(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        environment = kwargs.get("environment") or "uat"
        web_base = ENVIRONMENTS[environment][1]
        return {
            "status": "waiting",
            "authorization_url": f"{web_base}/one/profile/security/devices/authorize?x=y",
            "expires_in": 300,
            "mode": "connect",
            "environment": environment,
            "web_base": web_base,
        }


class TestTheChatCommandTakesTheEnvironmentAsAnArgument:
    def _server(self, monkeypatch: pytest.MonkeyPatch):
        from tui_gateway import server

        bridge = _FakeConnectBridge()
        monkeypatch.setattr(server, "_hussh_one_bridge", lambda: bridge)
        return server, bridge

    def test_connect_production_links_to_one_hushh_ai(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, bridge = self._server(monkeypatch)

        result = server._hussh_one_setup_output("connect   production")

        assert bridge.calls == [
            {"device_name": "Hussh One Hermes dashboard", "environment": "production"}
        ]
        # The confirmation names the host, because "a browser window was
        # opened" no longer says which One is being linked.
        assert "opened at one.hushh.ai" in result

    def test_connect_prod_is_the_same_as_connect_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, bridge = self._server(monkeypatch)

        server._hussh_one_setup_output("connect prod")

        assert bridge.calls[0]["environment"] == "production"

    def test_connect_uat_names_uat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, bridge = self._server(monkeypatch)

        result = server._hussh_one_setup_output("connect uat")

        assert bridge.calls[0]["environment"] == "uat"
        assert "opened at uat.one.hushh.ai" in result

    def test_a_bare_connect_leaves_the_choice_to_the_config_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, bridge = self._server(monkeypatch)

        server._hussh_one_setup_output("connect")

        assert bridge.calls[0]["environment"] is None

    def test_connect_mars_is_refused_naming_the_accepted_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, bridge = self._server(monkeypatch)

        result = server._hussh_one_setup_output("connect mars")

        assert bridge.calls == []
        assert "mars" in result
        assert '"uat"' in result
        assert '"production"' in result
        assert "/hussh-one connect [uat|production]" in result

    def test_help_documents_the_environment_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, _bridge = self._server(monkeypatch)

        help_text = server._hussh_one_setup_output("help")

        assert "/hussh-one connect [uat|production]" in help_text
        assert "hussh_one.environment" in help_text
