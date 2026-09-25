"""Owner-bound Puppy connection to a BYOC pod.

The hub issues a signed binding and publishes the endpoint. Inference frames
travel only on the device-to-pod socket and are sealed for that pod incarnation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from websockets.asyncio.client import connect

from gateway.puppy_inference_relay import PuppyInferenceRelay, _MAX_FRAME_BYTES
from hermes_cli.hussh_one_pkm.client import HusshIdentityClient, HusshIdentityError

logger = logging.getLogger(__name__)
_KEY_INFO = b"hussh/puppy-envelope/aes256gcm/v1"
_DEVICE_TO_POD = "d2p"
_POD_TO_DEVICE = "p2d"
_AAD_FIELDS = (
    "v",
    "hushhId",
    "deviceId",
    "sessionId",
    "epoch",
    "seq",
    "dir",
    "innerType",
)


class DirectPodRefused(RuntimeError):
    """The signed owner, deployment, or device boundary did not match."""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DirectPodRefused("Pod response was not an object")
    return value


def _nonce(direction: str, seq: int) -> bytes:
    if direction not in {_DEVICE_TO_POD, _POD_TO_DEVICE} or not 1 <= seq < 2**63:
        raise DirectPodRefused("Invalid Puppy frame position")
    return bytes([1 if direction == _DEVICE_TO_POD else 0, 0, 0, 0]) + seq.to_bytes(
        8, "big"
    )


class DeviceEnvelope:
    """The pod's PuppyEnvelope wire bytes, with independent device counters."""

    def __init__(
        self,
        private_key: X25519PrivateKey,
        pod_public_b64: str,
        *,
        hushh_id: str,
        device_id: str,
        session_id: str,
        epoch: int,
    ) -> None:
        try:
            public = base64.b64decode(pod_public_b64, validate=True)
            if len(public) != 32:
                raise ValueError("wrong key length")
            shared = private_key.exchange(X25519PublicKey.from_public_bytes(public))
        except (TypeError, ValueError) as exc:
            raise DirectPodRefused("Invalid pod identity key") from exc
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO
        ).derive(shared)
        self._aead = AESGCM(key)
        self._binding = (hushh_id, device_id, session_id, epoch)
        self.outbound_seq = 0
        self.inbound_seq = 0

    def _aad(self, direction: str, seq: int, inner_type: str) -> bytes:
        hushh_id, device_id, session_id, epoch = self._binding
        values = dict(
            zip(
                _AAD_FIELDS,
                (1, hushh_id, device_id, session_id, epoch, seq, direction, inner_type),
            )
        )
        return json.dumps(
            values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

    def seal(self, frame: dict[str, Any]) -> dict[str, Any]:
        kind = str(frame.get("type") or "")
        if not kind:
            raise DirectPodRefused("Puppy frame has no type")
        plaintext = json.dumps(
            frame, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if len(plaintext) > _MAX_FRAME_BYTES:
            raise DirectPodRefused("Puppy frame is too large")
        self.outbound_seq += 1
        seq = self.outbound_seq
        ciphertext = self._aead.encrypt(
            _nonce(_DEVICE_TO_POD, seq), plaintext, self._aad(_DEVICE_TO_POD, seq, kind)
        )
        return {
            "type": "sealed",
            "v": 1,
            "dir": _DEVICE_TO_POD,
            "seq": seq,
            "innerType": kind,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def open(self, frame: dict[str, Any]) -> dict[str, Any]:
        seq = self.inbound_seq + 1
        if (
            frame.get("type") != "sealed"
            or frame.get("v") != 1
            or frame.get("dir") != _POD_TO_DEVICE
            or frame.get("seq") != seq
        ):
            raise DirectPodRefused("Puppy frame order or direction changed")
        kind = str(frame.get("innerType") or "")
        try:
            ciphertext = base64.b64decode(
                str(frame.get("ciphertext") or ""), validate=True
            )
            if len(ciphertext) > _MAX_FRAME_BYTES + 64:
                raise ValueError("frame too large")
            plaintext = self._aead.decrypt(
                _nonce(_POD_TO_DEVICE, seq),
                ciphertext,
                self._aad(_POD_TO_DEVICE, seq, kind),
            )
            inner = _object(json.loads(plaintext))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DirectPodRefused("Puppy frame did not authenticate") from exc
        except Exception as exc:
            raise DirectPodRefused("Puppy frame did not authenticate") from exc
        if inner.get("type") != kind:
            raise DirectPodRefused("Puppy frame type changed")
        self.inbound_seq = seq
        return inner


class _SealedSender:
    def __init__(self, socket: Any, envelope: DeviceEnvelope) -> None:
        self.socket = socket
        self.envelope = envelope
        self.lock = asyncio.Lock()

    async def send(self, raw: str) -> None:
        frame = _object(json.loads(raw))
        async with self.lock:
            wire = json.dumps(self.envelope.seal(frame), separators=(",", ":"))
            if len(wire.encode()) > _MAX_FRAME_BYTES:
                raise DirectPodRefused("Puppy sealed frame is too large")
            await self.socket.send(wire)


def _pin_endpoint(profile_home: Path, record: dict[str, Any], environment: str) -> None:
    url = str(record.get("url") or "").rstrip("/")
    if (
        urlsplit(url).scheme != "https"
        or not urlsplit(url).hostname
        or str(record.get("environment") or "") != environment
        or not str(record.get("signature") or "").startswith("ed25519.")
        or not str(record.get("podKeyId") or "")
        or not str(record.get("hushhId") or "")
    ):
        raise DirectPodRefused("Owner pod endpoint is unavailable")
    version = record.get("endpointVersion")
    if type(version) is not int or version < 1:
        raise DirectPodRefused("Owner pod endpoint version is invalid")
    path = profile_home / "hussh-one" / "puppy-pod-pin.json"
    if path.exists():
        previous = _object(json.loads(path.read_text()))
        prior_version = previous.get("endpointVersion")
        if (
            type(prior_version) is not int
            or version < prior_version
            or previous.get("hushhId") != record["hushhId"]
            or (
                version == prior_version
                and (
                    previous.get("url") != url
                    or previous.get("podKeyId") != record["podKeyId"]
                )
            )
        ):
            raise DirectPodRefused("Owner pod endpoint changed without a valid version")
    public = {
        "url": url,
        "hushhId": record["hushhId"],
        "podKeyId": record["podKeyId"],
        "endpointVersion": version,
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(public, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _validate_binding(
    binding: dict[str, Any],
    endpoint: dict[str, Any],
    *,
    user_id: str,
    device_id: str,
    environment: str,
) -> None:
    expected = {
        "kind": "pod_binding_v1",
        "user_id": user_id,
        "subject_id": device_id,
        "hushh_id": endpoint["hushhId"],
        "pod_key_id": endpoint["podKeyId"],
        "url": str(endpoint["url"]).rstrip("/"),
        "environment": environment,
        "deployment_target": "user_gcp",
        "role": "device",
        "subject_kind": "device",
    }
    if any(binding.get(key) != value for key, value in expected.items()):
        raise DirectPodRefused("Puppy binding names another owner or pod")
    if (
        "puppy.inference" not in (binding.get("scopes") or [])
        or type(binding.get("expires_at_ms")) is not int
        or binding["expires_at_ms"] <= time.time() * 1000
    ):
        raise DirectPodRefused("Puppy inference is not authorized")
    try:
        if (
            len(
                base64.b64decode(
                    str(binding.get("pod_public_key") or ""), validate=True
                )
            )
            != 32
        ):
            raise ValueError("wrong key length")
    except (TypeError, ValueError) as exc:
        raise DirectPodRefused("Puppy binding has no pod identity key") from exc


def _json_response(response: Any) -> dict[str, Any]:
    if not response.is_success:
        raise DirectPodRefused("Puppy binding or pod session was refused")
    return _object(response.json())


def _admit(identity: HusshIdentityClient) -> tuple[dict[str, Any], dict[str, Any]]:
    state = identity.read_state()
    if state is None:
        raise DirectPodRefused("Connect this trusted device first")
    headers = identity.auth_headers()
    endpoint = _json_response(
        identity.http.get(
            f"{state.api_base}/api/one/personal-agent/endpoint", headers=headers
        )
    )
    issued = _json_response(
        identity.http.post(
            f"{state.api_base}/api/account/trusted-devices/{state.device_id}/pod-binding",
            headers=headers,
            json={"puppyInference": True},
        )
    )
    binding = _object(issued.get("binding"))
    _validate_binding(
        binding,
        endpoint,
        user_id=state.user_id,
        device_id=state.device_id,
        environment=state.environment,
    )
    if binding.get("subject_public_key") != identity.public_key_b64():
        raise DirectPodRefused("Puppy binding names another device key")
    signature = str(issued.get("signature") or "")
    if not signature.startswith("ed25519."):
        raise DirectPodRefused("Puppy binding signature is missing")
    pod = str(binding["url"]).rstrip("/")
    challenge = _json_response(
        identity.http.post(
            f"{pod}/api/one/pod/session/challenge", json={"subjectId": state.device_id}
        )
    )
    expected_payload = json.dumps(
        {
            "challenge_id": challenge.get("challengeId"),
            "epoch": challenge.get("epoch"),
            "hushh_id": binding["hushh_id"],
            "nonce": challenge.get("nonce"),
            "pod_key_id": binding["pod_key_id"],
            "purpose": "pod-session-admission",
            "subject_id": state.device_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if (
        challenge.get("signingPayload") != expected_payload
        or challenge.get("podKeyId") != binding["pod_key_id"]
    ):
        raise DirectPodRefused("Puppy pod challenge changed owner or deployment")
    session = _json_response(
        identity.http.post(
            f"{pod}/api/one/pod/session/admit",
            json={
                "binding": binding,
                "signature": signature,
                "challengeId": challenge["challengeId"],
                "nonce": challenge["nonce"],
                "proof": identity.sign(expected_payload),
                "epoch": challenge["epoch"],
            },
        )
    )
    if (
        session.get("role") != "device"
        or "puppy.inference" not in (session.get("scopes") or [])
        or session.get("epoch") != challenge["epoch"]
        or not session.get("sid")
        or not session.get("session")
    ):
        raise DirectPodRefused("Puppy pod session has the wrong scope")
    _pin_endpoint(identity.profile_home, endpoint, state.environment)
    return binding, session


class PuppyDirectPodRelay:
    def __init__(
        self,
        identity: HusshIdentityClient,
        *,
        model_url: str | None = None,
        model: str | None = None,
        model_api_key: str | None = None,
    ) -> None:
        self.identity = identity
        self.model_options = {
            "model_url": model_url,
            "model": model,
            "model_api_key": model_api_key,
        }

    async def _connected(
        self, binding: dict[str, Any], session: dict[str, Any]
    ) -> None:
        token = str(session["session"])
        device_id = str(binding["subject_id"])
        url = (
            str(binding["url"]).replace("https://", "wss://", 1)
            + "/api/one/puppy/relay"
        )
        model = PuppyInferenceRelay(
            relay_url=url, token=token, device_id=device_id, **self.model_options
        )
        ephemeral = X25519PrivateKey.generate()
        public = ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=_MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            hello = {
                **model.hello(),
                "capabilities": [
                    name for name, supported in model.capabilities.items() if supported
                ],
                "deviceEphemeralPublicKey": base64.b64encode(public).decode("ascii"),
            }
            await socket.send(json.dumps(hello, separators=(",", ":")))
            ready = _object(json.loads(await socket.recv()))
            if (
                ready.get("type") != "relay.ready"
                or ready.get("role") != "device"
                or ready.get("sealed") is not True
                or ready.get("podKeyId") != binding["pod_key_id"]
                or ready.get("epoch") != session["epoch"]
            ):
                raise DirectPodRefused("Puppy pod relay refused the bound session")
            envelope = DeviceEnvelope(
                ephemeral,
                str(binding["pod_public_key"]),
                hushh_id=str(binding["hushh_id"]),
                device_id=device_id,
                session_id=str(session["sid"]),
                epoch=int(session["epoch"]),
            )
            sender = _SealedSender(socket, envelope)
            heartbeat = asyncio.create_task(model._heartbeat(sender))
            inference: asyncio.Task[None] | None = None
            request_id = ""
            try:
                async for raw in socket:
                    if not isinstance(raw, str) or len(raw.encode()) > _MAX_FRAME_BYTES:
                        raise DirectPodRefused("Puppy pod frame is too large")
                    frame = envelope.open(_object(json.loads(raw)))
                    kind = frame.get("type")
                    if (
                        kind == "inference.cancel"
                        and frame.get("requestId") == request_id
                    ):
                        if inference is not None:
                            inference.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await inference
                        inference = None
                        request_id = ""
                    elif kind == "inference.request":
                        if inference is not None and not inference.done():
                            await sender.send(
                                json.dumps({
                                    "type": "inference.error",
                                    "requestId": frame.get("requestId"),
                                    "code": "LOCAL_MODEL_OVERLOADED",
                                })
                            )
                            continue
                        request_id = str(frame.get("requestId") or "")
                        inference = asyncio.create_task(model._infer(frame, sender))
            finally:
                heartbeat.cancel()
                if inference is not None:
                    inference.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                if inference is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await inference

    async def serve(self) -> None:
        delay = 2.0
        while True:
            try:
                binding, session = await asyncio.to_thread(_admit, self.identity)
                await self._connected(binding, session)
                delay = 2.0
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except (DirectPodRefused, HusshIdentityError):
                logger.warning("puppy_direct.refused")
                raise
            except Exception:
                logger.info("puppy_direct.reconnecting")
                await asyncio.sleep(delay + random.uniform(0.0, min(1.0, delay / 4)))
                delay = min(delay * 2.0, 30.0)
