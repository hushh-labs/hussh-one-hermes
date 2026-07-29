# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Client-side PKM read/validate/commit implementation for the vault bridge."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .bridge import HusshVaultBridge

_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,15}$")
_PKM_CONTRACT_VERSION = "6.0.0"
_READABLE_PROJECTION_VERSION = "6.0.0"
_MAX_NATIVE_READ_LEAVES = 500
_MAX_NATIVE_READ_JSON_BYTES = 65_536
_BLOCKED_EXTERNAL_PATH_PARTS = {
    "changes",
    "created_at",
    "debug",
    "debug_fields",
    "entity_id",
    "hash",
    "metadata",
    "parser_metadata",
    "provenance",
    "schema_version",
    "source_agent",
    "timestamps",
    "updated_at",
    "workflow",
    "workflow_id",
    "workflow_state",
}


class PkmBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PkmProposal:
    proposal_id: str
    domain: str
    scope_path: str
    operation: str
    summary: str
    merge_patch: dict[str, Any]
    sharing_impact: dict[str, Any]
    source_revision: int

    def safe_view(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "domain": self.domain,
            "scope_path": self.scope_path,
            "operation": self.operation,
            "summary": self.summary,
            "sharing_impact": {
                "active_recipient_count": int(
                    self.sharing_impact.get("active_recipient_count") or 0
                ),
                "recipient_labels": self.sharing_impact.get("recipient_labels") or [],
                "enters_next_export_revision": bool(
                    self.sharing_impact.get("enters_next_export_revision")
                ),
                "summary": str(self.sharing_impact.get("summary") or ""),
            },
        }


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decrypt_blob(blob: dict[str, Any], key: bytes) -> Any:
    combined = base64.b64decode(blob["ciphertext"]) + base64.b64decode(blob["tag"])
    plaintext = AESGCM(key).decrypt(base64.b64decode(blob["iv"]), combined, None)
    return json.loads(plaintext.decode("utf-8"))


def _encrypt_value(value: Any, key: bytes) -> dict[str, str]:
    iv = __import__("os").urandom(12)
    combined = AESGCM(key).encrypt(
        iv,
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        None,
    )
    return {
        "ciphertext": _b64(combined[:-16]),
        "iv": _b64(iv),
        "tag": _b64(combined[-16:]),
        "algorithm": "aes-256-gcm",
    }


def _encrypt_domain(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    full = _encrypt_value(value, key)
    segments: dict[str, Any] = {}
    root: dict[str, Any] = {}
    for name, item in value.items():
        if isinstance(item, (dict, list)) and name != "root":
            segments[name] = _encrypt_value(item, key)
        else:
            root[name] = item
    if root or not segments:
        segments["root"] = _encrypt_value(root, key)
    return {**full, "segments": segments}


def _decrypt_domain(blob: dict[str, Any], key: bytes) -> dict[str, Any]:
    segments = blob.get("segments") or {}
    if not segments:
        value = _decrypt_blob(blob, key)
        return value if isinstance(value, dict) else {}
    output: dict[str, Any] = {}
    for segment_id, segment in segments.items():
        value = _decrypt_blob(segment, key)
        if segment_id == "root" and isinstance(value, dict):
            output.update(value)
        else:
            output[segment_id] = value
    return output


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _path_exists(value: dict[str, Any], path: str) -> bool:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    return True


def _read_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise PkmBridgeError("The requested PKM scope does not exist.")
        current = current[part]
    return copy.deepcopy(current)


def _count_materialized_leaves(value: Any, path: tuple[str, ...] = ()) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, (bool, int, float)):
        return 1
    if isinstance(value, list):
        return sum(
            _count_materialized_leaves(item, (*path, "_items"))
            for item in value
        )
    if not isinstance(value, dict):
        return 0
    count = 0
    for raw_key, item in value.items():
        key = str(raw_key).strip().lower()
        if not key or key in _BLOCKED_EXTERNAL_PATH_PARTS:
            continue
        count += _count_materialized_leaves(item, (*path, key))
    return count


def _patch_leaf_paths(value: Any, prefix: str = "") -> list[str]:
    if not isinstance(value, dict) or not value:
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        paths.extend(_patch_leaf_paths(item, path))
    return paths


