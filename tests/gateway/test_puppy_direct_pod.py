# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
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
from hermes_cli.hussh_one_pkm.client import HusshIdentityError

from gateway.puppy_direct_pod import (
    DeviceEnvelope,
    DirectPodRefused,
    DirectPodUnavailable,
    PuppyDirectPodRelay,
    _admit,
    _pin_endpoint,
    _validate_binding,
    _verify_hub_signature,
    _json_response,
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


@pytest.mark.asyncio
async def test_remote_model_command_requires_exact_admitted_owner_and_pod(tmp_path: Path, monkeypatch):
    acknowledgements = []
    identity = SimpleNamespace(
        profile_home=tmp_path,
        read_state=lambda: SimpleNamespace(user_id="user-1", device_id="device-1"),
        post_puppy_model_selection_ack=lambda statement: acknowledgements.append(statement) or True,
    )
    direct = PuppyDirectPodRelay(identity)
    catalog_version = "a" * 64
    command = {
        "id": "command-1", "version": 1, "ownerId": "user-1",
        "deviceId": "device-1", "hushhId": "owner-1", "podKeyId": "pod-key-1",
        "model": "local-two", "catalogVersion": catalog_version,
        "expiresAt": int(time.time() * 1000) + 60_000,
    }

    class LocalRelay:
        model = "local-one"
        model_url = "http://127.0.0.1:1234/v1"

        async def model_catalog(self):
            return {
                "status": "available", "catalogVersion": catalog_version,
                "models": [{"id": "local-one"}, {"id": "local-two"}],
            }

        def _probe_mode(self):
            return "local-two-mode"

    relay = LocalRelay()
    saves = []
    monkeypatch.setattr(direct, "_save_local_default", lambda model, active: saves.append(model) or True)
    binding = _binding()
    assert await direct._process_model_command(
        {"status": "active", "puppyModelSelection": {**command, "podKeyId": "replaced"}},
        binding, relay, busy=False,
    ) is False
    assert saves == [] and acknowledgements == []
    assert await direct._process_model_command(
        {"status": "active", "puppyModelSelection": command}, binding, relay, busy=False,
    ) is True
    assert saves == ["local-two"]
    assert relay.model == "local-two"
    assert acknowledgements[0] == {
        "purpose": "puppy-model-selection-ack-v1",
        "id": "command-1", "version": 1, "ownerId": "user-1",
        "deviceId": "device-1", "hushhId": "owner-1", "podKeyId": "pod-key-1",
        "model": "local-two", "catalogVersion": catalog_version,
        "result": "applied", "reason": "",
    }
    restarted = PuppyDirectPodRelay(identity, model="local-two")
    monkeypatch.setattr(restarted, "_save_local_default", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate write")))
    assert await restarted._process_model_command(
        {"status": "active", "puppyModelSelection": command}, binding, relay, busy=False,
    ) is False
    assert acknowledgements[-1] == acknowledgements[0]
    next_command = {**command, "id": "command-2", "version": 2}
    assert await direct._process_model_command(
        {"status": "active", "puppyModelSelection": next_command},
        binding, relay, busy=True,
    ) is False
    assert saves == ["local-two"]
    assert acknowledgements[-1]["reason"] == "DEVICE_BUSY"


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
        {"deployment_target": "gcp"},
        {"deployment_target": None},
        {"deployment_target": "user_aws"},
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


@pytest.mark.parametrize("target", ["user_gcp", "user_azure"])
def test_binding_admits_an_agent_in_either_owner_cloud(target):
    """Founder's first Azure agent (2026-10-05): the hub signs ``user_azure``."""
    _validate_binding(
        {**_binding(), "deployment_target": target},
        _endpoint(),
        user_id="user-1",
        device_id="device-1",
        environment="dev",
    )


def test_binding_tolerates_clock_skew_without_accepting_future_grants():
    binding = {**_binding(), "issued_at_ms": int(time.time() * 1000) + 1000}
    _validate_binding(
        binding, _endpoint(), user_id="user-1", device_id="device-1", environment="dev"
    )
    binding["issued_at_ms"] += 60_000
    with pytest.raises(DirectPodRefused, match="not authorized"):
        _validate_binding(
            binding, _endpoint(), user_id="user-1", device_id="device-1", environment="dev"
        )


def test_remote_default_changes_only_the_local_profile_model(tmp_path: Path, monkeypatch):
    from gateway.puppy_inference_relay import PuppyInferenceRelay
    from hermes_cli import config as cfg

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = tmp_path / "config.yaml"
    source.write_text(
        "model:\n  provider: lmstudio\n  default: local-one\n"
        "  base_url: http://127.0.0.1:1234/v1\n"
        "agent:\n  max_turns: 12\n",
        encoding="utf-8",
    )
    relay = PuppyInferenceRelay(
        relay_url="wss://owner.example/relay", token="token", device_id="device-1",
        model="local-one", model_url="http://127.0.0.1:1234/v1",
    )
    assert PuppyDirectPodRelay._save_local_default("local-two", relay) is True
    stored = cfg.read_user_config_raw(source)
    assert stored["model"] == {
        "provider": "lmstudio", "default": "local-one",
        "base_url": "http://127.0.0.1:1234/v1",
    }
    assert stored["hussh_one"]["puppy"] == {
        "default_model": "local-two",
        "model_base_url": "http://127.0.0.1:1234/v1",
    }
    assert stored["agent"]["max_turns"] == 12
    other_endpoint = PuppyInferenceRelay(
        relay_url="wss://owner.example/relay", token="token", device_id="device-1",
        model="local-two", model_url="http://localhost:9999/v1",
    )
    assert PuppyDirectPodRelay._save_local_default("local-three", other_endpoint) is False
    assert cfg.read_user_config_raw(source)["model"]["default"] == "local-one"


def test_remote_model_ack_signs_exact_owner_device_and_pod_statement(tmp_path: Path, monkeypatch):
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

    calls = []
    http = SimpleNamespace(post=lambda url, **kwargs: calls.append((url, kwargs)) or SimpleNamespace(status_code=200))
    client = HusshIdentityClient(
        profile_home=tmp_path, http=http, keychain=SimpleNamespace(),
    )
    monkeypatch.setattr(
        client, "read_state",
        lambda: SimpleNamespace(api_base="https://hub.example", device_id="device-1"),
    )
    monkeypatch.setattr(client, "auth_headers", lambda: {"Authorization": "Bearer memory-only"})
    signed = []
    monkeypatch.setattr(client, "sign", lambda payload: signed.append(payload) or "device-proof")
    statement = {
        "purpose": "puppy-model-selection-ack-v1", "id": "command-1", "version": 1,
        "ownerId": "user-1", "deviceId": "device-1", "hushhId": "owner-1",
        "podKeyId": "pod-key-1", "model": "local-two",
        "catalogVersion": "a" * 64, "result": "applied", "reason": "",
    }
    assert client.post_puppy_model_selection_ack(statement) is True
    assert signed == [json.dumps(statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False)]
    assert calls[0][0] == "https://hub.example/api/account/trusted-devices/device-1/puppy-model-selection/ack"
    assert calls[0][1]["json"] == {
        "id": "command-1", "version": 1, "result": "applied",
        "reason": "", "proof": "device-proof",
    }


def test_endpoint_pin_refuses_same_version_repoint(tmp_path: Path):
    _pin_endpoint(tmp_path, _endpoint(), "dev")
    with pytest.raises(DirectPodRefused, match="changed"):
        _pin_endpoint(
            tmp_path, {**_endpoint(), "url": "https://attacker.example"}, "dev"
        )


@pytest.mark.parametrize("refusal", [None, "PUPPY_OWNER_APPROVAL_REQUIRED", "unknown", "hub_timeout", "pod_timeout"])
def test_admission_reuses_trusted_device_key_and_pod_challenge(tmp_path: Path, refusal, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="gateway.puppy_direct_pod")
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
        timeouts = request.extensions["timeout"]
        defaults = identity.http.timeout.as_dict()
        assert {k: v for k, v in timeouts.items() if k != "read"} == {k: v for k, v in defaults.items() if k != "read"}
        if request.url.path.endswith("/challenge"):
            assert defaults["read"] < timeouts["read"] < 120
        else:
            assert timeouts["read"] == defaults["read"]
        if (refusal == "hub_timeout" and request.url.path.endswith("/endpoint")
                or refusal == "pod_timeout" and request.url.path.endswith("/challenge")):
            raise httpx.ReadTimeout("Bearer sensitive-test-token https://private-pod.example", request=request)
        if request.url.path.endswith("/verification-keys"):
            return httpx.Response(200, json={"kind": "pod_verification_keys_v1", "keys": {"key": PUBLIC}})
        if request.url.path.endswith("/endpoint"):
            return httpx.Response(200, json=_endpoint())
        if request.url.path.endswith("/pod-binding"):
            if refusal in {"PUPPY_OWNER_APPROVAL_REQUIRED", "unknown"}:
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
    if refusal in {"hub_timeout", "pod_timeout"}:
        with pytest.raises(httpx.ReadTimeout):
            _admit(identity)
        stage = "endpoint" if refusal == "hub_timeout" else "challenge"
        assert f"admission_request stage={stage} outcome=timeout elapsed_ms=" in caplog.text
        assert "sensitive-test-token" not in caplog.text
        assert "private-pod.example" not in caplog.text
        assert not identity.signed
        assert not (tmp_path / "hussh-one/puppy-pod-pin.json").exists()
        assert not any(path.endswith("/admit") for path in paths)
        return
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
async def test_direct_relay_releases_idle_pod_but_preserves_active_inference():
    """A transport reconnect cannot keep the owner's pod running indefinitely."""
    import asyncio

    class Socket:
        def __init__(self):
            self.closed = []

        async def close(self, *, code, reason):
            self.closed.append((code, reason))

    relay = PuppyDirectPodRelay(SimpleNamespace())
    relay._idle_grace = 0.01
    relay._last_work = time.monotonic() - 1
    socket = Socket()
    busy = True
    task = asyncio.create_task(relay._close_when_idle(socket, lambda: busy))
    await asyncio.sleep(0.02)
    assert socket.closed == []
    busy = False
    await asyncio.wait_for(task, timeout=2)
    assert socket.closed == [(1000, "Puppy relay idle")]


@pytest.mark.asyncio
async def test_idle_device_refuses_revocation_without_contacting_pod():
    from gateway.puppy_direct_pod import PuppyDirectPodRelay
    identity = SimpleNamespace(read_state=lambda: SimpleNamespace(user_id="user-1", device_id="device-1"),
                               device_control_status=lambda: {"status": "revoked"})
    with pytest.raises(DirectPodRefused, match="no longer active"):
        await PuppyDirectPodRelay(identity)._wait_for_activation()


@pytest.mark.parametrize(("status", "code"), [(409, "POD_DIRECT_NOT_READY"), (403, "PUPPY_OWNER_APPROVAL_REQUIRED"), (401, "expired"), (403, "revoked"), (503, "unavailable")])
def test_temporary_pod_refusal_requires_fresh_activation_not_identity_reset(status, code):
    response = httpx.Response(status, json={"detail": {"code": code}})
    with pytest.raises(DirectPodUnavailable):
        _json_response(response)
    # A security failure is terminal; it must not masquerade as a retryable wake.
    with pytest.raises(DirectPodRefused) as refused:
        _json_response(httpx.Response(403, json={"detail": {"code": "bad_signature"}}))
    assert not isinstance(refused.value, DirectPodUnavailable)


@pytest.mark.asyncio
async def test_temporarily_unavailable_pod_returns_to_control_lane_without_reusing_session(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    relay = PuppyDirectPodRelay(SimpleNamespace(), wait_for_activation=True)
    binding = _binding()
    hint = {"id": "a" * 32, "podKeyId": binding["pod_key_id"], "hushhId": binding["hushh_id"]}
    relay._admitted = (binding, {"session": "previous"})
    relay._wait_for_activation = AsyncMock(side_effect=[hint, asyncio.CancelledError()])
    relay._session = AsyncMock(side_effect=DirectPodUnavailable("pod waking"))
    relay._connected = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await relay.serve()
    assert relay._admitted is None
    assert relay._waiting_activation is True
    assert relay._wait_for_activation.await_count == 2
    relay._session.assert_awaited_once()
    relay._connected.assert_not_awaited()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "relay"])
async def test_transient_reconnect_preserves_authority_and_redacts_transport_failure(stage, monkeypatch, caplog):
    import asyncio
    import logging
    from unittest.mock import AsyncMock

    relay = PuppyDirectPodRelay(SimpleNamespace(), wait_for_activation=True)
    binding = _binding()
    hint = {"id": "a" * 32, "podKeyId": binding["pod_key_id"], "hushhId": binding["hushh_id"]}
    private_message = "Authorization: Bearer sensitive-test-token https://private-pod.example"
    failure = httpx.ReadTimeout(private_message)
    relay._wait_for_activation = AsyncMock(side_effect=[hint, asyncio.CancelledError()])
    relay._session = AsyncMock(return_value=(binding, {"session": "admitted"}))
    relay._connected = AsyncMock(side_effect=asyncio.CancelledError())
    if stage == "admission":
        relay._session.side_effect = [failure, asyncio.CancelledError()]
    else:
        relay._session.side_effect = [(binding, {"session": "admitted"}), asyncio.CancelledError()]
        relay._connected.side_effect = failure
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    caplog.set_level(logging.INFO, logger="gateway.puppy_direct_pod")
    with pytest.raises(asyncio.CancelledError):
        await relay.serve()
    assert f"reconnecting stage={stage} reason=timeout elapsed_ms=" in caplog.text
    assert private_message not in caplog.text
    assert "sensitive-test-token" not in caplog.text
    assert "private-pod.example" not in caplog.text
    if stage == "admission":
        relay._session.assert_awaited_once()
        relay._connected.assert_not_awaited()
    else:
        assert relay._session.await_count == 2
        relay._connected.assert_awaited_once()


@pytest.mark.asyncio
async def test_machine_presence_retries_after_login_then_uses_keepalive(monkeypatch):
    import asyncio
    from gateway.puppy_direct_pod import PuppyDirectPodRelay

    class Presence:
        def __init__(self):
            self.events = []
            self.keepalives = 0

        def on_event(self, reason, *, force=False):
            self.events.append((reason, force))
            return len(self.events) >= 2

        def keepalive(self):
            self.keepalives += 1

    presence = Presence()
    relay = PuppyDirectPodRelay(SimpleNamespace(), presence=presence)
    sleeps = 0

    async def stop_after_three_ticks(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_three_ticks)
    with pytest.raises(asyncio.CancelledError):
        await relay._report_presence()

    assert presence.events == [
        ("relay_running", True),
        ("relay_running", True),
        ("relay_running", False),
    ]
    assert presence.keepalives == 1


@pytest.fixture
def relay_service_profile(tmp_path, monkeypatch):
    import yaml

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = {
        "model": {"default": "local/fixture", "base_url": "http://127.0.0.1:1234/v1"},
        "hussh_one": {"puppy": {"auto_start": True}},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    return home, config


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "cloud_model"])
async def test_profile_relay_does_not_open_identity_without_local_opt_in(
    relay_service_profile, monkeypatch, reason,
):
    import yaml
    from gateway.puppy_direct_service import run_profile_direct_relay
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

    home, config = relay_service_profile
    if reason == "disabled":
        config["hussh_one"]["puppy"]["auto_start"] = False
    else:
        config["model"]["base_url"] = "https://provider.example/v1"
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    def unexpected_identity(*args, **kwargs):
        raise AssertionError("No identity or network client should be opened")

    monkeypatch.setattr(HusshIdentityClient, "__init__", unexpected_identity)
    await run_profile_direct_relay()


