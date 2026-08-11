# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Vault- and device-custody-bound encrypted storage for source-plane state."""

from __future__ import annotations

import base64
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SourceStoreError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


class SourcePlaneCrypto:
    def __init__(
        self,
        *,
        vault_key_provider: Callable[[], bytes],
        device_custody_key_provider: Callable[[], bytes] | None = None,
        profile_id: str,
        user_id: str,
        device_id: str = "",
    ) -> None:
        self._vault_key_provider = vault_key_provider
        self._device_custody_key_provider = device_custody_key_provider
        self.profile_id = profile_id
        self.user_id = user_id
        self.device_id = device_id
        self._legacy_v1_allowed = True

    def _vault_key(self) -> bytes:
        vault_key = self._vault_key_provider()
        if len(vault_key) != 32:
            raise SourceStoreError("The active vault key has an invalid length.")
        return vault_key

    def _device_custody_key(self) -> bytes:
        if self._device_custody_key_provider is None:
            raise SourceStoreError(
                "Secure device custody is required before opening Source Library data."
            )
        custody_key = self._device_custody_key_provider()
        if len(custody_key) != 32:
            raise SourceStoreError("The device-custody key has an invalid length.")
        return custody_key

    def key(self, purpose: str, *, schema_version: int = 2) -> bytes:
        if schema_version == 1:
            vault_key = self._vault_key()
            salt = b"hussh-one-source-library-v1"
            key_material = vault_key
        elif schema_version == 2:
            vault_key = self._vault_key()
            salt = b"hussh-one-source-library-v2"
            # Both conditions are required to open new Source Library state:
            # the unlocked owner vault and the device-only user-presence gate.
            key_material = vault_key + self._device_custody_key()
        else:
            raise SourceStoreError("The encrypted source-plane version is unsupported.")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=(
                f"{purpose}\x00{self.profile_id}\x00{self.user_id}"
                f"\x00{self.device_id if schema_version == 2 else ''}"
            ).encode("utf-8"),
        ).derive(key_material)

    def aad(
        self, *, purpose: str, identifier: str, schema_version: int = 2
    ) -> bytes:
        return _canonical({
            "identifier": identifier,
            "device_id": self.device_id if schema_version == 2 else "",
            "profile_id": self.profile_id,
            "purpose": purpose,
            "schema_version": schema_version,
            "user_id": self.user_id,
        })

    def seal(self, value: bytes, *, purpose: str, identifier: str) -> dict[str, Any]:
        schema_version = 2
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key(purpose, schema_version=schema_version)).encrypt(
            nonce,
            value,
            self.aad(
                purpose=purpose,
                identifier=identifier,
                schema_version=schema_version,
            ),
        )
        return {
            "schema_version": schema_version,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def open(self, envelope: dict[str, Any], *, purpose: str, identifier: str) -> bytes:
        schema_version = int(envelope.get("schema_version") or 0)
        if schema_version not in {1, 2}:
            raise SourceStoreError("The encrypted source-plane version is unsupported.")
        if schema_version == 1 and not self._legacy_v1_allowed:
            raise SourceStoreError(
                "Legacy source-plane ciphertext is rejected after device-custody migration."
            )
        try:
            return AESGCM(self.key(purpose, schema_version=schema_version)).decrypt(
                base64.b64decode(str(envelope["nonce"]), validate=True),
                base64.b64decode(str(envelope["ciphertext"]), validate=True),
                self.aad(
                    purpose=purpose,
                    identifier=identifier,
                    schema_version=schema_version,
                ),
            )
        except Exception as exc:
            raise SourceStoreError(
                "The encrypted source-plane state failed its integrity check."
            ) from exc

    def reject_legacy_v1(self) -> None:
        """Fail closed if a migrated local plane is rolled back to v1."""
        self._legacy_v1_allowed = False


class EncryptedSourceStore:
    """One encrypted profile-scoped state document plus encrypted artifacts."""

    def __init__(self, profile_home: Path, crypto: SourcePlaneCrypto) -> None:
        self.root = profile_home / "hussh-one" / "source-library"
        self.state_path = self.root / "state.enc.json"
        self.artifact_root = self.root / "artifacts"
        self.crypto = crypto
        self._lock = threading.RLock()

    @staticmethod
    def empty_state() -> dict[str, Any]:
        return {
            "revision": 0,
            "bindings": {},
            "entries": {},
            "proposals": {},
            "provenance": {},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.state_path.exists():
                return self.empty_state()
            try:
                envelope = json.loads(self.state_path.read_text(encoding="utf-8"))
                value = json.loads(
                    self.crypto.open(
                        envelope, purpose="catalog", identifier="state"
                    ).decode("utf-8")
                )
            except SourceStoreError:
                raise
            except Exception as exc:
                raise SourceStoreError("The local source catalog is invalid.") from exc
            if not isinstance(value, dict):
                raise SourceStoreError("The local source catalog is invalid.")
            return value

    def save(self, value: dict[str, Any]) -> None:
        with self._lock:
            payload = dict(value)
            payload["revision"] = int(value.get("revision") or 0) + 1
            envelope = self.crypto.seal(
                _canonical(payload), purpose="catalog", identifier="state"
            )
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(_canonical(envelope).decode("utf-8"), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.state_path)

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Serialize one in-process read/modify/write transaction."""
        with self._lock:
            value = self.load()
            yield value
            self.save(value)

    def write_artifact(self, artifact_id: str, payload: dict[str, Any]) -> None:
        if not artifact_id.startswith("art_"):
            raise SourceStoreError("The artifact identifier is invalid.")
        envelope = self.crypto.seal(
            _canonical(payload), purpose="artifact", identifier=artifact_id
        )
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.artifact_root / f"{artifact_id}.enc.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(_canonical(envelope).decode("utf-8"), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def read_artifact(self, artifact_id: str) -> dict[str, Any]:
        if not artifact_id.startswith("art_"):
            raise SourceStoreError("The artifact identifier is invalid.")
        path = self.artifact_root / f"{artifact_id}.enc.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = json.loads(
                self.crypto.open(
                    envelope, purpose="artifact", identifier=artifact_id
                ).decode("utf-8")
            )
        except FileNotFoundError as exc:
            raise SourceStoreError("The local source artifact is unavailable.") from exc
        except SourceStoreError:
            raise
        except Exception as exc:
            raise SourceStoreError("The local source artifact is invalid.") from exc
        if not isinstance(payload, dict):
            raise SourceStoreError("The local source artifact is invalid.")
        return payload

    def rekey_legacy_envelopes(self) -> None:
        """Re-encrypt legacy v1 local state under the device-custody v2 key.

        The old envelope is retained only until this succeeds; every rewrite is
        atomic, so an interruption leaves a readable v1 or v2 envelope rather
        than plaintext or a partially written document.
        """
        with self._lock:
            if self.state_path.exists():
                state = self.load()
                self.save(state)
            if not self.artifact_root.exists():
                return
            for path in sorted(self.artifact_root.glob("art_*.enc.json")):
                artifact_id = path.name.removesuffix(".enc.json")
                payload = self.read_artifact(artifact_id)
                self.write_artifact(artifact_id, payload)