def _patch_has_delete(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return any(_patch_has_delete(item) for item in value.values())


def _path_descriptors(value: Any, prefix: str = "") -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    segment_id = prefix.split(".", 1)[0] if prefix else "root"
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.append({
                "json_path": path,
                "parent_path": prefix or None,
                "path_type": (
                    "array"
                    if isinstance(item, list)
                    else "object"
                    if isinstance(item, dict)
                    else "leaf"
                ),
                "exposure_eligibility": False,
                "segment_id": path.split(".", 1)[0] or segment_id,
                "source_agent": "hussh_one_hermes_local_planner",
            })
            paths.extend(_path_descriptors(item, path))
    elif isinstance(value, list):
        for item in value:
            path = f"{prefix}._items" if prefix else "_items"
            paths.append({
                "json_path": path,
                "parent_path": prefix or None,
                "path_type": (
                    "array"
                    if isinstance(item, list)
                    else "object"
                    if isinstance(item, dict)
                    else "leaf"
                ),
                "exposure_eligibility": False,
                "segment_id": segment_id,
                "source_agent": "hussh_one_hermes_local_planner",
            })
            paths.extend(_path_descriptors(item, path))
    return list({item["json_path"]: item for item in paths}.values())


def _handle(user_id: str, domain: str, scope: str) -> str:
    digest = hashlib.sha256(f"{user_id}:{domain}:{scope}".encode()).hexdigest()[:12]
    return f"s_{digest}"


def _sharing_fingerprint(impact: dict[str, Any]) -> dict[str, Any]:
    """Compare only the reviewable, authority-bearing sharing fields."""
    return {
        "active_recipient_count": int(impact.get("active_recipient_count") or 0),
        "recipient_labels": sorted(
            str(item) for item in (impact.get("recipient_labels") or [])
        ),
        "enters_next_export_revision": bool(impact.get("enters_next_export_revision")),
        "summary": str(impact.get("summary") or ""),
        "affected_grant_ids": sorted(
            str(item) for item in (impact.get("affected_grant_ids") or [])
        ),
        "affected_export_ids": sorted(
            str(item) for item in (impact.get("affected_export_ids") or [])
        ),
    }


class PkmClient:
    def __init__(self, bridge: HusshVaultBridge) -> None:
        self.bridge = bridge

    def _state(self):
        state = self.bridge.identity.read_state()
        if state is None:
            raise PkmBridgeError("Connect this Hermes profile to Hussh One first.")
        return state

    def _snapshot(self, *, domain: str, owner_token: str) -> dict[str, Any] | None:
        state = self._state()
        response = self.bridge.http.get(
            f"{state.api_base}/api/pkm/domain-snapshot/{state.user_id}/{domain}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if response.status_code == 404:
            self.bridge.replica.delete_domain(domain)
            return None
        response.raise_for_status()
        snapshot = response.json()
        self.bridge.replica.store_snapshot(domain, snapshot)
        return snapshot

    def sync_encrypted_replica(self) -> dict[str, Any]:
        """Apply metadata events by fetching only current encrypted snapshots."""
        state = self._state()
        owner_token = self.bridge.acquire_vault_owner_token()
        applied = 0
        while True:
            response = self.bridge.http.get(
                f"{state.api_base}/api/pkm/device-sync/{state.user_id}",
                headers={"Authorization": f"Bearer {owner_token}"},
                params={
                    "after_cursor": self.bridge.replica.cursor(),
                    "limit": 100,
                },
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get("events") or []
            for event in events:
                cursor = int(event.get("cursor") or 0)
                domain = str(event.get("domain") or "").strip().lower()
                if not domain or not _DOMAIN_RE.fullmatch(domain):
                    raise PkmBridgeError("The PKM sync feed returned an invalid domain.")
                if event.get("operation") == "delete":
                    self.bridge.replica.delete_domain(domain)
                else:
                    self._snapshot(domain=domain, owner_token=owner_token)
                self.bridge.replica.advance(cursor)
                applied += 1
            if not payload.get("has_more") or not events:
                return {
                    "success": True,
                    "events_applied": applied,
                    "cursor": self.bridge.replica.cursor(),
                }

    def list_domains(self) -> dict[str, Any]:
        """List owner PKM domains without decrypting their contents."""
        self.bridge.require_vault_key()
        state = self._state()
        owner_token = self.bridge.acquire_vault_owner_token()
        response = self.bridge.http.get(
            f"{state.api_base}/api/pkm/metadata/{state.user_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if response.status_code == 404:
            return {"success": True, "domains": []}
        response.raise_for_status()
        domains = []
        for item in response.json().get("domains") or []:
            key = str(item.get("key") or "").strip().lower()
            if key and _DOMAIN_RE.fullmatch(key):
                domains.append({
                    "domain": key,
                    "available_scopes": sorted(
                        {
                            str(scope)
                            for scope in (item.get("available_scopes") or [])
                            if isinstance(scope, str) and scope
                        }
                    ),
                    "attribute_count": max(0, int(item.get("attribute_count") or 0)),
                    "last_updated": item.get("last_updated"),
                })
        return {"success": True, "domains": sorted(domains, key=lambda item: item["domain"])}

    def read(self, *, domain: str, scope_path: str = "") -> dict[str, Any]:
        """Decrypt one owner domain, optionally narrowed to a requested scope."""
        normalized_domain = domain.strip().lower()
        normalized_scope = scope_path.strip().lower()
        if not _DOMAIN_RE.fullmatch(normalized_domain):
            raise PkmBridgeError("A normalized PKM domain is required.")
        if normalized_scope and not _SCOPE_RE.fullmatch(normalized_scope):
            raise PkmBridgeError("The requested PKM scope path is invalid.")
        key = self.bridge.require_vault_key()
        owner_token = self.bridge.acquire_vault_owner_token()
        snapshot = self._snapshot(domain=normalized_domain, owner_token=owner_token)
        if snapshot is None:
            raise PkmBridgeError("The requested PKM domain does not exist.")
        domain_data = _decrypt_domain(snapshot["encrypted_blob"], key)
        value = (
            _read_path(domain_data, normalized_scope)
            if normalized_scope
            else copy.deepcopy(domain_data)
        )
        serialized_size = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        materialized_leaves = _count_materialized_leaves(value)
        if (
            serialized_size > _MAX_NATIVE_READ_JSON_BYTES
            or materialized_leaves > _MAX_NATIVE_READ_LEAVES
        ):
            raise PkmBridgeError(
                "The requested PKM read is too broad. Request a narrower scope path."
            )
        return {
            "success": True,
            "domain": normalized_domain,
            "scope_path": normalized_scope or None,
            "content_revision": int(snapshot.get("content_revision") or 0),
            "materialized_leaf_count": materialized_leaves,
            "value": value,
        }

    def validate_readiness(self) -> dict[str, Any]:
        """Validate current PKM ciphertext/manifest compatibility without saving."""
        key = self.bridge.require_vault_key()
        state = self._state()
        owner_token = self.bridge.acquire_vault_owner_token()
        domain_data = {"readiness": {"compatible": True}}
        manifest = self._manifest(
            domain_data=domain_data,
            scope_path="readiness",
            current=None,
        )
        response = self.bridge.http.post(
            f"{state.api_base}/api/pkm/store-domain/validate",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "user_id": state.user_id,
                "domain": "hermes_contract_probe",
                "encrypted_blob": _encrypt_domain(domain_data, key),
                "summary": {
                    "attribute_count": len(manifest["paths"]),
                    "top_level_scope_count": 1,
                    "pkm_contract_version": _PKM_CONTRACT_VERSION,
                    "readable_projection_version": _READABLE_PROJECTION_VERSION,
                },
                "structure_decision": {
                    "action": "validate_contract",
                    "target_domain": "hermes_contract_probe",
                    "json_paths": [item["json_path"] for item in manifest["paths"]],
                    "top_level_scope_paths": ["readiness"],
                    "externalizable_paths": manifest["externalizable_paths"],
                    "summary_projection": manifest["summary_projection"],
                    "confidence": 1.0,
                    "source_agent": "hussh_one_hermes_local_planner",
                    "writer_id": "hussh_one_hermes",
                    "structure_agent_id": "hussh_one_hermes_local_planner",
                    "contract_version": 1,
                },
                "manifest": manifest,
                "source_agent": "hussh_one_hermes",
            },
        )
        response.raise_for_status()
        return {"compatible": bool(response.json().get("success"))}

    def propose(
        self,
        *,
        domain: str,
        scope_path: str,
        merge_patch: dict[str, Any],
        summary: str,
        operation: str = "upsert",
    ) -> PkmProposal:
        domain = domain.strip().lower()
        scope_path = scope_path.strip().lower()
        if not _DOMAIN_RE.fullmatch(domain) or not _SCOPE_RE.fullmatch(scope_path):
            raise PkmBridgeError(
                "A normalized top-level domain and scope path are required."
            )
        requested_operation = str(operation or "upsert").strip().lower()
        if requested_operation not in {
            "upsert",
            "create",
            "update",
            "merge",
            "delete_path",
            "delete_scope",
            "delete_domain",
        }:
            raise PkmBridgeError("The requested PKM operation is not supported.")
        if requested_operation == "delete_domain":
            merge_patch = {}
        elif not isinstance(merge_patch, dict) or not merge_patch:
            raise PkmBridgeError("A non-empty JSON merge patch is required.")
        changed_paths = _patch_leaf_paths(merge_patch)
        if requested_operation != "delete_domain" and (
            not changed_paths
            or any(
            path != scope_path and not path.startswith(f"{scope_path}.")
            for path in changed_paths
            )
        ):
            raise PkmBridgeError(
                "Every changed path must stay inside the reviewed PKM scope."
            )
        if (
            requested_operation in {"delete_path", "delete_scope"}
            and not _patch_has_delete(merge_patch)
        ):
            raise PkmBridgeError(
                "A delete operation must use a JSON merge-patch null at the reviewed scope."
            )
        if not summary.strip() or len(summary) > 500:
            raise PkmBridgeError("A concise mutation summary is required.")
        self.bridge.require_vault_key()
        owner_token = self.bridge.acquire_vault_owner_token()
        snapshot = self._snapshot(domain=domain, owner_token=owner_token)
        if requested_operation == "create" and snapshot is not None:
            raise PkmBridgeError("The requested PKM domain already exists.")
        if requested_operation in {
            "update",
            "merge",
            "delete_path",
            "delete_scope",
            "delete_domain",
        } and snapshot is None:
            raise PkmBridgeError("The requested PKM target does not exist.")
        state = self._state()
        impact_response = self.bridge.http.get(
            f"{state.api_base}/api/pkm/memory/mutation-impact/{state.user_id}/{domain}",
            headers={"Authorization": f"Bearer {owner_token}"},
            params={"scope_path": scope_path},
        )
        impact_response.raise_for_status()
        resolved_operation = requested_operation
        if requested_operation == "upsert":
            resolved_operation = "update" if snapshot else "create"
        return PkmProposal(
            proposal_id=f"pkm_proposal_{uuid.uuid4().hex}",
            domain=domain,
            scope_path=scope_path,
            operation=resolved_operation,
            summary=summary.strip(),
            merge_patch=copy.deepcopy(merge_patch),
            sharing_impact=impact_response.json(),
            source_revision=int(snapshot.get("content_revision") or 0)
            if snapshot
            else 0,
        )

    def _manifest(
        self,
        *,
        domain_data: dict[str, Any],
        scope_path: str,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        previous = (current or {}).get("manifest") or {}
        discovered_paths = _path_descriptors(domain_data)
        previous_by_path = {
            str(item.get("json_path")): item
            for item in (previous.get("paths") or [])
            if isinstance(item, dict) and item.get("json_path")
        }
        paths = []
        for item in discovered_paths:
            prior = previous_by_path.get(item["json_path"]) or {}
            paths.append({
                **item,
                **{
                    key: prior[key]
                    for key in (
                        "consent_label",
                        "sensitivity_label",
                        "source_agent",
                    )
                    if prior.get(key) is not None
                },
                "exposure_eligibility": bool(
                    prior.get("exposure_eligibility", item["path_type"] == "leaf")
                ),
            })
        externalizable = [
            item["json_path"]
            for item in paths
            if item["path_type"] == "leaf" and item["exposure_eligibility"]
        ]
        previous_paths = set(previous_by_path)
        has_new_paths = any(
            item["json_path"] not in previous_paths for item in discovered_paths
        )
        previous_manifest_version = int(previous.get("manifest_version") or 0)
        manifest_version = max(1, previous_manifest_version) + (
            1 if previous and has_new_paths else 0
        )
        scope_materialization = {}
        for raw_scope, value in domain_data.items():
            scope = str(raw_scope).strip().lower()
            if not scope or scope in _BLOCKED_EXTERNAL_PATH_PARTS:
                continue
            leaf_count = _count_materialized_leaves(value, (scope,))
            scope_materialization[scope] = {
                "state": "materialized" if leaf_count > 0 else "empty",
                "materialized_leaf_count": leaf_count,
            }
        return {
            "manifest_version": manifest_version,
            "domain_contract_version": max(
                1, int(previous.get("domain_contract_version") or 1)
            ),
            "pkm_contract_version": str(
                previous.get("pkm_contract_version") or _PKM_CONTRACT_VERSION
            ),
            "readable_summary_version": max(
                6, int(previous.get("readable_summary_version") or 0)
            ),
            "readable_projection_version": str(
                previous.get("readable_projection_version")
                or _READABLE_PROJECTION_VERSION
            ),
            "summary_projection": {
                "top_level_scope_count": len(domain_data),
                "attribute_count": len(paths),
                "path_count": len(paths),
                "externalizable_path_count": len(externalizable),
                "pkm_contract_version": _PKM_CONTRACT_VERSION,
                "readable_projection_version": _READABLE_PROJECTION_VERSION,
                "scope_materialization": scope_materialization,
            },
            "top_level_scope_paths": sorted(domain_data),
            "externalizable_paths": externalizable,
            "paths": paths,
            "source_agent": "hussh_one_hermes",
        }

    def _mutation_plan(
        self,
        *,
        proposal: PkmProposal,
        state,
        current: dict[str, Any] | None,
        impact: dict[str, Any],
    ) -> dict[str, Any]:
        plan_id = f"pkm_plan_{uuid.uuid4().hex}"
        top_level_scope = proposal.scope_path.split(".", 1)[0]
        handle = _handle(state.user_id, proposal.domain, top_level_scope)
        scopes = (current or {}).get("scopes") or []
        for scope in scopes:
            projection = scope.get("summary_projection") or {}
            if projection.get("top_level_scope_path") == top_level_scope:
                handle = str(scope.get("scope_handle") or handle)
                break
        operation = (
            "delete" if proposal.operation.startswith("delete_") else proposal.operation
        )
        if operation == "merge" and current is None:
            operation = "create"
        return {
            "version": 2,
            "plan_id": plan_id,
            "operation": operation,
            **({} if operation == "create" else {"source_scope_handle": handle}),
            **({} if operation == "delete" else {"target_scope_handle": handle}),
            "proposed_domain": proposal.domain,
            "proposed_scope": proposal.scope_path,
            "friendly_domain_name": proposal.domain.replace("_", " ").title(),
            "friendly_scope_name": proposal.scope_path.replace("_", " ").title(),
            "confidence": 1.0,
            "explanation": proposal.summary,
            "affected_grant_ids": impact.get("affected_grant_ids") or [],
            "affected_export_ids": impact.get("affected_export_ids") or [],
            "sharing_impact": {
                "active_recipient_count": int(
                    impact.get("active_recipient_count") or 0
                ),
                "recipient_labels": impact.get("recipient_labels") or [],
                "enters_next_export_revision": bool(
                    impact.get("enters_next_export_revision")
                ),
                "summary": str(
                    impact.get("summary") or "No active recipients are affected."
                ),
            },
            "semantic_contract_version": _PKM_CONTRACT_VERSION,
            "writer_id": "hussh_one_hermes",
            "structure_agent_id": "pkm_structure_agent",
            "source_revision": int((current or {}).get("content_revision") or 0),
            "confirmation_receipt": {
                "version": 2,
                "receipt_id": f"pkm_receipt_{uuid.uuid4().hex}",
                "plan_id": plan_id,
                "confirmed_by_user_id": state.user_id,
                "confirmed_at": datetime.now(UTC).isoformat(),
                "surface": "chat",
                "displayed_domain": proposal.domain,
                "displayed_scope": proposal.scope_path,
                "sharing_impact_acknowledged": True,
            },
        }

    def commit(self, proposal: PkmProposal) -> dict[str, Any]:
        key = self.bridge.require_vault_key()
        state = self._state()
        for attempt in range(3):
            owner_token = self.bridge.acquire_vault_owner_token()
            current = self._snapshot(domain=proposal.domain, owner_token=owner_token)
            current_revision = int((current or {}).get("content_revision") or 0)
            if current_revision != proposal.source_revision:
                raise PkmBridgeError(
                    "PKM changed after review. Create and confirm a new proposal."
                )
            current_data = (
                _decrypt_domain(current["encrypted_blob"], key) if current else {}
            )
            merged = (
                {}
                if proposal.operation == "delete_domain"
                else _merge_patch(current_data, proposal.merge_patch)
            )
            if not isinstance(merged, dict):
                raise PkmBridgeError(
                    "The proposed mutation must produce a JSON object."
                )
            if not proposal.operation.startswith("delete_") and not _path_exists(
                merged, proposal.scope_path
            ):
                raise PkmBridgeError(
                    "The proposed mutation must retain the reviewed top-level scope."
                )
            impact_response = self.bridge.http.get(
                f"{state.api_base}/api/pkm/memory/mutation-impact/{state.user_id}/{proposal.domain}",
                headers={"Authorization": f"Bearer {owner_token}"},
                params={"scope_path": proposal.scope_path},
            )
            impact_response.raise_for_status()
            impact = impact_response.json()
            if _sharing_fingerprint(impact) != _sharing_fingerprint(
                proposal.sharing_impact
            ):
                raise PkmBridgeError(
                    "Sharing changed after review. Create and confirm a new proposal."
                )
            if proposal.operation.startswith("delete_") and not merged:
                response = self.bridge.http.post(
                    f"{state.api_base}/api/pkm/delete-domain",
                    headers={"Authorization": f"Bearer {owner_token}"},
                    json={
                        "user_id": state.user_id,
                        "domain": proposal.domain,
                        "expected_data_version": current_revision,
                        "mutation_plan": self._mutation_plan(
                            proposal=proposal,
                            state=state,
                            current=current,
                            impact=impact,
                        ),
                    },
                )
                if response.status_code == 409 and attempt < 2:
                    continue
                response.raise_for_status()
                result = response.json()
                self.bridge.replica.delete_domain(proposal.domain)
                return {
                    "success": True,
                    "domain": proposal.domain,
                    "data_version": result.get("data_version"),
                    "deleted": True,
                    "exports_marked_for_refresh": bool(
                        impact.get("enters_next_export_revision")
                    ),
                }

            manifest = self._manifest(
                domain_data=merged, scope_path=proposal.scope_path, current=current
            )
            previous_manifest = (current or {}).get("manifest") or {}
            structure_action = (
                "create_domain"
                if current is None
                else (
                    "extend_domain"
                    if int(manifest["manifest_version"])
                    > int(previous_manifest.get("manifest_version") or 0)
                    else "match_existing_domain"
                )
            )
            payload = {
                "user_id": state.user_id,
                "domain": proposal.domain,
                "encrypted_blob": _encrypt_domain(merged, key),
                "summary": {
                    "attribute_count": len(manifest["paths"]),
                    "top_level_scope_count": len(merged),
                    "pkm_contract_version": _PKM_CONTRACT_VERSION,
                    "readable_projection_version": _READABLE_PROJECTION_VERSION,
                },
                "structure_decision": {
                    "action": structure_action,
                    "target_domain": proposal.domain,
                    "json_paths": [item["json_path"] for item in manifest["paths"]],
                    "top_level_scope_paths": manifest["top_level_scope_paths"],
                    "externalizable_paths": manifest["externalizable_paths"],
                    "summary_projection": manifest["summary_projection"],
                    "confidence": 1.0,
                    "source_agent": "hussh_one_hermes_local_planner",
                    "writer_id": "hussh_one_hermes",
                    "structure_agent_id": "hussh_one_hermes_local_planner",
                    "contract_version": 1,
                },
                "manifest": manifest,
                "source_agent": "hussh_one_hermes",
                "expected_data_version": int(current["content_revision"])
                if current
                else None,
                "mutation_plan": self._mutation_plan(
                    proposal=proposal,
                    state=state,
                    current=current,
                    impact=impact,
                ),
            }
            validation = self.bridge.http.post(
                f"{state.api_base}/api/pkm/store-domain/validate",
                headers={"Authorization": f"Bearer {owner_token}"},
                json=payload,
            )
            validation.raise_for_status()
            response = self.bridge.http.post(
                f"{state.api_base}/api/pkm/store-domain",
                headers={"Authorization": f"Bearer {owner_token}"},
                json=payload,
            )
            if response.status_code == 409 and attempt < 2:
                continue
            response.raise_for_status()
            result = response.json()
            return {
                "success": bool(result.get("success")),
                "domain": proposal.domain,
                "data_version": result.get("data_version"),
                "exports_marked_for_refresh": bool(
                    impact.get("enters_next_export_revision")
                ),
            }
        raise PkmBridgeError("PKM changed repeatedly while saving. Try again.")
