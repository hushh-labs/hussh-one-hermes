# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Seal on revoke.

Unlinking a device in the Hussh One cloud app must destroy this device's local
copy of the vault. A credential-only disconnect is NOT a seal: it drops the
login while leaving the envelope, the device-wrapping key, the encrypted PKM
replica and the Source Library on disk, so a revoked device could still open
everything it already held.
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge
from hermes_cli.hussh_one_pkm.client import HusshIdentityClient


class _SealStatusHttp:
    """Fake HTTP answering the device-status read and recording the seal ack."""

    def __init__(self, *, status_code: int = 200, status: str = "active") -> None:
        self.status_code = status_code
        self.status = status
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(f"GET {url}")
        payload = {"status": self.status}
        return SimpleNamespace(status_code=self.status_code, json=lambda: payload)

    def post(self, url: str, **_kwargs):
        self.calls.append(f"POST {url}")
        return SimpleNamespace(status_code=200, json=lambda: {})


def _identity_for_status(tmp_path: Path, http) -> HusshIdentityClient:
    identity = HusshIdentityClient(
        profile_home=tmp_path,
        keychain=SimpleNamespace(get=lambda _a: None, set=lambda _a, _v: None),
        http=http,
    )
    identity.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity.identity_path.write_text(
        json.dumps(
            {
                "user_id": "user-1",
                "device_id": "tdv_seal_test",
                "profile_id": identity.profile_id,
                "account_email": "owner@example.test",
                "api_base": "https://api.example.test",
                "web_base": "https://web.example.test",
                "environment": "uat",
            }
        ),
        encoding="utf-8",
    )
    # Bypass the Firebase refresh path; this suite exercises status handling.
    identity.auth_headers = lambda: {"Authorization": "Bearer test"}  # type: ignore[method-assign]
    return identity


@pytest.mark.parametrize(
    ("status_code", "status", "expected"),
    [
        (200, "revoked", "revoked"),
        (200, "active", "active"),
        (404, "", "unknown_device"),
        (503, "", "indeterminate"),
        (500, "", "indeterminate"),
    ],
)
def test_device_status_classification(tmp_path, status_code, status, expected):
    http = _SealStatusHttp(status_code=status_code, status=status)
    identity = _identity_for_status(tmp_path, http)
    assert identity.device_status() == expected


def test_device_status_never_raises_on_transport_failure(tmp_path):
    class Exploding(_SealStatusHttp):
        def get(self, url: str, **_kwargs):
            raise RuntimeError("network down")

    identity = _identity_for_status(tmp_path, Exploding())
    # A transport failure must be indistinguishable from "don't act": the
    # caller destroys user data on "revoked".
    assert identity.device_status() == "indeterminate"


def _seal_bridge(tmp_path: Path, identity, order: list[str]):
    # object.__new__ keeps the daemon monitor thread out of the test.
    bridge = object.__new__(HusshVaultBridge)
    bridge._lock = threading.RLock()
    bridge._seal_state = None
    bridge.profile_home = tmp_path
    bridge.seal_state_path = tmp_path / "hussh-one" / "seal-state.json"
    bridge.identity = identity
    bridge.lock = lambda *, reason="explicit": order.append("lock")  # type: ignore[method-assign]
    bridge.remove_local_vault = lambda: order.append("remove_local_vault")  # type: ignore[method-assign]
    return bridge


def test_seal_destroys_local_vault_and_credentials_in_order(tmp_path):
    order: list[str] = []

    def _ack():
        order.append("seal_ack")
        return True

    identity = SimpleNamespace(
        post_seal_ack=_ack,
        disconnect=lambda *, remove_device_key: order.append(
            f"disconnect:{remove_device_key}"
        ),
    )
    bridge = _seal_bridge(tmp_path, identity, order)

    result = bridge.seal()

    assert result["sealed"] is True
    # Lock first (no concurrent reader may re-open the envelope mid-seal), ack
    # while credentials still exist, then destroy data, then credentials.
    assert order == ["lock", "seal_ack", "remove_local_vault", "disconnect:True"]
    assert bridge._seal_state == "sealed"


def test_seal_writes_marker_before_destroying(tmp_path):
    order: list[str] = []
    identity = SimpleNamespace(
        post_seal_ack=lambda: True,
        disconnect=lambda *, remove_device_key: None,
    )
    bridge = _seal_bridge(tmp_path, identity, order)

    bridge.seal(reason="trusted_device_revoked")

    # The marker explains an empty profile after a crash mid-seal.
    payload = json.loads(bridge.seal_state_path.read_text(encoding="utf-8"))
    assert payload["sealed"] is True
    assert payload["reason"] == "trusted_device_revoked"
    assert payload["seal_ack_delivered"] is True


