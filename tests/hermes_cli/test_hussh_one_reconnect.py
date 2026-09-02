# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Repairing a device login without destroying what the device holds.

Before this existed, a Hermes whose refresh token died had exactly one route
back: ``/hussh-one disconnect confirm``, which removes the vault envelope, the
encrypted PKM replica and Source Library custody. Losing local data to fix a
token is the wrong trade, and the symptom that leads there is silent -- a stale
login stops the presence heartbeat, so One shows the machine as gone while
everything on the machine looks healthy.

These pin the two halves that make the repair safe: an expired session is
distinguishable from a revocation, and a repair may only re-approve the account
this machine already holds custody for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import x25519

from hermes_cli.hussh_one_pkm.client import (
    HusshIdentityClient,
    HusshIdentityError,
    HusshSessionExpiredError,
    IdentityState,
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
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return self._payload


def _connected_client(tmp_path: Path, *, email: str = "owner@example.com", http=None):
    client = HusshIdentityClient(
        profile_home=tmp_path,
        keychain=FakeKeychain(),  # type: ignore[arg-type]
        http=http,  # type: ignore[arg-type]
    )
    state = IdentityState(
        user_id="user-1",
        device_id="tdv_original",
        profile_id=client.profile_id,
        account_email=email,
    )
    client.identity_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client.identity_path.write_text(
        __import__("json").dumps(state.to_json()), encoding="utf-8"
    )
    client.keychain.set(client._account("firebase-refresh-token"), b"refresh-token")
    return client


class TestSessionState:
    """A status read must answer "is this device reaching Hussh One", not
    "is an enrollment stored here"."""

    def test_a_profile_with_no_identity_is_not_connected(self, tmp_path: Path) -> None:
        client = HusshIdentityClient(
            profile_home=tmp_path, keychain=FakeKeychain()  # type: ignore[arg-type]
        )
        assert client.session_state() == "not_connected"

    def test_a_dead_refresh_token_reads_as_expired_not_revoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _connected_client(tmp_path)

        def _expired() -> str:
            raise HusshSessionExpiredError("The Hussh device session expired.")

        monkeypatch.setattr(client, "id_token", _expired)
        # Expired is recoverable in place; revoked means seal. Conflating them
        # is what made an aged-out login look like a reason to disconnect.
        assert client.session_state() == "expired"

    def test_a_revoked_device_is_reported_as_revoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _connected_client(tmp_path)

        def _revoked() -> str:
            raise HusshIdentityError("This Hussh trusted device was revoked.")

        monkeypatch.setattr(client, "id_token", _revoked)
        assert client.session_state() == "revoked"

    def test_a_live_session_reads_ok_and_never_raises_on_transport_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _connected_client(tmp_path)
        monkeypatch.setattr(client, "id_token", lambda: "token")
        assert client.session_state() == "ok"

        def _boom() -> str:
            raise TimeoutError("network down")

        monkeypatch.setattr(client, "id_token", _boom)
        # A monitor must never be the outage: an unreachable check is
        # indeterminate, which no caller acts on.
        assert client.session_state() == "indeterminate"


class TestRepairOnlyRebindsTheSameAccount:
    def _exchange_client(self, tmp_path: Path, *, returned_email: str):
        class FakeHttp:
            def post(self, url: str, **_kwargs: object) -> FakeResponse:
                if url.endswith("/trusted-device-authorizations/exchange"):
                    return FakeResponse(
                        {
                            "firebase_custom_token": "custom-token",
                            "user_id": "user-1",
                            "device_id": "tdv_replacement",
                            "account_email": returned_email,
                        }
                    )
                return FakeResponse(
                    {
                        "refreshToken": "refresh-token-2",
                        "idToken": "id-token-2",
                        "expiresIn": "3600",
                    }
                )

        return _connected_client(tmp_path, http=FakeHttp())

    class _FakeServer:
        timeout = 0
        result = {"code": "authorization-code", "state": "state-1"}
        closed = False

        def handle_request(self) -> None:
            return

        def server_close(self) -> None:
            self.closed = True

    def _pending(self, *, expected: str) -> dict:
        return {
            "server": None,
            "status": "waiting",
            "error": None,
            "on_connected": None,
            "vault_handoff_private_key": x25519.X25519PrivateKey.generate(),
            "expected_account_email": expected,
        }

    def test_the_same_account_completes_and_replaces_the_stored_identity(
        self, tmp_path: Path
    ) -> None:
        client = self._exchange_client(tmp_path, returned_email="owner@example.com")
        server = self._FakeServer()
        pending = self._pending(expected="owner@example.com")
        pending["server"] = server
        client._pending = pending

        client._serve_authorization(server, "v" * 43)  # type: ignore[arg-type]

        assert client._pending["status"] == "connected"
        state = client.read_state()
        assert state is not None
        # The server swapped the device row; the local identity follows it.
        assert state.device_id == "tdv_replacement"
        assert state.account_email == "owner@example.com"

    def test_a_different_account_is_refused_and_nothing_is_overwritten(
        self, tmp_path: Path
    ) -> None:
        client = self._exchange_client(tmp_path, returned_email="someone-else@example.com")
        server = self._FakeServer()
        pending = self._pending(expected="owner@example.com")
        pending["server"] = server
        client._pending = pending

        client._serve_authorization(server, "v" * 43)  # type: ignore[arg-type]

        assert client._pending["status"] == "error"
        assert "different Hussh One account" in str(client._pending["error"])
        state = client.read_state()
        assert state is not None
        # The vault envelope and encrypted replica on this machine belong to
        # the original account. Rebinding the identity under them would leave
        # one account's data behind another account's login.
        assert state.device_id == "tdv_original"
        assert state.account_email == "owner@example.com"
        assert client.keychain.get(client._account("firebase-refresh-token")) == b"refresh-token"

    def test_case_and_whitespace_do_not_defeat_the_match(self, tmp_path: Path) -> None:
        client = self._exchange_client(tmp_path, returned_email="  Owner@Example.com  ")
        server = self._FakeServer()
        pending = self._pending(expected="owner@example.com")
        pending["server"] = server
        client._pending = pending

        client._serve_authorization(server, "v" * 43)  # type: ignore[arg-type]

        assert client._pending["status"] == "connected"

    def test_a_first_connection_carries_no_expectation(self, tmp_path: Path) -> None:
        client = self._exchange_client(tmp_path, returned_email="new-owner@example.com")
        server = self._FakeServer()
        pending = self._pending(expected="")
        pending["server"] = server
        client._pending = pending

        client._serve_authorization(server, "v" * 43)  # type: ignore[arg-type]

        assert client._pending["status"] == "connected"


class TestBeginOnboardingRepairPath:
    """The bridge decides repair vs new; the client enforces the account."""

    def _bridge(self, tmp_path: Path, *, email: str):
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        bridge = HusshVaultBridge(profile_home=tmp_path)
        calls: list[dict] = []

        class _Identity:
            profile_id = "p1"

            def lock_identity(self) -> None:
                # The bridge's lock monitor runs on its own thread and calls
                # this; a stub without it turns into an unhandled-thread
                # warning that has nothing to do with the assertion.
                return

            def read_state(self):
                return IdentityState(
                    user_id="user-1",
                    device_id="tdv_original",
                    profile_id="p1",
                    account_email=email,
                )

            def open_authorization(self, **kwargs):
                calls.append(kwargs)
                return {"status": "waiting", "authorization_url": "https://example", "expires_in": 300}

        bridge.identity = _Identity()  # type: ignore[assignment]
        return bridge, calls

    def test_reconnect_repairs_in_place_and_pins_the_account(self, tmp_path: Path) -> None:
        bridge, calls = self._bridge(tmp_path, email="owner@example.com")

        result = bridge.begin_onboarding(device_name="Mac", reconnect=True)

        assert result["mode"] == "reconnect"
        (call,) = calls
        # replaces_device_id makes the server swap the row atomically rather
        # than leaving an orphaned device behind.
        assert call["replaces_device_id"] == "tdv_original"
        assert call["expected_account_email"] == "owner@example.com"

    def test_connect_on_a_connected_profile_still_refuses(self, tmp_path: Path) -> None:
        from hermes_cli.hussh_one_pkm.crypto import VaultCryptoError

        bridge, calls = self._bridge(tmp_path, email="owner@example.com")
        with pytest.raises(VaultCryptoError, match="Disconnect it before"):
            bridge.begin_onboarding(device_name="Mac")
        assert calls == []

    def test_a_legacy_state_without_an_email_connects_without_an_expectation(
        self, tmp_path: Path
    ) -> None:
        bridge, calls = self._bridge(tmp_path, email="")

        result = bridge.begin_onboarding(device_name="Mac")

        assert result["mode"] == "connect"
        (call,) = calls
        assert call["expected_account_email"] is None
        assert call["replaces_device_id"] == "tdv_original"


class TestAnAbandonedApprovalNeverLocksTheOwnerOut:
    """Asking to connect again means "start over", not "you are locked out".

    A waiting authorization used to make ``start_authorization`` refuse. When
    the browser tab was closed, never opened, or simply ignored, that turned
    into a five-minute lockout with no command to clear it: every retry
    answered "already in progress", the generic handler rewrote that as advice
    to run ``enroll``, and ``enroll`` answers "connect first". A closed loop.
    """

    def _client(self, tmp_path: Path):
        return HusshIdentityClient(
            profile_home=tmp_path, keychain=FakeKeychain()  # type: ignore[arg-type]
        )

    def test_a_second_request_supersedes_the_first_instead_of_refusing(
        self, tmp_path: Path
    ) -> None:
        client = self._client(tmp_path)

        first = client.start_authorization(device_name="Mac")
        first_pending = client._pending
        assert first["status"] == "waiting"

        # The owner asks again. This must not raise.
        second = client.start_authorization(device_name="Mac")

        assert second["status"] == "waiting"
        assert second["authorization_url"] != first["authorization_url"]
        assert client._pending is not first_pending
        assert client._pending["status"] == "waiting"

        client.cancel_pending_authorization()

    def test_the_superseded_listener_is_closed_so_its_port_is_released(
        self, tmp_path: Path
    ) -> None:
        client = self._client(tmp_path)
        client.start_authorization(device_name="Mac")
        stale_server = client._pending["server"]

        client.start_authorization(device_name="Mac")

        # A closed socket reports fileno() -1; a live listener reports its fd.
        assert stale_server.socket.fileno() == -1

        client.cancel_pending_authorization()

    def test_a_finished_attempt_also_leaves_the_next_one_free(
        self, tmp_path: Path
    ) -> None:
        client = self._client(tmp_path)
        client.start_authorization(device_name="Mac")
        client._pending["status"] = "error"

        result = client.start_authorization(device_name="Mac")

        assert result["status"] == "waiting"
        client.cancel_pending_authorization()


class TestSessionHealth:
    def test_it_names_the_remedy_for_an_expired_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        bridge = HusshVaultBridge(profile_home=tmp_path)
        monkeypatch.setattr(bridge.identity, "session_state", lambda: "expired")

        health = bridge.session_health()

        assert health["session"] == "expired"
        assert health["reconnect_required"] is True
        # A live heartbeat is what makes One show the machine as here.
        assert health["heartbeat_live"] is False
        assert "reconnect" in health["remedy"]

    def test_a_healthy_session_asks_for_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        bridge = HusshVaultBridge(profile_home=tmp_path)
        monkeypatch.setattr(bridge.identity, "session_state", lambda: "ok")

        health = bridge.session_health()

        assert health == {
            "session": "ok",
            "reconnect_required": False,
            "heartbeat_live": True,
            "remedy": "",
        }
