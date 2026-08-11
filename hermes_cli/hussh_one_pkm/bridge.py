# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Profile-scoped local vault bridge for trusted Hermes installations."""

from __future__ import annotations

import base64
import json
import os
import plistlib
import shutil
import subprocess
import sys
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
from .replica import EncryptedPkmReplica


class HusshVaultBridge:
    """Owns identity, vault memory, and device-bound owner capabilities."""

    DEVICE_SYNC_INTERVAL_SECONDS = 30
    WORKSTATION_LOCK_CHECK_SECONDS = 5

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
        self.replica = EncryptedPkmReplica(self.profile_home)
        self._vault_key: bytearray | None = None
        self._source_library_custody_key: bytearray | None = None
        self._source_library_custody_phase: int | None = None
        self._last_activity = 0.0
        self._owner_token: bytearray | None = None
        self._owner_token_expires_at_ms = 0
        self._next_device_sync_at = 0.0
        self._next_workstation_lock_check_at = 0.0
        self._last_device_sync_at_ms = 0
        self._device_sync_status = "idle"
        self._lock = threading.RLock()
        self._onboarding_status = "idle"
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
        connection_state = "not_connected"
        if state is not None:
            connection_state = "connected" if state.account_email else "reconnect_required"
        return {
            "connected": state is not None,
            "connection_state": connection_state,
            "environment": state.environment if state else "uat",
            "device_id": state.device_id if state else None,
            "account_email": state.account_email if state else None,
            "profile_id": self.identity.profile_id,
            "onboarding_status": self._onboarding_status,
        }

    def begin_onboarding(self, *, device_name: str) -> dict[str, Any]:
        """Open One's browser approval and continue vault setup locally.

        A legacy state without a verified email is deliberately repaired by a
        fresh browser approval. It is not trusted for vault access while the
        repair is pending, and the server atomically replaces its device row.
        """
        state = self.identity.read_state()
        if state is not None and state.account_email:
            raise VaultCryptoError(
                "This Hermes profile is already connected. Disconnect it before choosing another account."
            )
        self._onboarding_status = "waiting_for_browser_approval"
        return self.identity.open_authorization(
            device_name=device_name,
            replaces_device_id=state.device_id if state is not None else None,
            on_connected=self._continue_native_enrollment,
        )

    def _continue_native_enrollment(self, passkey_vault_key: bytes | None = None) -> None:
        """Run the password/recovery ceremony outside chat after browser approval."""
        if self.envelope_path.exists():
            self._onboarding_status = "ready"
            return
        try:
            from .native_prompt import (
                disclose_recovery_key,
                prompt_for_new_vault_passphrase,
                prompt_for_vault_passphrase,
            )

            self._onboarding_status = "awaiting_native_vault_prompt"
            vault_exists = self.vault_preflight().get("vault_exists")
            if vault_exists and passkey_vault_key is not None:
                try:
                    result = self.enroll_vault_key(passkey_vault_key)
                except Exception:
                    # A passkey is only a quick-unlock optimization. If the
                    # wrapper, handoff, integrity check, or readiness probe is
                    # no longer valid, immediately continue with the existing
                    # native masked passphrase flow.
                    pass
                else:
                    self._onboarding_status = (
                        "ready"
                        if result.get("contract_compatible")
                        else "contract_incompatible"
                    )
                    return
            passphrase = (
                prompt_for_vault_passphrase()
                if vault_exists
                else prompt_for_new_vault_passphrase()
            )
            if passphrase is None:
                self._onboarding_status = "vault_setup_canceled"
                return
            if not passphrase:
                self._onboarding_status = "vault_setup_requires_passphrase"
                return
            result = (
                self.enroll_vault(passphrase)
                if vault_exists
                else self.create_vault(passphrase, disclose_recovery=disclose_recovery_key)
            )
            self._onboarding_status = (
                "ready" if result.get("contract_compatible") else "contract_incompatible"
            )
        except Exception:
            # Native prompts and vault operations must not leak details into
            # the terminal transcript. /hussh-one status exposes only this
            # bounded recovery state.
            self._onboarding_status = "vault_setup_needs_retry"

    def enroll_vault_key(self, vault_key: bytes) -> dict[str, Any]:
        """Secure an already-unwrapped vault key received from a local passkey ceremony."""
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        if len(vault_key) != 32:
            raise VaultCryptoError("The passkey vault key has an invalid format.")
        response = self.http.post(
            f"{state.api_base}/db/vault/get",
            headers=self.identity.auth_headers(),
            json={"userId": state.user_id},
        )
        response.raise_for_status()
        expected_hash = str(response.json().get("vaultKeyHash") or "")
        if not expected_hash or vault_key_hash(vault_key) != expected_hash:
            raise VaultCryptoError("The passkey vault key failed its integrity check.")
        return self._finish_vault_enrollment(
            state=state,
            vault_key=vault_key,
            expected_hash=expected_hash,
        )

    def vault_status(self) -> dict[str, Any]:
        state = self.identity.read_state()
        return {
            "connected": state is not None,
            "enrolled": self.envelope_path.exists(),
            "unlocked": self._active_vault_key() is not None,
            "profile_locked": self._profile_is_locked(),
            "device_id": state.device_id if state else None,
            "profile_id": self.identity.profile_id,
            "custody_mode": "trusted_device_until_lock_or_revoke",
            "source_library_custody_mode": "device_only_user_presence",
            "source_library_custody_unlocked": self._source_library_custody_key
            is not None,
            "owner_capability_mode": "automatic_short_lived",
            "encrypted_replica_cursor": self.replica.cursor(),
            "device_sync_status": self._device_sync_status,
            "last_device_sync_at_ms": self._last_device_sync_at_ms or None,
        }

    def vault_preflight(self) -> dict[str, Any]:
        """Return whether the authenticated Hussh account already has a vault."""
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        if not state.account_email:
            raise VaultCryptoError("Reconnect Hussh One to verify this account before vault setup.")
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
            if self._profile_is_locked():
                with self._lock:
                    self._clear_vault_key_locked()
                    self._clear_source_library_custody_key_locked()
                    self._clear_owner_token_locked()
                self.identity.lock_identity()
            elif time.monotonic() >= self._next_workstation_lock_check_at:
                self._next_workstation_lock_check_at = (
                    time.monotonic() + self.WORKSTATION_LOCK_CHECK_SECONDS
                )
                if self._macos_console_locked():
                    self.lock(reason="workstation_lock")
            elif time.monotonic() >= self._next_device_sync_at:
                self._next_device_sync_at = (
                    time.monotonic() + self.DEVICE_SYNC_INTERVAL_SECONDS
                )
                try:
                    from .pkm import PkmClient

                    PkmClient(self).sync_encrypted_replica()
                    self._device_sync_status = "current"
                    self._last_device_sync_at_ms = int(time.time() * 1000)
                except Exception:
                    # Background sync is opportunistic. Status and explicit
                    # writes surface bounded actionable errors without placing
                    # credentials or decrypted information in logs.
                    self._device_sync_status = "retry_pending"
            time.sleep(1)

    @staticmethod
    def _macos_console_locked() -> bool:
        if sys.platform != "darwin":
            return False
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"],
                check=True,
                capture_output=True,
                timeout=3,
            )
            payload = plistlib.loads(result.stdout)
            if isinstance(payload, dict):
                return payload.get("IOConsoleLocked") is True
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0].get("IOConsoleLocked") is True
            return False
        except (
            KeyError,
            OSError,
            subprocess.SubprocessError,
            plistlib.InvalidFileException,
        ):
            return False

    def _active_vault_key(self) -> bytes | None:
        with self._lock:
            if self._macos_console_locked():
                self._clear_vault_key_locked()
                self._clear_source_library_custody_key_locked()
                self._clear_owner_token_locked()
                self._write_lock_state(locked=True, reason="workstation_lock")
                self.identity.lock_identity()
                return None
            if self._profile_is_locked():
                self._clear_vault_key_locked()
                self._clear_source_library_custody_key_locked()
                return None
            if self._vault_key is None:
                try:
                    self._restore_vault_key_locked()
                except Exception:
                    return None
            if self._vault_key is None:
                return None
            self._last_activity = time.monotonic()
            return bytes(self._vault_key)

    def _restore_vault_key_locked(self) -> None:
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
            envelope=envelope,
            device_wrapping_key=wrapping_key,
        )
        self._vault_key = bytearray(vault_key)
        self._last_activity = time.monotonic()

    def _clear_vault_key_locked(self) -> None:
        if self._vault_key is not None:
            for index in range(len(self._vault_key)):
                self._vault_key[index] = 0
        self._vault_key = None
        self._last_activity = 0.0

    def _clear_source_library_custody_key_locked(self) -> None:
        if self._source_library_custody_key is not None:
            for index in range(len(self._source_library_custody_key)):
                self._source_library_custody_key[index] = 0
        self._source_library_custody_key = None
        self._source_library_custody_phase = None

    def _clear_owner_token_locked(self) -> None:
        if self._owner_token is not None:
            for index in range(len(self._owner_token)):
                self._owner_token[index] = 0
        self._owner_token = None
        self._owner_token_expires_at_ms = 0

    def require_vault_key(self) -> bytes:
        key = self._active_vault_key()
        if key is None:
            raise VaultCryptoError(
                "Unlock the Hussh One vault in this Hermes process before saving."
            )
        return key

    def require_source_library_custody_key(
        self, *, create_if_missing: bool = False
    ) -> bytes:
        """Return the local Source Library gate only with owner vault + user presence.

        This is deliberately separate from the trusted-device wrapping key.
        A local Source Library ciphertext needs both the unlocked canonical
        vault key and this device-only Keychain item; neither secret is stored
        in the Source Library database, artifact files, or model context.
        """
        self.require_vault_key()
        with self._lock:
            if self._source_library_custody_key is not None:
                return bytes(self._source_library_custody_key)
        account = self._account("source-library-custody-key")
        getter = getattr(self.keychain, "get_user_presence_secret", None)
        setter = getattr(self.keychain, "set_user_presence_secret", None)
        if not callable(getter) or not callable(setter):
            raise VaultCryptoError(
                "This Hermes installation cannot provide device-only Source Library custody."
            )
        # Do not hold the bridge lock while macOS presents LocalAuthentication.
        material = getter(account, prompt="Unlock Hussh One Source Library on this Mac")
        if material is None:
            if not create_if_missing:
                raise VaultCryptoError(
                    "The Source Library custody key is unavailable. Rebuild the local index only after explicitly re-binding empty sources."
                )
            setter(account, b"\x01" + os.urandom(32))
            # Re-read through the protected path so first use is not reported
            # as user-presence protected merely because this process generated
            # the secret.
            material = getter(
                account, prompt="Unlock Hussh One Source Library on this Mac"
            )
        if material is None or len(material) != 33 or material[0] not in {1, 2}:
            raise VaultCryptoError("The Source Library custody key is invalid.")
        phase = int(material[0])
        key = material[1:]
        with self._lock:
            # A lock, revocation, or profile switch that raced the native prompt
            # must win over caching the recovered secret.
            if self._profile_is_locked() or self._vault_key is None:
                raise VaultCryptoError("The Hussh One vault locked during Source Library unlock.")
            self._source_library_custody_key = bytearray(key)
            self._source_library_custody_phase = phase
            return bytes(self._source_library_custody_key)

    def source_library_custody_phase(self) -> int:
        self.require_source_library_custody_key()
        with self._lock:
            if self._source_library_custody_phase is None:
                raise VaultCryptoError("The Source Library custody key is unavailable.")
            return self._source_library_custody_phase

    def complete_source_library_custody_upgrade(self) -> None:
        """Latch v2 after all legacy ciphertext is atomically re-encrypted."""
        self.require_source_library_custody_key()
        with self._lock:
            if self._source_library_custody_phase == 2:
                return
            if self._source_library_custody_key is None:
                raise VaultCryptoError("The Source Library custody key is unavailable.")
            material = b"\x02" + bytes(self._source_library_custody_key)
        setter = getattr(self.keychain, "set_user_presence_secret", None)
        if not callable(setter):
            raise VaultCryptoError(
                "This Hermes installation cannot update device-only Source Library custody."
            )
        setter(self._account("source-library-custody-key"), material)
        with self._lock:
            if self._source_library_custody_key is None or self._profile_is_locked():
                raise VaultCryptoError("The Hussh One vault locked during Source Library upgrade.")
            self._source_library_custody_phase = 2

    def acquire_vault_owner_token(self) -> str:
        state = self.identity.read_state()
        if state is None:
            raise VaultCryptoError("Connect this Hermes profile to Hussh One first.")
        with self._lock:
            if (
                self._owner_token is not None
                and self._owner_token_expires_at_ms > int(time.time() * 1000) + 30_000
            ):
                return bytes(self._owner_token).decode("utf-8")
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
        payload = response.json()
        token = str(payload["token"])
        with self._lock:
            self._clear_owner_token_locked()
            self._owner_token = bytearray(token.encode("utf-8"))
            self._owner_token_expires_at_ms = int(payload.get("expiresAt") or 0)
        return token

    def lock(self, *, reason: str = "explicit") -> dict[str, Any]:
        with self._lock:
            self._clear_vault_key_locked()
            self._clear_source_library_custody_key_locked()
            self._clear_owner_token_locked()
        self.identity.lock_identity()
        self._write_lock_state(locked=True, reason=reason)
        return {"locked": True}

    def remove_local_vault(self) -> None:
        self.lock()
        self.keychain.delete(self._account("device-wrapping-key"))
        delete_custody = getattr(self.keychain, "delete_user_presence_secret", None)
        if callable(delete_custody):
            try:
                delete_custody(self._account("source-library-custody-key"))
            except Exception:
                # An owner can cancel a Keychain deletion prompt after remote
                # revocation. Source ciphertext is still removed below and the
                # surviving device-only secret cannot open a deleted envelope.
                pass
        if self.envelope_path.exists():
            self.envelope_path.unlink()
        if self.lock_state_path.exists():
            self.lock_state_path.unlink()
        self.replica.clear()
        source_library_root = self.profile_home / "hussh-one" / "source-library"
        if source_library_root.exists():
            shutil.rmtree(source_library_root)

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
