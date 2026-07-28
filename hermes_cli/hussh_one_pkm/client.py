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
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .keychain import MacOSKeychain

UAT_API_BASE = "https://api.uat.hushh.ai"
UAT_WEB_BASE = "https://uat.one.hushh.ai"
# Firebase web API keys are public project identifiers, not credentials. The
# refresh credential created with this key remains Keychain-only.
UAT_FIREBASE_WEB_API_KEY = "AIzaSyAJ0RDWrBYF6yIDvwdyoAVO4K5QkL218Yc"


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
    api_base: str = UAT_API_BASE
    web_base: str = UAT_WEB_BASE
    environment: str = "uat"

    def to_json(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "profile_id": self.profile_id,
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

    def start_authorization(self, *, device_name: str) -> dict[str, Any]:
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
        server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        server.expected_state = state  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]

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
            }

        params = urllib.parse.urlencode({
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "device_public_key": device_public_key,
            "device_name": device_name.strip() or "Hermes on Mac",
            "platform": "macos",
            "state": state,
        })
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

    def open_authorization(self, *, device_name: str) -> dict[str, Any]:
        result = self.start_authorization(device_name=device_name)
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

    def _serve_authorization(self, server: ThreadingHTTPServer, verifier: str) -> None:
        try:
            server.timeout = 300
            server.handle_request()
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
            refresh_token = str(session["refreshToken"]).encode("utf-8")
            self.keychain.set(self._account("firebase-refresh-token"), refresh_token)
            self._id_token = str(session["idToken"])
            self._id_token_expires_at = (
                time.time() + int(session.get("expiresIn") or 3600) - 60
            )
            state = IdentityState(
                user_id=str(payload["user_id"]),
                device_id=str(payload["device_id"]),
                profile_id=self.profile_id,
            )
            _atomic_json(self.identity_path, state.to_json())
            with self._pending_lock:
                assert self._pending is not None
                self._pending["status"] = "connected"
        except Exception as exc:
            with self._pending_lock:
                if self._pending is not None:
                    self._pending["status"] = "error"
                    self._pending["error"] = str(exc)
        finally:
            server.server_close()

    def id_token(self) -> str:
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
        state = self.read_state()
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
            self.send_response(400)
            body = b"Authorization failed. Return to Hermes."
        else:
            self.server.result = {  # type: ignore[attr-defined]
                "code": (query.get("code") or [""])[0],
                "error": (query.get("error") or [""])[0],
            }
            self.send_response(200)
            body = b"Device connected. You can return to Hermes."
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Callback URLs carry one-time authorization codes. Never log them.
        return
