# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request

from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge
from hermes_cli.hussh_one_pkm.crypto import (
    VaultCryptoError,
    read_envelope,
    unwrap_local_vault_key,
    unwrap_passphrase_vault_key,
    vault_key_hash,
    wrap_local_vault_key,
    write_envelope,
)
from hermes_cli.hussh_one_pkm.mcp_server import ProposalStore
from hermes_cli.hussh_one_pkm import mcp_server
from hermes_cli.hussh_one_pkm.pkm import (
    PkmClient,
    PkmProposal,
    _decrypt_domain,
    _patch_leaf_paths,
    _path_exists,
)
from hermes_cli.web_server import _require_local_hussh_one_request


def _vector() -> dict:
    path = (
        Path(__file__).parents[2]
        / "hermes_cli"
        / "hussh_one_pkm"
        / "golden_vectors"
        / "vault_passphrase_v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _mutation_vector() -> dict:
    path = (
        Path(__file__).parents[2]
        / "hermes_cli"
        / "hussh_one_pkm"
        / "golden_vectors"
        / "mutation_plan_v2.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_web_vault_passphrase_vector_parity() -> None:
    vector = _vector()
    vault_key = unwrap_passphrase_vault_key(
        passphrase=vector["passphrase"],
        encrypted_vault_key=vector["encrypted_vault_key_b64"],
        salt=vector["salt_b64"],
        iv=vector["iv_b64"],
    )
    assert vault_key.hex() == vector["vault_key_hex"]
    assert vault_key_hash(vault_key) == vector["vault_key_hash"]

    with pytest.raises(VaultCryptoError, match="incorrect"):
        unwrap_passphrase_vault_key(
            passphrase="wrong",
            encrypted_vault_key=vector["encrypted_vault_key_b64"],
            salt=vector["salt_b64"],
            iv=vector["iv_b64"],
        )


def test_local_envelope_is_profile_and_device_bound(tmp_path: Path) -> None:
    vault_key = bytes(range(32))
    wrapping_key = bytes(reversed(range(32)))
    envelope = wrap_local_vault_key(
        vault_key=vault_key,
        device_wrapping_key=wrapping_key,
        profile_id="profile-a",
        user_id="user-a",
        device_id="tdv_device-a",
    )
    path = tmp_path / "profile" / "hussh-one" / "vault-envelope.json"
    write_envelope(path, envelope)

    stored = read_envelope(path)
    assert (
        unwrap_local_vault_key(envelope=stored, device_wrapping_key=wrapping_key)
        == vault_key
    )
    assert path.stat().st_mode & 0o777 == 0o600

    tampered = stored.__class__(**{**stored.to_json(), "profile_id": "profile-b"})
    with pytest.raises(VaultCryptoError, match="could not be opened"):
        unwrap_local_vault_key(envelope=tampered, device_wrapping_key=wrapping_key)

    unsupported = stored.__class__(**{**stored.to_json(), "schema_version": 2})
    with pytest.raises(VaultCryptoError, match="version is unsupported"):
        unwrap_local_vault_key(envelope=unsupported, device_wrapping_key=wrapping_key)


def test_profile_lock_state_coordinates_dashboard_and_mcp_processes(
    tmp_path: Path,
) -> None:
    class FakeKeychain:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def get(self, account: str) -> bytes | None:
            return self.values.get(account)

        def set(self, account: str, secret: bytes) -> None:
            self.values[account] = secret

        def delete(self, account: str) -> None:
            self.values.pop(account, None)

    profile = tmp_path / "profile"
    keychain = FakeKeychain()
    dashboard_bridge = HusshVaultBridge(
        profile_home=profile,
        keychain=keychain,  # type: ignore[arg-type]
    )
    identity = {
        "user_id": "user-a",
        "device_id": "tdv_device-a",
        "profile_id": dashboard_bridge.identity.profile_id,
        "environment": "uat",
    }
    dashboard_bridge.identity.identity_path.parent.mkdir(parents=True)
    dashboard_bridge.identity.identity_path.write_text(
        json.dumps(identity), encoding="utf-8"
    )

    vault_key = bytes(range(32))
    wrapping_key = bytes(reversed(range(32)))
    keychain.set(dashboard_bridge._account("device-wrapping-key"), wrapping_key)
    write_envelope(
        dashboard_bridge.envelope_path,
        wrap_local_vault_key(
            vault_key=vault_key,
            device_wrapping_key=wrapping_key,
            profile_id=dashboard_bridge.identity.profile_id,
            user_id="user-a",
            device_id="tdv_device-a",
        ),
    )
    dashboard_bridge._write_lock_state(locked=False, reason="test_unlock")

    mcp_bridge = HusshVaultBridge(
        profile_home=profile,
        keychain=keychain,  # type: ignore[arg-type]
    )
    assert mcp_bridge.require_vault_key() == vault_key

    dashboard_bridge.lock(reason="workstation_lock")
    with pytest.raises(VaultCryptoError, match="Unlock"):
        mcp_bridge.require_vault_key()


def test_local_enrollment_validate_write_and_readback_smoke(tmp_path: Path) -> None:
    class FakeKeychain:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def get(self, account: str) -> bytes | None:
            return self.values.get(account)

        def set(self, account: str, secret: bytes) -> None:
            self.values[account] = secret

        def delete(self, account: str) -> None:
            self.values.pop(account, None)

    class Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class FakeHttp:
        def __init__(self, vector: dict) -> None:
            self.vector = vector
            self.store_payload: dict | None = None

        def get(self, url: str, **_kwargs) -> Response:
            if "/domain-snapshot/" in url:
                return Response(404, {})
            if "/mutation-impact/" in url:
                return Response(
                    200,
                    {
                        "active_recipient_count": 0,
                        "recipient_labels": [],
                        "enters_next_export_revision": False,
                        "summary": "No active recipients are affected.",
                        "affected_grant_ids": [],
                        "affected_export_ids": [],
                    },
                )
            raise AssertionError(f"Unexpected GET {url}")

        def post(self, url: str, **kwargs) -> Response:
            if url.endswith("/db/vault/get"):
                return Response(
                    200,
                    {
                        "vaultKeyHash": self.vector["vault_key_hash"],
                        "wrappers": [
                            {
                                "method": "passphrase",
                                "encryptedVaultKey": self.vector[
                                    "encrypted_vault_key_b64"
                                ],
                                "salt": self.vector["salt_b64"],
                                "iv": self.vector["iv_b64"],
                            }
                        ],
                    },
                )
            if url.endswith("/api/pkm/store-domain/validate"):
                return Response(200, {"success": True})
            if url.endswith("/api/pkm/store-domain"):
                self.store_payload = kwargs["json"]
                return Response(200, {"success": True, "data_version": 1})
            raise AssertionError(f"Unexpected POST {url}")

    vector = _vector()
    profile = tmp_path / "profile"
    fake_http = FakeHttp(vector)
    bridge = HusshVaultBridge(
        profile_home=profile,
        keychain=FakeKeychain(),  # type: ignore[arg-type]
        http=fake_http,  # type: ignore[arg-type]
    )
    state = {
        "user_id": "user-a",
        "device_id": "tdv_device-a",
        "profile_id": bridge.identity.profile_id,
        "api_base": "https://api.uat.hushh.ai",
        "web_base": "https://uat.one.hushh.ai",
        "environment": "uat",
    }
    bridge.identity.identity_path.parent.mkdir(parents=True)
    bridge.identity.identity_path.write_text(json.dumps(state), encoding="utf-8")
    bridge.identity.auth_headers = lambda: {"Authorization": "Bearer identity"}  # type: ignore[method-assign]
    bridge.acquire_vault_owner_token = lambda: "owner-token"  # type: ignore[method-assign]

    readiness = bridge.enroll_vault(vector["passphrase"])
    assert readiness == {
        "enrolled": True,
        "unlocked": True,
        "contract_compatible": True,
    }

    client = PkmClient(bridge)
    proposal = client.propose(
        domain="hermes_uat_smoke",
        scope_path="readiness",
        merge_patch={"readiness": {"verified": True}},
        summary="Create a synthetic readiness marker.",
    )
    assert client.commit(proposal) == {
        "success": True,
        "domain": "hermes_uat_smoke",
        "data_version": 1,
        "exports_marked_for_refresh": False,
    }
    assert fake_http.store_payload is not None
    decrypted = _decrypt_domain(
        fake_http.store_payload["encrypted_blob"],
        bytes.fromhex(vector["vault_key_hex"]),
    )
    assert decrypted == {"readiness": {"verified": True}}


def test_vault_setup_control_plane_rejects_remote_dashboard() -> None:
    app = FastAPI()
    app.state.auth_required = True
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/hussh-one/vault/enroll",
            "query_string": b"",
            "headers": [(b"host", b"remote.example")],
            "server": ("remote.example", 443),
            "client": ("203.0.113.10", 43119),
            "app": app,
        }
    )

    with pytest.raises(HTTPException) as raised:
        _require_local_hussh_one_request(request)
    assert raised.value.status_code == 403