@pytest.mark.asyncio
async def test_profile_relay_is_single_instance_and_releases_transport_and_lock_on_stop(
    relay_service_profile, monkeypatch,
):
    import asyncio
    from gateway.puppy_direct_service import run_profile_direct_relay
    from gateway.status import _get_scope_lock_path
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

    home, _ = relay_service_profile
    monkeypatch.setattr(HusshIdentityClient, "read_state", lambda self: SimpleNamespace(environment="dev"))
    entered = asyncio.Event()
    relays = []

    async def serve(relay):
        relays.append(relay)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(PuppyDirectPodRelay, "serve", serve)
    lock = _get_scope_lock_path("puppy-direct-relay", str(home.resolve()))
    for count in (1, 2):
        entered.clear()
        task = asyncio.create_task(run_profile_direct_relay())
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            await run_profile_direct_relay()  # No second relay for this profile.
            assert len(relays) == count
            assert relays[-1].identity.profile_home == home.resolve()
            assert relays[-1]._waiting_activation is True
            assert relays[-1].model_options["model"] == "local/fixture"
            assert lock.exists()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert relays[-1].identity.http.is_closed
        assert not lock.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [DirectPodRefused, HusshIdentityError])
async def test_profile_relay_self_disables_after_terminal_owner_refusal(
    relay_service_profile, monkeypatch, refusal,
):
    from gateway.puppy_direct_service import run_profile_direct_relay
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

    monkeypatch.setattr(HusshIdentityClient, "read_state", lambda self: SimpleNamespace(environment="dev"))

    async def serve(relay):
        raise refusal("fixture refusal")

    monkeypatch.setattr(PuppyDirectPodRelay, "serve", serve)
    # Clean return is the existing gateway supervisor's self-disable contract.
    await run_profile_direct_relay()
    with pytest.raises(refusal):
        await run_profile_direct_relay(supervised=False)


