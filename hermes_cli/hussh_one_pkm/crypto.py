# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Crypto-compatible helpers for the existing Hussh web vault contract."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_PBKDF2_ITERATIONS = 100_000


class VaultCryptoError(ValueError):
    pass


def decode_binary(value: str) -> bytes:
    raw = value.strip()
    if (
        raw
        and len(raw) % 2 == 0
        and _HEX_RE.fullmatch(raw)
        and not re.search(r"[+/=_-]", raw)
    ):
        return bytes.fromhex(raw)
    padded = raw.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except ValueError as exc:
        raise VaultCryptoError("Unsupported encoded binary format.") from exc


def derive_passphrase_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )


def unwrap_passphrase_vault_key(
    *, passphrase: str, encrypted_vault_key: str, salt: str, iv: str
) -> bytes:
    try:
        key = derive_passphrase_key(passphrase, decode_binary(salt))
        vault_key = AESGCM(key).decrypt(
            decode_binary(iv),
            decode_binary(encrypted_vault_key),
            None,
        )
    except Exception as exc:
        raise VaultCryptoError("The vault passphrase is incorrect.") from exc
    if len(vault_key) != 32:
        raise VaultCryptoError("The unwrapped vault key has an invalid length.")
    return vault_key


def vault_key_hash(vault_key: bytes) -> str:
    # Existing web contract hashes the lower-case 64-character key hex string.
    return hashlib.sha256(vault_key.hex().encode("utf-8")).hexdigest()


def envelope_aad(*, profile_id: str, user_id: str, device_id: str) -> bytes:
    return json.dumps(
        {
            "device_id": device_id,
            "profile_id": profile_id,
            "purpose": "hussh-one-local-vault-envelope-v1",
            "user_id": user_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class LocalVaultEnvelope:
    schema_version: int
    user_id: str
    device_id: str
    profile_id: str
    iv: str
    ciphertext: str
    vault_key_hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "iv": self.iv,
            "ciphertext": self.ciphertext,
            "vault_key_hash": self.vault_key_hash,
        }


def wrap_local_vault_key(
    *,
    vault_key: bytes,
    device_wrapping_key: bytes,
    profile_id: str,
    user_id: str,
    device_id: str,
) -> LocalVaultEnvelope:
    if len(device_wrapping_key) != 32:
        raise VaultCryptoError("The device wrapping key has an invalid length.")
    iv = os.urandom(12)
    ciphertext = AESGCM(device_wrapping_key).encrypt(
        iv,
        vault_key,
        envelope_aad(profile_id=profile_id, user_id=user_id, device_id=device_id),
    )
    return LocalVaultEnvelope(
        schema_version=1,
        user_id=user_id,
        device_id=device_id,
        profile_id=profile_id,
        iv=base64.b64encode(iv).decode("ascii"),
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        vault_key_hash=vault_key_hash(vault_key),
    )


def unwrap_local_vault_key(
    *, envelope: LocalVaultEnvelope, device_wrapping_key: bytes
) -> bytes:
    if envelope.schema_version != 1:
        raise VaultCryptoError("The local vault envelope version is unsupported.")
    if len(device_wrapping_key) != 32:
        raise VaultCryptoError("The device wrapping key has an invalid length.")
    try:
        vault_key = AESGCM(device_wrapping_key).decrypt(
            base64.b64decode(envelope.iv, validate=True),
            base64.b64decode(envelope.ciphertext, validate=True),
            envelope_aad(
                profile_id=envelope.profile_id,
                user_id=envelope.user_id,
                device_id=envelope.device_id,
            ),
        )
    except Exception as exc:
        raise VaultCryptoError("The local vault envelope could not be opened.") from exc
    if vault_key_hash(vault_key) != envelope.vault_key_hash:
        raise VaultCryptoError("The local vault envelope failed its integrity check.")
    return vault_key


def write_envelope(path: Path, envelope: LocalVaultEnvelope) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(envelope.to_json(), separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_envelope(path: Path) -> LocalVaultEnvelope:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LocalVaultEnvelope(
            schema_version=int(payload["schema_version"]),
            user_id=str(payload["user_id"]),
            device_id=str(payload["device_id"]),
            profile_id=str(payload["profile_id"]),
            iv=str(payload["iv"]),
            ciphertext=str(payload["ciphertext"]),
            vault_key_hash=str(payload["vault_key_hash"]),
        )
    except Exception as exc:
        raise VaultCryptoError("The local vault envelope is invalid.") from exc