def test_proposal_store_returns_only_safe_metadata_and_is_single_use() -> None:
    proposal = PkmProposal(
        proposal_id="pkm_proposal_fixture",
        domain="hermes_uat_smoke",
        scope_path="readiness",
        operation="create",
        summary="Add a synthetic readiness marker.",
        merge_patch={"readiness": {"secret": "never echo"}},
        sharing_impact={
            "active_recipient_count": 0,
            "recipient_labels": [],
            "enters_next_export_revision": False,
            "summary": "No active recipients are affected.",
        },
        source_revision=0,
    )
    assert "merge_patch" not in proposal.safe_view()
    store = ProposalStore()
    store.put(proposal)
    assert store.take(proposal.proposal_id) == proposal
    with pytest.raises(ValueError, match="missing or expired"):
        store.take(proposal.proposal_id)


def test_nested_write_scope_helpers_are_exact() -> None:
    patch = {"profile": {"preferences": {"theme": "dark"}}}
    assert _patch_leaf_paths(patch) == ["profile.preferences.theme"]
    assert _path_exists(patch, "profile.preferences")
    assert not _path_exists(patch, "profile.identity")


def test_typescript_mutation_plan_vector_parity() -> None:
    vector = _mutation_vector()
    proposal = PkmProposal(
        proposal_id="pkm_proposal_fixture",
        domain=vector["domain"],
        scope_path=vector["scope_path"],
        operation="update",
        summary=vector["summary"],
        merge_patch={"profile": {"marker": True}},
        sharing_impact=vector["sharing_impact"],
        source_revision=vector["source_revision"],
    )
    client = object.__new__(PkmClient)
    plan = client._mutation_plan(
        proposal=proposal,
        state=SimpleNamespace(user_id=vector["user_id"]),
        current={
            "content_revision": vector["source_revision"],
            "scopes": [
                {
                    "scope_handle": vector["scope_handle"],
                    "summary_projection": {
                        "top_level_scope_path": vector["scope_path"],
                    },
                }
            ],
        },
        impact=vector["sharing_impact"],
    )

    for key, expected in vector["expected"].items():
        assert plan[key] == expected
    assert plan["confirmation_receipt"]["plan_id"] == plan["plan_id"]
    assert plan["confirmation_receipt"]["confirmed_by_user_id"] == vector["user_id"]
    assert plan["confirmation_receipt"]["displayed_scope"] == vector["scope_path"]


def test_additive_mcp_surface_keeps_the_six_tool_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mcp")

    class FakeBridge:
        pass

    monkeypatch.setattr(mcp_server, "HusshVaultBridge", FakeBridge)
    server = mcp_server._build_server()
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == {
        "hussh_identity_status",
        "hussh_vault_status",
        "hussh_pkm_request_scope",
        "hussh_pkm_propose_write",
        "hussh_pkm_commit_write",
        "hussh_vault_lock",
    }