@pytest.mark.asyncio
async def test_profile_stop_retains_lock_until_blocked_admission_settles(
    relay_service_profile, monkeypatch,
):
    import asyncio
    import threading
    from gateway.puppy_direct_service import run_profile_direct_relay
    from gateway.status import _get_scope_lock_path
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

    home, _ = relay_service_profile
    entered, release = threading.Event(), threading.Event()
    requests, transports = [], []
    original_client = httpx.Client

    def answer(request):
        requests.append(request.url.path)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={})

    def client(**kwargs):
        transport = original_client(transport=httpx.MockTransport(answer), **kwargs)
        transports.append(transport)
        return transport

    async def wait_for_activation(self):
        return None

    async def report_presence(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(HusshIdentityClient, "read_state", lambda self: SimpleNamespace(
        environment="dev", api_base="https://hub.example", user_id="fixture-owner", device_id="fixture-device",
    ))
    monkeypatch.setattr(HusshIdentityClient, "auth_headers", lambda self: {})
    monkeypatch.setattr(PuppyDirectPodRelay, "_wait_for_activation", wait_for_activation)
    monkeypatch.setattr(PuppyDirectPodRelay, "_report_presence", report_presence)
    task = asyncio.create_task(run_profile_direct_relay())
    lock = _get_scope_lock_path("puppy-direct-relay", str(home.resolve()))
    try:
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert lock.exists()
            assert not transports[0].is_closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert requests == ["/api/one/personal-agent/endpoint"]
        assert not (home / "hussh-one" / "puppy-pod-pin.json").exists()
        assert transports[0].is_closed
        assert not lock.exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
