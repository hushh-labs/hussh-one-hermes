# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Vault-derived, profile-bound encrypted storage for source-plane state."""

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
        profile_id: str,
        user_id: str,
    ) -> None:
        self._vault_key_provider = vault_key_provider
        self.profile_id = profile_id
        self.user_id = user_id

    def key(self, purpose: str) -> bytes:
        vault_key = self._vault_key_provider()
        if len(vault_key) != 32:
            raise SourceStoreError("The active vault key has an invalid length.")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"hussh-one-source-library-v1",
            info=(
                f"{purpose}\x00{self.profile_id}\x00{self.user_id}"
            ).encode("utf-8"),
        ).derive(vault_key)

    def aad(self, *, purpose: str, identifier: str) -> bytes:
        return _canonical({
            "identifier": identifier,
            "profile_id": self.profile_id,
            "purpose": purpose,
            "schema_version": 1,
            "user_id": self.user_id,
        })

    def seal(self, value: bytes, *, purpose: str, identifier: str) -> dict[str, Any]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key(purpose)).encrypt(
            nonce, value, self.aad(purpose=purpose, identifier=identifier)
        )
        return {
            "schema_version": 1,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def open(self, envelope: dict[str, Any], *, purpose: str, identifier: str) -> bytes:
        if int(envelope.get("schema_version") or 0) != 1:
            raise SourceStoreError("The encrypted source-plane version is unsupported.")
        try:
            return AESGCM(self.key(purpose)).decrypt(
                base64.b64decode(str(envelope["nonce"]), validate=True),
                base64.b64decode(str(envelope["ciphertext"]), validate=True),
                self.aad(purpose=purpose, identifier=identifier),
            )
        except Exception as exc:
            raise SourceStoreError(
                "The encrypted source-plane state failed its integrity check."
            ) from exc


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
