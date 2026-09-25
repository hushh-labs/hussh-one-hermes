"""The device's direct BYOC boundary and the pod's sealed-frame wire vector."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from gateway.puppy_direct_pod import (
    DeviceEnvelope,
    DirectPodRefused,
    _admit,
    _pin_endpoint,
    _validate_binding,
)


POD_PUBLIC = "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9/AsrhtHHw="
POD_FRAME = (
    "fNMHTzy4zmCzTvm1HVcUqwv6sUTl9gjjWPTWxNE8k7+puDX/lGk+0MezhtAeL1v/"
    "NM0c0GgbyDxPmjkxDu716pshGnqdlHbooW4="
)


def _endpoint():
    return {
        "url": "https://owner-pod.example",
        "hushhId": "owner-1",
        "podKeyId": "pod-key-1",
        "environment": "dev",
        "endpointVersion": 2,
        "signature": "ed25519.key.signature",
    }


def _binding():
    return {
        "kind": "pod_binding_v1",
        "user_id": "user-1",
        "subject_id": "device-1",
        "subject_public_key": "device-public-key",
        "hushh_id": "owner-1",
        "pod_key_id": "pod-key-1",
        "pod_public_key": POD_PUBLIC,
        "url": "https://owner-pod.example",
        "environment": "dev",
        "deployment_target": "user_gcp",
        "role": "device",
        "subject_kind": "device",
        "scopes": ["puppy.inference"],
        "expires_at_ms": int(time.time() * 1000) + 60_000,
    }


def test_pod_to_device_vector_and_replay_refusal():
    device = X25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    envelope = DeviceEnvelope(
        device,
        POD_PUBLIC,
        hushh_id="owner-1",
        device_id="device-1",
        session_id="session-1",
        epoch=2,
    )
    sealed = {
        "type": "sealed",
        "v": 1,
        "dir": "p2d",
        "seq": 1,
        "innerType": "inference.delta",
        "ciphertext": POD_FRAME,
    }
    assert envelope.open(sealed) == {
        "type": "inference.delta",
        "requestId": "r1",
        "text": "hello",
    }
    with pytest.raises(DirectPodRefused, match="order"):
        envelope.open(sealed)
    assert envelope.seal({"type": "relay.heartbeat", "status": "ready"}) == {
        "type": "sealed",
        "v": 1,
        "dir": "d2p",
        "seq": 1,
        "innerType": "relay.heartbeat",
        "ciphertext": "CwBQkl6OHrIP+utbBAzITyRyCso3W+FrTxzCvymUNLtMwqcZBNuREBUWOjx0k+PAamvqZuIV8uxYqmA=",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"deployment_target": "gcp"},
        {"user_id": "someone-else"},
        {"subject_id": "other-device"},
        {"pod_key_id": "replaced-pod"},
        {"scopes": []},
    ],
)
def test_binding_refuses_wrong_owner_pod_or_scope(change):
    binding = {**_binding(), **change}
    with pytest.raises(DirectPodRefused):
        _validate_binding(
            binding,
            _endpoint(),
            user_id="user-1",
            device_id="device-1",
            environment="dev",
        )


def test_endpoint_pin_refuses_same_version_repoint(tmp_path: Path):
    _pin_endpoint(tmp_path, _endpoint(), "dev")
    with pytest.raises(DirectPodRefused, match="changed"):
        _pin_endpoint(
            tmp_path, {**_endpoint(), "url": "https://attacker.example"}, "dev"
        )


def test_admission_reuses_trusted_device_key_and_pod_challenge(tmp_path: Path):
    binding = _binding()
    payload = json.dumps(
        {
            "challenge_id": "challenge-1",
            "epoch": 2,
            "hushh_id": "owner-1",
            "nonce": "nonce-1",
            "pod_key_id": "pod-key-1",
            "purpose": "pod-session-admission",
            "subject_id": "device-1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    paths = []

    def answer(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/endpoint"):
            return httpx.Response(200, json=_endpoint())
        if request.url.path.endswith("/pod-binding"):
            return httpx.Response(
                200, json={"binding": binding, "signature": "ed25519.key.signature"}
            )
        if request.url.path.endswith("/challenge"):
            return httpx.Response(
                200,
                json={
                    "challengeId": "challenge-1",
                    "nonce": "nonce-1",
                    "epoch": 2,
                    "podKeyId": "pod-key-1",
                    "signingPayload": payload,
                },
            )
        if request.url.path.endswith("/admit"):
            sent = json.loads(request.content)
            assert sent["proof"] == "device-signature"
            assert sent["binding"] == binding
            return httpx.Response(
                200,
                json={
                    "session": "opaque-session",
                    "sid": "sid-1",
                    "role": "device",
                    "scopes": ["puppy.inference"],
                    "epoch": 2,
                },
            )
        raise AssertionError(request.url.path)

    class Identity:
        profile_home = tmp_path
        http = httpx.Client(transport=httpx.MockTransport(answer))
        signed = ""

        def read_state(self):
            return SimpleNamespace(
                api_base="https://hub.example",
                user_id="user-1",
                device_id="device-1",
                environment="dev",
            )

        def auth_headers(self):
            return {"Authorization": "Bearer test-owner-token"}

        def public_key_b64(self):
            return "device-public-key"

        def sign(self, message):
            self.signed = message
            return "device-signature"

    identity = Identity()
    admitted_binding, session = _admit(identity)
    assert admitted_binding == binding and session["sid"] == "sid-1"
    assert identity.signed == payload
    assert paths == [
        "/api/one/personal-agent/endpoint",
        "/api/account/trusted-devices/device-1/pod-binding",
        "/api/one/pod/session/challenge",
        "/api/one/pod/session/admit",
    ]
    pin = json.loads((tmp_path / "hussh-one/puppy-pod-pin.json").read_text())
    assert pin["podKeyId"] == "pod-key-1"
    pin_path = tmp_path / "hussh-one/puppy-pod-pin.json"
    pin_path.write_text(json.dumps({**pin, "url": "https://previous-pod.example"}))
    paths.clear()
    with pytest.raises(DirectPodRefused, match="endpoint changed"):
        _admit(identity)
    assert paths == [
        "/api/one/personal-agent/endpoint",
        "/api/account/trusted-devices/device-1/pod-binding",
    ]
