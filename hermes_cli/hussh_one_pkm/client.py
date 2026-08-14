# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""UAT identity and first-party Hussh API client for the local bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .keychain import MacOSKeychain

UAT_API_BASE = "https://api.uat.hushh.ai"
UAT_WEB_BASE = "https://uat.one.hushh.ai"
# Firebase web API keys are public project identifiers, not credentials. The
# refresh credential created with this key remains Keychain-only.
UAT_FIREBASE_WEB_API_KEY = "AIzaSyAJ0RDWrBYF6yIDvwdyoAVO4K5QkL218Yc"
VAULT_HANDOFF_ALGORITHM = "X25519-AES256-GCM"
VAULT_HANDOFF_VERSION = "hussh-one-trusted-device-vault-handoff-v1"


class HusshIdentityError(RuntimeError):
    pass


def _profile_id(profile_home: Path) -> str:
    return hashlib.sha256(str(profile_home.resolve()).encode("utf-8")).hexdigest()[:24]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


@dataclass(frozen=True)
class IdentityState:
    user_id: str
    device_id: str
    profile_id: str
    account_email: str
    api_base: str = UAT_API_BASE
    web_base: str = UAT_WEB_BASE
    environment: str = "uat"

    def to_json(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "account_email": self.account_email,
            "api_base": self.api_base,
            "web_base": self.web_base,
            "environment": self.environment,
        }


