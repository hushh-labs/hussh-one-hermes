"""The device's direct BYOC boundary and the pod's sealed-frame wire vector."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from gateway.puppy_direct_pod import (
    DeviceEnvelope,
    DirectPodRefused,
    _admit,
    _pin_endpoint,
    _validate_binding,
    _verify_hub_signature,
)


POD_PUBLIC = "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9/AsrhtHHw="
POD_FRAME = (
    "fNMHTzy4zmCzTvm1HVcUqwv6sUTl9gjjWPTWxNE8k7+puDX/lGk+0MezhtAeL1v/"
    "NM0c0GgbyDxPmjkxDu716pshGnqdlHbooW4="
)


ISSUER = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(ISSUER.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def _signature(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "ed25519.key." + base64.urlsafe_b64encode(ISSUER.sign(raw)).decode().rstrip("=")


def _endpoint():
    body = {
        "kind": "pod_endpoint_v1",
        "url": "https://owner-pod.example",
        "hushhId": "owner-1",
        "podKeyId": "pod-key-1",
        "environment": "dev",
        "endpointVersion": 2,
    }
    return {**body, "signature": _signature(body)}


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
        "issued_at_ms": int(time.time() * 1000) - 1000,
        "version": 1,
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
        {"scopes": ["puppy.inference", "files.manage"]},
        {"scopes": "puppy.inference"},
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


@pytest.mark.parametrize("refusal", [None, "PUPPY_OWNER_APPROVAL_REQUIRED", "unknown"])
def test_admission_reuses_trusted_device_key_and_pod_challenge(tmp_path: Path, refusal):
    binding = _binding()
    payload = json.dumps(
        {
            "challenge_id": "psc_challenge1",
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
        if request.url.path.endswith("/verification-keys"):
            return httpx.Response(200, json={"kind": "pod_verification_keys_v1", "keys": {"key": PUBLIC}})
        if request.url.path.endswith("/endpoint"):
            return httpx.Response(200, json=_endpoint())
        if request.url.path.endswith("/pod-binding"):
            if refusal:
                return httpx.Response(403, json={"detail": {
                    "code": refusal, "message": "synthetic-private-response-do-not-display",
                }})
            return httpx.Response(
                200, json={"binding": binding, "signature": _signature(binding)}
            )
        if request.url.path.endswith("/challenge"):
            return httpx.Response(
                200,
                json={
                    "challengeId": "psc_challenge1",
                    "nonce": "nonce-1",
                    "epoch": 2,
                    "podKeyId": "pod-key-1",
                    "signingPayload": payload,
                    "expiresAt": int(time.time() * 1000) + 30_000,
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
                    "version": 1,
                    "expiresAt": int(time.time() * 1000) + 20_000,
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
    if refusal:
        with pytest.raises(DirectPodRefused) as caught:
            _admit(identity)
        assert "synthetic-private-response" not in str(caught.value)
        if refusal == "PUPPY_OWNER_APPROVAL_REQUIRED":
            assert "enable Puppy" in str(caught.value)
        assert not identity.signed
        assert not (tmp_path / "hussh-one/puppy-pod-pin.json").exists()
        assert not any(path.endswith(("/challenge", "/admit")) for path in paths)
        return
    admitted_binding, session = _admit(identity)
    assert admitted_binding == binding and session["sid"] == "sid-1"
    assert identity.signed == payload
    assert paths == [
        "/api/one/personal-agent/endpoint",
        "/api/one/personal-agent/verification-keys",
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
        "/api/one/personal-agent/verification-keys",
    ]


@pytest.mark.parametrize("change", [{"url": "https://attacker.example"}, {"environment": "prod"}])
def test_signature_refuses_altered_destination(change):
    endpoint = _endpoint()
    signature = endpoint.pop("signature")
    _verify_hub_signature(endpoint, signature, {"key": PUBLIC})
    with pytest.raises(DirectPodRefused, match="signature"):
        _verify_hub_signature({**endpoint, **change}, signature, {"key": PUBLIC})


def test_unknown_key_and_prefix_only_signature_are_refused():
    endpoint = _endpoint()
    signature = endpoint.pop("signature")
    with pytest.raises(DirectPodRefused):
        _verify_hub_signature(endpoint, signature, {})
    with pytest.raises(DirectPodRefused):
        _verify_hub_signature(endpoint, "ed25519.key.signature", {"key": PUBLIC})


@pytest.mark.asyncio
async def test_cached_reconnect_renews_only_at_bound_pod_during_hub_outage():
    from gateway.puppy_direct_pod import PuppyDirectPodRelay
    binding = _binding()
    previous = {"session": "admitted", "sid": "session-1", "role": "device",
                "scopes": ["puppy.inference"], "version": 1, "epoch": 1,
                "expiresAt": binding["expires_at_ms"] - 1000}
    renewed = {**previous, "session": "renewed", "sid": "session-2", "epoch": 2}
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        assert url == binding["url"] + "/api/one/pod/session/renew"
        return httpx.Response(200, json=renewed, request=httpx.Request("POST", url))
    identity = SimpleNamespace(
        read_state=lambda: SimpleNamespace(user_id="user-1", device_id="device-1", environment="dev"),
        http=SimpleNamespace(post=post),
    )
    relay = PuppyDirectPodRelay(identity)
    relay._admitted = (binding, previous)
    assert await relay._session() == (binding, renewed)
    assert len(calls) == 1
    assert calls[0][1]["headers"] == {"Authorization": "Bearer admitted"}
    renewed["scopes"] = ["puppy.inference", "pod.admin"]
    with pytest.raises(DirectPodRefused):
        await relay._session()


@pytest.mark.asyncio
async def test_idle_device_waits_for_active_owner_bound_hint(monkeypatch):
    from gateway.puppy_direct_pod import PuppyDirectPodRelay
    from unittest.mock import AsyncMock
    hint = {"id": "a" * 32, "ownerId": "user-1", "deviceId": "device-1",
            "podKeyId": "pod-key-1", "hushhId": "owner-1", "expiresAt": int(time.time() * 1000) + 60_000}
    states = iter([
        {"status": "indeterminate", "puppyActivation": hint},
        {"status": "active", "puppyActivation": {**hint, "ownerId": "other"}},
        {"status": "active", "puppyActivation": hint},
    ])
    identity = SimpleNamespace(read_state=lambda: SimpleNamespace(user_id="user-1", device_id="device-1"),
                               device_control_status=lambda: next(states))
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.puppy_direct_pod.asyncio.sleep", sleep)
    relay = PuppyDirectPodRelay(identity)
    assert await relay._wait_for_activation() == hint
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_idle_device_refuses_revocation_without_contacting_pod():
    from gateway.puppy_direct_pod import PuppyDirectPodRelay
    identity = SimpleNamespace(read_state=lambda: SimpleNamespace(user_id="user-1", device_id="device-1"),
                               device_control_status=lambda: {"status": "revoked"})
    with pytest.raises(DirectPodRefused, match="no longer active"):
        await PuppyDirectPodRelay(identity)._wait_for_activation()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_startup_wait_requires_activation_and_fresh_admission(approved):
    import asyncio
    from unittest.mock import AsyncMock
    from gateway.puppy_direct_pod import PuppyDirectPodRelay

    relay = PuppyDirectPodRelay(SimpleNamespace(), wait_for_activation=True)
    binding = _binding()
    hint = {"id": "a" * 32, "podKeyId": binding["pod_key_id"],
            "hushhId": binding["hushh_id"]}

    async def activate():
        relay._session.assert_not_awaited()
        relay._connected.assert_not_awaited()
        return hint

    relay._wait_for_activation = AsyncMock(side_effect=activate)
    relay._session = AsyncMock(return_value=(binding, {"session": "admitted"}))
    if not approved:
        relay._session.side_effect = DirectPodRefused("Owner approval is required")
    relay._connected = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError if approved else DirectPodRefused):
        await relay.serve()
    relay._wait_for_activation.assert_awaited_once()
    relay._session.assert_awaited_once()
    if approved:
        relay._connected.assert_awaited_once_with(binding, {"session": "admitted"})
    else:
        relay._connected.assert_not_awaited()
