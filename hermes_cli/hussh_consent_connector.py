# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Trusted device-key custody for the hosted Hussh Consent MCP lifecycle."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_constants import get_hermes_home

_WRAPPING_ALG = "X25519-AES256-GCM"
_KEYPAIR_FILE = "connector_keypair.json"


def _state_dir() -> Path:
    return Path(get_hermes_home()) / "hussh-consent"


def _decode(value: str) -> bytes:
    normalized = str(value or "").strip().replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized, validate=True)


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _load_or_create_keypair() -> tuple[X25519PrivateKey, str, str]:
    directory = _state_dir()
    path = directory / _KEYPAIR_FILE
    try:
        record = json.loads(path.read_text())
        private = X25519PrivateKey.from_private_bytes(_decode(record["private_key_b64"]))
        return private, str(record["public_key_b64"]), str(record["key_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass

    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    record = {
        "private_key_b64": _encode(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        "public_key_b64": _encode(public),
        "key_id": f"hussh-one-{uuid4().hex[:12]}",
        "wrapping_alg": _WRAPPING_ALG,
    }
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)
    fd, temporary = tempfile.mkstemp(prefix=".connector-key-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return private, record["public_key_b64"], record["key_id"]


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _decrypt_export(payload: dict[str, Any], expected_scope: str) -> dict[str, Any]:
    if payload.get("delivery") == "encrypted_inline":
        crypto = payload.get("crypto")
        if isinstance(crypto, dict):
            payload = {
                "encrypted_data": payload.get("ciphertext"),
                "iv": crypto.get("iv"),
                "tag": crypto.get("tag"),
                "wrapped_key_bundle": crypto.get("wrapped_key_bundle"),
                "export_envelope": crypto.get("export_envelope"),
            }
        else:
            try:
                envelope = json.loads(str(payload.get("export_envelope_json") or ""))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "The approved export is missing its protected envelope."
                ) from exc
            payload = {
                "encrypted_data": payload.get("ciphertext"),
                "iv": payload.get("payload_iv"),
                "tag": payload.get("payload_tag"),
                "wrapped_key_bundle": {
                    "connector_key_id": payload.get("connector_key_id"),
                    "sender_public_key": payload.get("sender_public_key"),
                    "wrapped_key_iv": payload.get("wrapped_key_iv"),
                    "wrapped_export_key": payload.get("wrapped_export_key"),
                    "wrapped_key_tag": payload.get("wrapped_key_tag"),
                    "wrapping_alg": payload.get("wrapping_alg"),
                },
                "export_envelope": envelope,
            }
    private, _, key_id = _load_or_create_keypair()
    wrapped = payload.get("wrapped_key_bundle")
    envelope = payload.get("export_envelope")
    if not isinstance(wrapped, dict) or not isinstance(envelope, dict):
        raise RuntimeError("The approved export is missing its protected envelope.")
    if str(wrapped.get("connector_key_id") or "") != key_id:
        raise RuntimeError("This grant belongs to a different trusted connector.")
    if str(wrapped.get("wrapping_alg") or _WRAPPING_ALG) != _WRAPPING_ALG:
        raise RuntimeError("The approved export uses an unsupported wrapping algorithm.")
    if int(envelope.get("version") or 0) != 2:
        raise RuntimeError("The approved export uses an unsupported envelope version.")

    ciphertext = _decode(str(payload.get("encrypted_data") or ""))
    aad = envelope.get("aad")
    if not isinstance(aad, dict):
        raise RuntimeError("The approved export has invalid authenticated metadata.")
    if str(envelope.get("export_id") or "") != str(aad.get("export_id") or ""):
        raise RuntimeError("The approved export failed identifier validation.")
    if str(aad.get("machine_scope") or "") != expected_scope:
        raise RuntimeError("The approved export does not match the requested scope.")
    if envelope.get("aad_sha256") != _digest(_canonical(aad)):
        raise RuntimeError("The approved export failed metadata validation.")
    if envelope.get("ciphertext_sha256") != _digest(ciphertext):
        raise RuntimeError("The approved export failed ciphertext validation.")
    if int(envelope.get("ciphertext_bytes") or 0) != len(ciphertext):
        raise RuntimeError("The approved export failed size validation.")

    sender = X25519PublicKey.from_public_bytes(
        _decode(str(wrapped["sender_public_key"]))
    )
    shared = private.exchange(sender)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(shared)
    wrapping_key = digest.finalize()
    export_key = AESGCM(wrapping_key).decrypt(
        _decode(str(wrapped["wrapped_key_iv"])),
        _decode(str(wrapped["wrapped_export_key"]))
        + _decode(str(wrapped["wrapped_key_tag"])),
        _canonical(envelope),
    )
    plaintext = AESGCM(export_key).decrypt(
        _decode(str(payload["iv"])),
        ciphertext + _decode(str(payload["tag"])),
        _canonical(aad),
    )
    decoded = json.loads(plaintext)
    if not isinstance(decoded, dict):
        raise RuntimeError("The approved export did not contain an object.")
    return _narrow(decoded, expected_scope)


def _narrow(payload: dict[str, Any], scope: str) -> dict[str, Any]:
    parts = scope.split(".")
    if len(parts) < 3 or parts[0] != "attr" or parts[-1] != "*":
        raise RuntimeError("The approved scope cannot be projected safely.")
    domain, path = parts[1], parts[2:-1]
    value: Any = payload.get(domain)
    for segment in path:
        if not isinstance(value, dict) or segment not in value:
            return {domain: {}}
        value = value[segment]
    projected: Any = copy.deepcopy(value)
    for segment in reversed(path):
        projected = {segment: projected}
    return {domain: projected}