def test_seal_is_idempotent(tmp_path):
    order: list[str] = []
    acks: list[int] = []

    def _ack():
        acks.append(1)
        return True

    identity = SimpleNamespace(
        post_seal_ack=_ack,
        disconnect=lambda *, remove_device_key: None,
    )
    bridge = _seal_bridge(tmp_path, identity, order)

    bridge.seal()
    bridge.seal()

    # The monitor thread and an in-flight owner call can both trip at once.
    assert len(acks) == 1
    assert order.count("remove_local_vault") == 1


def test_seal_completes_even_when_ack_fails(tmp_path):
    order: list[str] = []

    def _failing_ack():
        raise RuntimeError("cloud unreachable")

    identity = SimpleNamespace(
        post_seal_ack=_failing_ack,
        disconnect=lambda *, remove_device_key: order.append("disconnect"),
    )
    bridge = _seal_bridge(tmp_path, identity, order)

    result = bridge.seal()

    # The ack is advisory. Losing it must never leave the vault on disk.
    assert result["sealed"] is True
    assert "remove_local_vault" in order
    assert "disconnect" in order


def _tick_bridge(status: str, sealed: list[str], seal_state=None):
    bridge = object.__new__(HusshVaultBridge)
    bridge._lock = threading.RLock()
    bridge._seal_state = seal_state
    bridge.identity = SimpleNamespace(device_status=lambda: status)
    bridge.seal = lambda: sealed.append("sealed")  # type: ignore[method-assign]
    return bridge


@pytest.mark.parametrize(
    ("status", "should_seal"),
    [
        ("revoked", True),
        ("active", False),
        ("unknown_device", False),
        ("indeterminate", False),
    ],
)
def test_revocation_tick_seals_only_on_explicit_revoked(status, should_seal):
    sealed: list[str] = []
    _tick_bridge(status, sealed)._revocation_tick()
    assert bool(sealed) is should_seal


def test_unknown_device_marks_needs_reinit_without_destroying():
    sealed: list[str] = []
    bridge = _tick_bridge("unknown_device", sealed)

    bridge._revocation_tick()

    # A missing server row is a re-enrollment signal, not consent to delete:
    # destroying here would turn a server-side problem into user data loss.
    assert sealed == []
    assert bridge._seal_state == "needs_reinit"


def test_active_status_clears_stale_needs_reinit():
    bridge = _tick_bridge("active", [], seal_state="needs_reinit")
    bridge._revocation_tick()
    assert bridge._seal_state is None


def test_revocation_poll_is_not_gated_behind_the_lock_chain():
    # Regression guard: the monitor body is an if/elif ladder, so a locked
    # profile short-circuits everything after it. The revocation poll must be
    # its own unconditional `if` -- a locked device is exactly the one that
    # must still learn it was revoked instead of re-opening on unlock.
    source = inspect.getsource(HusshVaultBridge._monitor_shared_lock_state)
    assert source.index("_next_revocation_check_at") < source.index(
        "if self._profile_is_locked()"
    )


def test_identity_revocation_delegates_to_the_full_seal(tmp_path):
    # A revoked device must run the owner's seal, not the credential-only
    # disconnect that leaves the envelope and replica behind.
    calls: list[str] = []
    identity = HusshIdentityClient(
        profile_home=tmp_path,
        keychain=SimpleNamespace(get=lambda _a: None, set=lambda _a, _v: None),
        http=_SealStatusHttp(),
        on_revoked=lambda: calls.append("seal"),
    )
    identity.disconnect = lambda *, remove_device_key: calls.append(  # type: ignore[method-assign]
        "disconnect"
    )

    identity._handle_revoked()

    assert calls == ["seal"]


def test_identity_revocation_falls_back_when_seal_fails(tmp_path):
    calls: list[str] = []

    def _boom():
        raise RuntimeError("seal failed")

    identity = HusshIdentityClient(
        profile_home=tmp_path,
        keychain=SimpleNamespace(get=lambda _a: None, set=lambda _a, _v: None),
        http=_SealStatusHttp(),
        on_revoked=_boom,
    )
    identity.disconnect = lambda *, remove_device_key: calls.append(  # type: ignore[method-assign]
        f"disconnect:{remove_device_key}"
    )

    identity._handle_revoked()

    # A failed seal must not strand the device holding a live login too.
    assert calls == ["disconnect:True"]
