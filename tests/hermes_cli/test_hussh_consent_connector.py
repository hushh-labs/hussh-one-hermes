from __future__ import annotations

import json
import os
import stat

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cli import hussh_consent_connector as connector
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _sha256(value: bytes) -> bytes:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(value)
    return digest.finalize()


def test_connector_key_is_profile_private_and_stable(tmp_path) -> None:
    token = set_hermes_home_override(tmp_path)
    try:
        first = connector._load_or_create_keypair()
        second = connector._load_or_create_keypair()
        key_file = tmp_path / "hussh-consent" / "connector_keypair.json"

        assert first[1:] == second[1:]
        assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    finally:
        reset_hermes_home_override(token)


def test_export_is_authenticated_decrypted_and_scope_narrowed(tmp_path) -> None:
    token = set_hermes_home_override(tmp_path)
    try:
        connector_private, connector_public_b64, key_id = (
            connector._load_or_create_keypair()
        )
        sender_private = X25519PrivateKey.generate()
        sender_public = sender_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        shared = sender_private.exchange(connector_private.public_key())
        wrapping_key = _sha256(shared)
        export_key = os.urandom(32)
        plaintext = json.dumps(
            {
                "financial": {
                    "portfolio": {"total_value": 125000},
                    "documents": {"count": 99},
                }
            }
        ).encode()
        aad = {
            "version": 2,
            "app_id": "app-test",
            "grant_id": "grant-test",
            "export_id": "e" * 32,
            "revision": 1,
            "machine_scope": "attr.financial.portfolio.*",
            "scope_handle": "s_123456",
            "recipient_key_fingerprint": f"sha256:{'a' * 64}",
            "payload_algorithm": "AES-256-GCM",
            "expires_at_ms": 9999999999999,
        }
        payload_iv = os.urandom(12)
        encrypted = AESGCM(export_key).encrypt(
            payload_iv,
            plaintext,
            connector._canonical(aad),
        )
        ciphertext, tag = encrypted[:-16], encrypted[-16:]
        envelope = {
            "version": 2,
            "export_id": aad["export_id"],
            "aad": aad,
            "aad_sha256": connector._digest(connector._canonical(aad)),
            "ciphertext_sha256": connector._digest(ciphertext),
            "ciphertext_bytes": len(ciphertext),
        }
        wrap_iv = os.urandom(12)
        wrapped = AESGCM(wrapping_key).encrypt(
            wrap_iv,
            export_key,
            connector._canonical(envelope),
        )
        result = connector._decrypt_export(
            {
                "delivery": "encrypted_inline",
                "ciphertext": connector._encode(ciphertext),
                "payload_iv": connector._encode(payload_iv),
                "payload_tag": connector._encode(tag),
                "export_envelope_json": json.dumps(envelope),
                "connector_key_id": key_id,
                "sender_public_key": connector._encode(sender_public),
                "wrapped_key_iv": connector._encode(wrap_iv),
                "wrapped_export_key": connector._encode(wrapped[:-16]),
                "wrapped_key_tag": connector._encode(wrapped[-16:]),
                "wrapping_alg": "X25519-AES256-GCM",
            },
            "attr.financial.portfolio.*",
        )

        assert result == {"financial": {"portfolio": {"total_value": 125000}}}
        assert "documents" not in json.dumps(result)
        assert connector_public_b64
    finally:
        reset_hermes_home_override(token)
