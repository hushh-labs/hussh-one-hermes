# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Profile-scoped local vault bridge for trusted Hermes installations."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from hermes_constants import get_hermes_home

from .client import HusshIdentityClient
from .crypto import (
    VaultCryptoError,
    create_vault_with_passphrase,
    read_envelope,
    unwrap_local_vault_key,
    unwrap_passphrase_vault_key,
    vault_key_hash,
    wrap_local_vault_key,
    write_envelope,
)
from .keychain import MacOSKeychain


class HusshVaultBridge:
    """Owns identity, vault memory, and device-bound owner capabilities."""

    INACTIVITY_TIMEOUT_SECONDS = 15 * 60

    def __init__(
        self,
        *,
        profile_home: Path | None = None,
        keychain: MacOSKeychain | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.profile_home = profile_home or get_hermes_home()
        self.keychain = keychain or MacOSKeychain()
        self.http = http or httpx.Client(timeout=45.0)
        self.identity = HusshIdentityClient(
            profile_home=self.profile_home,
            keychain=self.keychain,
            http=self.http,
        )
        self.envelope_path = self.profile_home / "hussh-one" / "vault-envelope.json"
        self.lock_state_path = self.profile_home / "hussh-one" / "vault-lock-state.json"
        self._vault_key: bytearray | None = None
        self._last_activity = 0.0
        self._lock = threading.RLock()
        self._monitor = threading.Thread(
            target=self._monitor_shared_lock_state,
            name=f"hussh-vault-lock-{self.identity.profile_id}",
            daemon=True,
        )
        self._monitor.start()

    def _account(self, kind: str) -> str:
        return f"{self.identity.profile_id}:{kind}"

    def identity_status(self) -> dict[str, Any]:
        state = self.identity.read_state()
        return {
            "connected": state is not None,
            "environment": state.environment if state else "uat",
            "device_id": state.device_id if state else None,
            "account_email": state.account_email if state else None,
            "profile_id": self.identity.profile_id,
        }

    def vault_status(self) -> dict[str, Any]:
        state = self.identity.read_state()
        return {
            "connected": state is not None,
            "enrolled": self.envelope_path.exists(),
            "unlocked": self._active_vault_key() is not None,
            "profile_locked": self._profile_is_locked(),
            "device_id": state.device_id if state else None,
            "profile_id": self.identity.profile_id,
        }

    def vault_preflight(self) -> dict[str, Any]:
        """Return whether the authenticated Hussh account already has a vault."""
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        response = self.http.post(
            f"{state.api_base}/db/vault/check",
            headers=self.identity.auth_headers(),
            json={"userId": state.user_id},
        )
        response.raise_for_status()
        return {"vault_exists": bool(response.json().get("hasVault"))}

    def enroll_vault(self, passphrase: str) -> dict[str, Any]:
        """Fetch and unwrap the existing passphrase wrapper locally.

        The passphrase is intentionally never retained as an attribute and is
        not included in any exception or return value.
        """
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        if not self.vault_preflight()["vault_exists"]:
            raise VaultCryptoError(
                "This Hussh One account does not have a vault yet. Create it locally first."
            )
        response = self.http.post(
            f"{state.api_base}/db/vault/get",
            headers=self.identity.auth_headers(),
            json={"userId": state.user_id},
        )
        response.raise_for_status()
        vault_state = response.json()
        wrappers = vault_state.get("wrappers") or []
        wrapper = next(
            (item for item in wrappers if item.get("method") == "passphrase"),
            None,
        )
        if not wrapper:
            raise VaultCryptoError("The account has no passphrase vault wrapper.")
        vault_key = unwrap_passphrase_vault_key(
            passphrase=passphrase,
            encrypted_vault_key=str(wrapper["encryptedVaultKey"]),
            salt=str(wrapper["salt"]),
            iv=str(wrapper["iv"]),
        )
        expected_hash = str(vault_state.get("vaultKeyHash") or "")
        if expected_hash and vault_key_hash(vault_key) != expected_hash:
            raise VaultCryptoError("The vault key failed its integrity check.")

        return self._finish_vault_enrollment(
            state=state,
            vault_key=vault_key,
            expected_hash=expected_hash,
        )

    def create_vault(
        self,
        passphrase: str,
        *,
        disclose_recovery: Callable[[str], bool],
    ) -> dict[str, Any]:
        """Create the existing One passphrase/recovery wrapper format locally.

        ``disclose_recovery`` runs locally in the protected native surface
        before anything reaches the server. Creation never replaces an active
        remote vault: the server remains the final concurrency guard.
        """
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        if self.vault_preflight()["vault_exists"]:
            raise VaultCryptoError(
                "A Hussh One vault already exists for this account. Secure this device instead."
            )
        created = create_vault_with_passphrase(passphrase)
        if not disclose_recovery(created.recovery_key):
            raise VaultCryptoError(
                "Vault creation was canceled before the recovery key was acknowledged."
            )
        response = self.http.post(
            f"{state.api_base}/db/vault/setup",
            headers={
                **self.identity.auth_headers(),
                "Content-Type": "application/json",
                "x-hushh-client-version": "2.0.0",
            },
            json={
                "userId": state.user_id,
                "vaultKeyHash": created.vault_key_hash,
                "primaryMethod": "passphrase",
                "primaryWrapperId": "default",
                "recoveryEncryptedVaultKey": created.recovery_encrypted_vault_key,
                "recoverySalt": created.recovery_salt,
                "recoveryIv": created.recovery_iv,
                "wrappers": [
                    {
                        "method": "passphrase",
                        "wrapperId": "default",
                        "encryptedVaultKey": created.encrypted_vault_key,
                        "salt": created.salt,
                        "iv": created.iv,
                    }
                ],
            },
        )
        if response.status_code in {400, 409}:
            detail = response.json() if response.content else {}
            message = str(detail.get("detail") or detail.get("error") or "")
            if "already exists" in message.lower():
                raise VaultCryptoError(
                    "A vault was created elsewhere. No existing vault was changed; secure this device instead."
                )
        response.raise_for_status()
        persisted = self.http.post(
            f"{state.api_base}/db/vault/get",
            headers=self.identity.auth_headers(),
            json={"userId": state.user_id},
        )
        persisted.raise_for_status()
        vault_state = persisted.json()
        expected_hash = str(vault_state.get("vaultKeyHash") or "")
        if expected_hash != created.vault_key_hash:
            raise VaultCryptoError("The newly created vault failed its integrity check.")
        try:
            return self._finish_vault_enrollment(
                state=state,
                vault_key=created.vault_key,
                expected_hash=expected_hash,
            )
        except Exception as exc:
            raise VaultCryptoError(
                "The vault was created, but this device could not be secured. "
                "Run Hussh One enrollment again using the passphrase you just created."
            ) from exc

    def _finish_vault_enrollment(
        self,
        *,
        state: Any,
        vault_key: bytes,
        expected_hash: str,
    ) -> dict[str, Any]:
        if expected_hash and vault_key_hash(vault_key) != expected_hash:
            raise VaultCryptoError("The vault key failed its integrity check.")
        self._write_lock_state(locked=False, reason="vault_enrollment_validation")
        with self._lock:
            self._clear_vault_key_locked()
            self._vault_key = bytearray(vault_key)
            self._last_activity = time.monotonic()
        try:
            # Import lazily to keep the bridge/PKM operons acyclic at import time.
            from .pkm import PkmClient

            readiness = PkmClient(self).validate_readiness()
            wrapping_key = os.urandom(32)
            self.keychain.set(self._account("device-wrapping-key"), wrapping_key)
            envelope = wrap_local_vault_key(
                vault_key=vault_key,
                device_wrapping_key=wrapping_key,
                profile_id=self.identity.profile_id,
                user_id=state.user_id,
                device_id=state.device_id,
            )
            write_envelope(self.envelope_path, envelope)
            self._write_lock_state(locked=False, reason="vault_enrolled")
        except Exception:
            self.lock()
            self.keychain.delete(self._account("device-wrapping-key"))
            raise
        return {
            "enrolled": True,
            "unlocked": True,
            "contract_compatible": bool(readiness.get("compatible")),
        }

    def unlock(self, *, reason: str = "user_unlock") -> dict[str, Any]:
        state = self.identity.read_state()
        if state is None or not self.envelope_path.exists():
            raise VaultCryptoError("Complete Hussh One vault enrollment first.")
        wrapping_key = self.keychain.get(self._account("device-wrapping-key"))
        if wrapping_key is None:
            raise VaultCryptoError("The device wrapping key is unavailable.")
        envelope = read_envelope(self.envelope_path)
        if (
            envelope.user_id != state.user_id
            or envelope.device_id != state.device_id
            or envelope.profile_id != self.identity.profile_id
        ):
            raise VaultCryptoError("The local vault envelope identity does not match.")
        vault_key = unwrap_local_vault_key(
            envelope=envelope, device_wrapping_key=wrapping_key
        )
        self._write_lock_state(locked=False, reason=reason)
        with self._lock:
            self._clear_vault_key_locked()
            self._vault_key = bytearray(vault_key)
            self._last_activity = time.monotonic()
        return {"unlocked": True}

    def _write_lock_state(self, *, locked: bool, reason: str) -> None:
        payload = {
            "schema_version": 1,
            "locked": locked,
            "reason": reason,
            "updated_at_ms": int(time.time() * 1000),
        }
        self.lock_state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.lock_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.lock_state_path)

    def _profile_is_locked(self) -> bool:
        if not self.lock_state_path.exists():
            return True
        try:
            payload = json.loads(self.lock_state_path.read_text(encoding="utf-8"))
            return payload.get("locked") is not False
        except Exception:
            return True

    def _monitor_shared_lock_state(self) -> None:
        while True:
            inactive = False
            with self._lock:
                inactive = (
                    self._vault_key is not None
                    and time.monotonic() - self._last_activity
                    > self.INACTIVITY_TIMEOUT_SECONDS
                )
            if inactive:
                self.lock(reason="inactivity")
            elif self._profile_is_locked():
                with self._lock:
                    self._clear_vault_key_locked()
                self.identity.lock_identity()
            time.sleep(1)

    def _active_vault_key(self) -> bytes | None:
        with self._lock:
            if self._profile_is_locked():
                self._clear_vault_key_locked()
                return None
            if self._vault_key is None:
                return None
            if time.monotonic() - self._last_activity > self.INACTIVITY_TIMEOUT_SECONDS:
                self._clear_vault_key_locked()
                self._write_lock_state(locked=True, reason="inactivity")
                self.identity.lock_identity()
                return None
            self._last_activity = time.monotonic()
            return bytes(self._vault_key)

    def _clear_vault_key_locked(self) -> None:
        if self._vault_key is not None:
            for index in range(len(self._vault_key)):
                self._vault_key[index] = 0
        self._vault_key = None
        self._last_activity = 0.0

    def require_vault_key(self) -> bytes:
        key = self._active_vault_key()
        if key is None:
            raise VaultCryptoError(
                "Unlock the Hussh One vault in this Hermes process before saving."
            )
        return key

    def acquire_vault_owner_token(self) -> str:
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        try:
            identity_headers = self.identity.auth_headers()
        except Exception:
            self.lock()
            raise
        challenge_response = self.http.post(
            f"{state.api_base}/api/account/trusted-devices/{state.device_id}/challenge",
            headers=identity_headers,
        )
        challenge_response.raise_for_status()
        challenge = challenge_response.json()
        signature = self.identity.sign(str(challenge["signing_payload"]))
        response = self.http.post(
            f"{state.api_base}/api/consent/vault-owner-token/device",
            headers={
                **identity_headers,
                "Content-Type": "application/json",
            },
            json={
                "user_id": state.user_id,
                "device_id": state.device_id,
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": signature,
            },
        )
        if response.status_code in {401, 403}:
            self.lock()
        response.raise_for_status()
        return str(response.json()["token"])

    def lock(self, *, reason: str = "explicit") -> dict[str, Any]:
        with self._lock:
            self._clear_vault_key_locked()
        self.identity.lock_identity()
        self._write_lock_state(locked=True, reason=reason)
        return {"locked": True}

    def remove_local_vault(self) -> None:
        self.lock()
        self.keychain.delete(self._account("device-wrapping-key"))
        if self.envelope_path.exists():
            self.envelope_path.unlink()
        if self.lock_state_path.exists():
            self.lock_state_path.unlink()

    def revoke_and_disconnect(self) -> dict[str, Any]:
        state = self.identity.read_state()
        if state is not None:
            response = self.http.delete(
                f"{state.api_base}/api/account/trusted-devices/{state.device_id}",
                headers=self.identity.auth_headers(),
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
        self.remove_local_vault()
        self.identity.disconnect(remove_device_key=True)
        return {"connected": False, "revoked": state is not None}


_PROFILE_BRIDGES: dict[str, HusshVaultBridge] = {}
_PROFILE_BRIDGES_LOCK = threading.Lock()


def get_profile_bridge(profile_home: Path | None = None) -> HusshVaultBridge:
    """Return the one in-process vault authority for a Hermes profile."""
    resolved = (profile_home or get_hermes_home()).resolve()
    cache_key = str(resolved)
    with _PROFILE_BRIDGES_LOCK:
        bridge = _PROFILE_BRIDGES.get(cache_key)
        if bridge is None:
            bridge = HusshVaultBridge(profile_home=resolved)
            _PROFILE_BRIDGES[cache_key] = bridge
        return bridge