class HusshIdentityClient:
    def __init__(
        self,
        *,
        profile_home: Path,
        keychain: MacOSKeychain | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.profile_home = profile_home
        self.profile_id = _profile_id(profile_home)
        self.state_dir = profile_home / "hussh-one"
        self.identity_path = self.state_dir / "identity.json"
        self.keychain = keychain or MacOSKeychain()
        self.http = http or httpx.Client(timeout=30.0)
        self._id_token: str | None = None
        self._id_token_expires_at = 0.0
        self._pending: dict[str, Any] | None = None
        self._pending_lock = threading.Lock()

    def _account(self, kind: str) -> str:
        return f"{self.profile_id}:{kind}"

    def read_state(self) -> IdentityState | None:
        if not self.identity_path.exists():
            return None
        try:
            payload = json.loads(self.identity_path.read_text(encoding="utf-8"))
            state = IdentityState(
                user_id=str(payload["user_id"]),
                device_id=str(payload["device_id"]),
                profile_id=str(payload["profile_id"]),
                account_email=str(payload.get("account_email") or "").strip(),
                api_base=str(payload.get("api_base") or UAT_API_BASE),
                web_base=str(payload.get("web_base") or UAT_WEB_BASE),
                environment=str(payload.get("environment") or "uat"),
            )
        except Exception as exc:
            raise HusshIdentityError(
                "The local Hussh identity state is invalid."
            ) from exc
        if state.profile_id != self.profile_id:
            raise HusshIdentityError(
                "The Hussh identity belongs to another Hermes profile."
            )
        return state

    def _device_private_key(self) -> ec.EllipticCurvePrivateKey:
        encoded = self.keychain.get(self._account("device-signing-key"))
        if encoded is None:
            private_key = ec.generate_private_key(ec.SECP256R1())
            encoded = private_key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self.keychain.set(self._account("device-signing-key"), encoded)
            return private_key
        key = serialization.load_der_private_key(encoded, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise HusshIdentityError(
                "The stored trusted-device signing key is invalid."
            )
        return key

    def public_key_b64(self) -> str:
        public_der = (
            self
            ._device_private_key()
            .public_key()
            .public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        return base64.b64encode(public_der).decode("ascii")

    def start_authorization(
        self,
        *,
        device_name: str,
        replaces_device_id: str | None = None,
        on_connected: Callable[[bytes | None], None] | None = None,
    ) -> dict[str, Any]:
        verifier = secrets.token_urlsafe(64)[:86]
        challenge = (
            base64
            .urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        state = secrets.token_urlsafe(32)
        # Prove secure device-key storage before opening a callback listener or
        # publishing any pending authorization state.
        device_public_key = self.public_key_b64()
        vault_handoff_private_key = x25519.X25519PrivateKey.generate()
        vault_handoff_public_key = base64.b64encode(
            vault_handoff_private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        server.expected_state = state  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]
        server.callback_event = threading.Event()  # type: ignore[attr-defined]
        server.completion_event = threading.Event()  # type: ignore[attr-defined]
        server.completion_status = "pending"  # type: ignore[attr-defined]

        with self._pending_lock:
            if self._pending is not None:
                if self._pending.get("status") == "waiting":
                    server.server_close()
                    raise HusshIdentityError(
                        "A trusted-device authorization is already in progress."
                    )
                self._pending = None
            self._pending = {
                "server": server,
                "verifier": verifier,
                "state": state,
                "status": "waiting",
                "error": None,
                "on_connected": on_connected,
                "vault_handoff_private_key": vault_handoff_private_key,
            }

        request_params = {
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "device_public_key": device_public_key,
            "device_name": device_name.strip() or "Hermes on Mac",
            "platform": "macos",
            "state": state,
            "vault_handoff_public_key": vault_handoff_public_key,
        }
        if replaces_device_id:
            request_params["replaces_device_id"] = replaces_device_id
        params = urllib.parse.urlencode(request_params)
        authorization_url = (
            f"{UAT_WEB_BASE}/one/profile/security/devices/authorize?{params}"
        )
        threading.Thread(
            target=self._serve_authorization,
            args=(server, verifier),
            daemon=True,
        ).start()
        return {
            "status": "waiting",
            "authorization_url": authorization_url,
            "expires_in": 300,
        }

    def open_authorization(
        self,
        *,
        device_name: str,
        replaces_device_id: str | None = None,
        on_connected: Callable[[bytes | None], None] | None = None,
    ) -> dict[str, Any]:
        result = self.start_authorization(
            device_name=device_name,
            replaces_device_id=replaces_device_id,
            on_connected=on_connected,
        )
        webbrowser.open(result["authorization_url"])
        return result

    def authorization_status(self) -> dict[str, Any]:
        with self._pending_lock:
            if self._pending is None:
                state = self.read_state()
                return {"status": "connected" if state else "idle"}
            return {
                "status": self._pending["status"],
                "error": self._pending.get("error"),
            }

    def cancel_pending_authorization(self) -> bool:
        """Invalidate a browser grant before disconnecting or switching accounts."""
        with self._pending_lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return False
        server = pending.get("server")
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        return True

    def _serve_authorization(self, server: ThreadingHTTPServer, verifier: str) -> None:
        try:
            server.timeout = 300
            server.handle_request()
            callback_event = getattr(server, "callback_event", None)
            if callback_event is not None:
                callback_event.wait(timeout=5)
            result = getattr(server, "result", None)
            if not result or not result.get("code"):
                raise HusshIdentityError(
                    result.get("error") if result else "Authorization timed out."
                )
            exchange = self.http.post(
                f"{UAT_API_BASE}/api/account/trusted-device-authorizations/exchange",
                json={"code": result["code"], "code_verifier": verifier},
            )
            exchange.raise_for_status()
            payload = exchange.json()
            firebase = self.http.post(
                "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
                params={"key": UAT_FIREBASE_WEB_API_KEY},
                json={
                    "token": payload["firebase_custom_token"],
                    "returnSecureToken": True,
                },
            )
            firebase.raise_for_status()
            session = firebase.json()
            state = IdentityState(
                user_id=str(payload["user_id"]),
                device_id=str(payload["device_id"]),
                profile_id=self.profile_id,
                account_email=str(payload.get("account_email") or "").strip(),
            )
            if not state.account_email:
                raise HusshIdentityError(
                    "The trusted-device exchange did not provide a verified account email."
                )
            with self._pending_lock:
                if self._pending is None or self._pending.get("server") is not server:
                    return
                refresh_token = str(session["refreshToken"]).encode("utf-8")
                self.keychain.set(
                    self._account("firebase-refresh-token"), refresh_token
                )
                self._id_token = str(session["idToken"])
                self._id_token_expires_at = (
                    time.time() + int(session.get("expiresIn") or 3600) - 60
                )
                _atomic_json(self.identity_path, state.to_json())
            vault_key: bytes | None = None
            try:
                with self._pending_lock:
                    pending_private_key = None
                    if (
                        self._pending is not None
                        and self._pending.get("server") is server
                    ):
                        pending_private_key = self._pending.get(
                            "vault_handoff_private_key"
                        )
                if isinstance(pending_private_key, x25519.X25519PrivateKey):
                    recipient_public_key = base64.b64encode(
                        pending_private_key.public_key().public_bytes(
                            serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw,
                        )
                    ).decode("ascii")
                    vault_key = _decrypt_vault_handoff(
                        result=payload.get("vault_handoff"),
                        recipient_private_key=pending_private_key,
                        state=str(result.get("state") or ""),
                        device_id=state.device_id,
                        user_id=state.user_id,
                        authorization_id=str(payload.get("authorization_id") or ""),
                        expires_at=int(payload.get("authorization_expires_at") or 0),
                        environment=state.environment,
                        recipient_public_key=recipient_public_key,
                    )
            except Exception:
                # The passkey handoff is an optimization, never an identity
                # dependency. A canceled, unavailable, or malformed handoff
                # falls back to the native masked passphrase ceremony.
                vault_key = None
            callback: Callable[[bytes | None], None] | None = None
            with self._pending_lock:
                if self._pending is None or self._pending.get("server") is not server:
                    return
                self._pending["status"] = "connected"
                callback = self._pending.get("on_connected")
            server.completion_status = "connected"  # type: ignore[attr-defined]
            completion_event = getattr(server, "completion_event", None)
            if completion_event is not None:
                completion_event.set()
            if callback is not None:
                callback(vault_key)
        except Exception as exc:
            with self._pending_lock:
                if (
                    self._pending is not None
                    and self._pending.get("server") is server
                ):
                    self._pending["status"] = "error"
                    self._pending["error"] = str(exc)
            server.completion_status = "error"  # type: ignore[attr-defined]
            completion_event = getattr(server, "completion_event", None)
            if completion_event is not None:
                completion_event.set()
        finally:
            # The handoff key is an enrollment-only capability. Retain the
            # bounded status for /hussh-one status, but remove private key
            # material as soon as this authorization attempt terminates.
            with self._pending_lock:
                if (
                    self._pending is not None
                    and self._pending.get("server") is server
                ):
                    self._pending.pop("vault_handoff_private_key", None)
            completion_event = getattr(server, "completion_event", None)
            if completion_event is not None:
                completion_event.set()
            server.server_close()

    def id_token(self) -> str:
        state = self.read_state()
        if state is not None and not state.account_email:
            raise HusshIdentityError(
                "Reconnect Hussh One to verify this account before vault access."
            )
        if self._id_token and time.time() < self._id_token_expires_at:
            return self._id_token
        refresh_token = self.keychain.get(self._account("firebase-refresh-token"))
        if refresh_token is None:
            raise HusshIdentityError("Connect this Hermes profile to Hussh One first.")
        response = self.http.post(
            "https://securetoken.googleapis.com/v1/token",
            params={"key": UAT_FIREBASE_WEB_API_KEY},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.decode("utf-8"),
            },
        )
        if response.status_code == 400:
            self.lock_identity()
            raise HusshIdentityError(
                "The Hussh device session expired. Connect it again."
            )
        response.raise_for_status()
        payload = response.json()
        self._id_token = str(payload["id_token"])
        self._id_token_expires_at = (
            time.time() + int(payload.get("expires_in") or 3600) - 60
        )
        rotated = str(payload.get("refresh_token") or "")
        if rotated:
            self.keychain.set(
                self._account("firebase-refresh-token"), rotated.encode("utf-8")
            )
        if state is None:
            self.lock_identity()
            raise HusshIdentityError("The local Hussh identity state is unavailable.")
        devices = self.http.get(
            f"{state.api_base}/api/account/trusted-devices",
            headers={"Authorization": f"Bearer {self._id_token}"},
        )
        if devices.status_code != 200 or not any(
            item.get("device_id") == state.device_id and item.get("status") == "active"
            for item in (devices.json().get("devices") or [])
        ):
            self.disconnect(remove_device_key=True)
            raise HusshIdentityError("This Hussh trusted device was revoked.")
        return self._id_token

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.id_token()}"}

    def sign(self, payload: str) -> str:
        signature = self._device_private_key().sign(
            payload.encode("utf-8"), ec.ECDSA(hashes.SHA256())
        )
        return base64.b64encode(signature).decode("ascii")

    def lock_identity(self) -> None:
        self._id_token = None
        self._id_token_expires_at = 0.0

    def disconnect(self, *, remove_device_key: bool = False) -> None:
        self.cancel_pending_authorization()
        self.lock_identity()
        self.keychain.delete(self._account("firebase-refresh-token"))
        if remove_device_key:
            self.keychain.delete(self._account("device-signing-key"))
        if self.identity_path.exists():
            self.identity_path.unlink()


class _LoopbackHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        state = (query.get("state") or [""])[0]
        expected = str(getattr(self.server, "expected_state", ""))
        if parsed.path != "/callback" or not secrets.compare_digest(state, expected):
            self.server.result = {"error": "The authorization state did not match."}  # type: ignore[attr-defined]
            callback_event = getattr(self.server, "callback_event", None)
            if callback_event is not None:
                callback_event.set()
            self.send_response(400)
            body = b"Authorization failed. Return to Hermes."
        else:
            self.server.result = {  # type: ignore[attr-defined]
                "code": (query.get("code") or [""])[0],
                "error": (query.get("error") or [""])[0],
                "state": state,
            }
            callback_event = getattr(self.server, "callback_event", None)
            if callback_event is not None:
                callback_event.set()
            completion_event = getattr(self.server, "completion_event", None)
            completed = bool(completion_event and completion_event.wait(timeout=65))
            completion_status = str(
                getattr(self.server, "completion_status", "pending")
            )
            if completed and completion_status == "connected":
                self.send_response(200)
                body = b"Hussh One connected to Hermes. You can close this window."
            elif completed and completion_status == "error":
                self.send_response(502)
                body = (
                    b"Approval was received, but Hermes could not complete the "
                    b"connection. Return to Hermes, run /hussh-one status, and retry."
                )
            else:
                self.send_response(202)
                body = (
                    b"Approval was received and Hermes is still finalizing the "
                    b"connection. Return to Hermes and run /hussh-one status."
                )
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Callback URLs carry one-time authorization codes. Never log them.
        del format, args
        return


def _vault_handoff_aad(
    *,
    state: str,
    authorization_id: str,
    device_id: str,
    user_id: str,
    expires_at: int,
    vault_key_hash: str,
    wrapper_id: str,
    rp_id: str,
    environment: str,
    recipient_public_key: str,
) -> bytes:
    return "|".join(
        (
            VAULT_HANDOFF_VERSION,
            state,
            authorization_id,
            device_id,
            user_id,
            str(expires_at),
            vault_key_hash,
            wrapper_id,
            rp_id,
            environment,
            recipient_public_key,
        )
    ).encode("utf-8")


def _decrypt_vault_handoff(
    *,
    result: Any,
    recipient_private_key: x25519.X25519PrivateKey,
    state: str,
    device_id: str,
    user_id: str,
    authorization_id: str,
    expires_at: int,
    environment: str,
    recipient_public_key: str,
) -> bytes | None:
    if not isinstance(result, dict):
        return None
    algorithm = str(result.get("vault_handoff_alg") or "")
    if not algorithm:
        return None
    if algorithm != VAULT_HANDOFF_ALGORITHM:
        raise HusshIdentityError("The passkey vault handoff algorithm is unsupported.")
    sender_public_raw = base64.b64decode(
        str(result.get("vault_handoff_sender_public_key") or ""),
        validate=True,
    )
    if len(sender_public_raw) != 32:
        raise HusshIdentityError("The passkey vault handoff sender key is invalid.")
    shared_secret = recipient_private_key.exchange(
        x25519.X25519PublicKey.from_public_bytes(sender_public_raw)
    )
    wrapping_key = hashlib.sha256(shared_secret).digest()
    iv = base64.b64decode(str(result.get("vault_handoff_iv") or ""), validate=True)
    ciphertext = base64.b64decode(
        str(result.get("vault_handoff_wrapped_key") or ""),
        validate=True,
    )
    tag = base64.b64decode(str(result.get("vault_handoff_tag") or ""), validate=True)
    if len(iv) != 12 or len(tag) != 16 or len(ciphertext) != 32:
        raise HusshIdentityError("The passkey vault handoff payload is invalid.")
    vault_key = AESGCM(wrapping_key).decrypt(
        iv,
        ciphertext + tag,
        _vault_handoff_aad(
            state=state,
            authorization_id=authorization_id,
            device_id=device_id,
            user_id=user_id,
            expires_at=expires_at,
            vault_key_hash=str(result.get("vault_handoff_vault_key_hash") or ""),
            wrapper_id=str(result.get("vault_handoff_wrapper_id") or ""),
            rp_id=str(result.get("vault_handoff_rp_id") or ""),
            environment=environment,
            recipient_public_key=recipient_public_key,
        ),
    )
    if len(vault_key) != 32:
        raise HusshIdentityError("The passkey vault handoff key is invalid.")
    return vault_key
