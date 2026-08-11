# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Filesystem-first sharing through owner-bound provider share targets."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Callable

from hermes_cli.hussh_one_pkm.pkm import PkmClient

from .policy import SOURCE_LIBRARY_MANIFEST_POLICY
from .service import SourceLibraryError, SourceLibraryService


class SourceShareDeclined(RuntimeError):
    pass


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _inside(relative_path: str, target_path: str) -> bool:
    item = PurePosixPath(relative_path)
    target = PurePosixPath(target_path)
    return item == target or target in item.parents


class SourceLibraryShareService:
    modes = frozenset({
        "reference_existing",
        "copy_revision",
        "move_original",
        "knowledge_snapshot",
    })

    def __init__(
        self, library: SourceLibraryService, *, approve: Callable[[str, str], str]
    ) -> None:
        self.library = library
        self.approve = approve

    def bind_target(
        self,
        *,
        source_id: str,
        relative_path: str,
        label: str,
        audience_label: str,
    ) -> dict[str, Any]:
        binding = self.library._binding(source_id)
        if binding.access_mode != "manage":
            raise SourceLibraryError(
                "A share target requires an explicitly manage-enabled source binding."
            )
        path = self.library.adapter.resolve_entry(binding, relative_path)
        if not path.is_dir():
            raise SourceLibraryError(
                "A share target must be an existing mounted folder."
            )
        normalized_label = label.strip()
        normalized_audience = audience_label.strip()
        if not normalized_label or len(normalized_label) > 120:
            raise SourceLibraryError("A concise share-target label is required.")
        if not normalized_audience or len(normalized_audience) > 160:
            raise SourceLibraryError(
                "A concise provider-managed audience label is required."
            )
        decision = self.approve(
            "\n".join([
                "Bind this existing mounted folder as a Source Library share target?",
                f"Label: {normalized_label}",
                f"Declared audience: {normalized_audience}",
                "Hermes cannot inspect or change the provider ACL for this folder.",
            ]),
            "Binding stores an encrypted local reference; it grants no new provider access.",
        )
        if decision != "accept":
            raise SourceShareDeclined("The share target binding was not approved.")
        target = {
            "target_id": f"share_target_{uuid.uuid4().hex}",
            "source_id": source_id,
            "relative_path": relative_path,
            "label": normalized_label,
            "audience_label": normalized_audience,
            "audience_authority": "provider_managed_unverified",
            "created_at": _now_ms(),
        }
        self.library.index.put_share_target(target)
        self.refresh_human_exposure()
        return {
            "success": True,
            "target_id": target["target_id"],
            "label": normalized_label,
            "audience_label": normalized_audience,
            "audience_verified": False,
        }

    def list_targets(self) -> dict[str, Any]:
        return {"success": True, "targets": self.library.index.list_share_targets()}

    def propose_share(
        self,
        *,
        target_id: str,
        mode: str,
        entry_id: str | None = None,
        destination_name: str | None = None,
        knowledge_id: str | None = None,
        knowledge_format: str = "markdown",
    ) -> dict[str, Any]:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in self.modes:
            raise SourceLibraryError("The requested share mode is unsupported.")
        target = self.library.index.get_share_target(target_id)
        entry = None
        if normalized_mode != "knowledge_snapshot":
            entry = self.library._entry(str(entry_id or ""))
        elif not str(knowledge_id or "").startswith("k_"):
            raise SourceLibraryError(
                "A reviewed Source Library knowledge id is required."
            )
        if normalized_mode == "reference_existing" and entry is not None:
            if entry.source_id != target["source_id"] or not _inside(
                entry.relative_path, str(target["relative_path"])
            ):
                raise SourceLibraryError(
                    "Reference sharing requires the item to already be inside the share target."
                )
        if (
            normalized_mode in {"copy_revision", "move_original"}
            and not destination_name
        ):
            destination_name = entry.display_name if entry else None
        if destination_name:
            destination_name = PurePosixPath(str(destination_name)).name
            if destination_name in {"", ".", ".."}:
                raise SourceLibraryError("A safe destination name is required.")
        if knowledge_format not in {"markdown", "json"}:
            raise SourceLibraryError("Knowledge snapshots support markdown or json.")
        proposal = {
            "proposal_id": f"source_share_proposal_{uuid.uuid4().hex}",
            "operation_kind": f"share:{normalized_mode}",
            "target_id": target_id,
            "mode": normalized_mode,
            "entry_id": entry.entry_id if entry else None,
            "base_revision": entry.content_revision if entry else None,
            "original_relative_path": entry.relative_path if entry else None,
            "destination_name": destination_name,
            "knowledge_id": knowledge_id,
            "knowledge_format": knowledge_format,
            "created_at": _now_ms(),
        }
        self.library.index.put_proposal(proposal)
        return {
            "success": True,
            "proposal_id": proposal["proposal_id"],
            "mode": normalized_mode,
            "item_ref": proposal["entry_id"],
            "target_id": target_id,
            "pinned_revision": proposal["base_revision"],
            "provider_acl_changed": False,
            "requires_fresh_owner_approval": True,
        }

    def commit_share(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.library.index.get_proposal(proposal_id)
        mode = str(proposal.get("mode") or "")
        if str(proposal.get("operation_kind")) != f"share:{mode}":
            raise SourceLibraryError("The share proposal is invalid.")
        target = self.library.index.get_share_target(str(proposal["target_id"]))
        entry = None
        if proposal.get("entry_id"):
            entry = self.library._entry(str(proposal["entry_id"]))
            if entry.content_revision != proposal.get("base_revision"):
                raise SourceLibraryError(
                    "The source changed after review. Create a new share proposal."
                )
        decision = self.approve(
            "\n".join([
                "Publish this Source Library item through an existing provider share target?",
                f"Mode: {mode}",
                f"Item reference: {proposal.get('entry_id') or proposal.get('knowledge_id')}",
                f"Target: {target['label']}",
                f"Declared provider-managed audience: {target['audience_label']}",
                "This does not grant or verify provider permissions.",
            ]),
            "A fresh owner approval is required for this one filesystem share operation.",
        )
        if decision != "accept":
            self._complete(proposal, "declined")
            raise SourceShareDeclined("The share was not approved.")
        proposal = self.library.index.claim_proposal(proposal_id)
        destination_relative_path = str(target["relative_path"])
        published_entry_id = entry.entry_id if entry else None
        if mode == "reference_existing" and entry is not None:
            pass
        elif mode in {"copy_revision", "move_original"} and entry is not None:
            destination_relative_path = (
                PurePosixPath(str(target["relative_path"]))
                / str(proposal["destination_name"])
            ).as_posix()
            source_binding = self.library._binding(entry.source_id)
            target_binding = self.library._binding(str(target["source_id"]))
            if target_binding.access_mode != "manage":
                raise SourceLibraryError(
                    "The share target no longer has manage authority."
                )
            if mode == "move_original" and source_binding.access_mode != "manage":
                raise SourceLibraryError(
                    "Moving the original requires manage authority."
                )
            if mode == "copy_revision":
                self.library.adapter.copy_entry(
                    source_binding,
                    entry,
                    destination_binding=target_binding,
                    destination_relative_path=destination_relative_path,
                    max_bytes=32 * 1024 * 1024,
                )
            else:
                self.library.adapter.move_entry(
                    source_binding,
                    entry,
                    destination_binding=target_binding,
                    destination_relative_path=destination_relative_path,
                )
        elif mode == "knowledge_snapshot":
            destination_relative_path, published_entry_id = (
                self._write_knowledge_snapshot(target, proposal)
            )
        else:
            raise SourceLibraryError("The share proposal is invalid.")
        target_scan = self.library.scan(source_id=str(target["source_id"]))
        for candidate in self.library.index.list_entries(str(target["source_id"])):
            if candidate.relative_path == destination_relative_path:
                published_entry_id = candidate.entry_id
                break
        if not published_entry_id:
            raise SourceLibraryError(
                "The published item was not observed after reconciliation."
            )
        now = _now_ms()
        share = {
            "share_ref": f"share_{uuid.uuid4().hex}",
            "entry_id": str(proposal.get("entry_id") or published_entry_id),
            "published_entry_id": published_entry_id,
            "target_id": target["target_id"],
            "mode": mode,
            "status": "provider_sync_pending",
            "pinned_revision": str(
                proposal.get("base_revision") or "knowledge_snapshot"
            ),
            "original_relative_path": proposal.get("original_relative_path"),
            "published_relative_path": destination_relative_path,
            "created_at": now,
            "updated_at": now,
            "provider_acl_changed": False,
        }
        self.library.index.put_share(share)
        self._complete(proposal, "provider_sync_pending", claimed=True)
        return {
            "success": True,
            "share_ref": share["share_ref"],
            "status": share["status"],
            "provider_acl_changed": False,
            "provider_completion_verified": False,
            "reconcile_change_count": len(target_scan.get("changes") or []),
        }

    def _write_knowledge_snapshot(
        self, target: dict[str, Any], proposal: dict[str, Any]
    ) -> tuple[str, None]:
        knowledge_id = str(proposal["knowledge_id"])
        result = PkmClient(
            self.library.bridge, manifest_policy=SOURCE_LIBRARY_MANIFEST_POLICY
        ).read(domain="source_library", scope_path=f"knowledge.{knowledge_id}")
        item = result.get("value")
        if not isinstance(item, dict):
            raise SourceLibraryError("The reviewed knowledge item is unavailable.")
        extension = ".json" if proposal["knowledge_format"] == "json" else ".md"
        name = str(proposal.get("destination_name") or f"{knowledge_id}{extension}")
        if not name.endswith(extension):
            name += extension
        destination = (PurePosixPath(str(target["relative_path"])) / name).as_posix()
        if proposal["knowledge_format"] == "json":
            content = json.dumps(item, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            content = (
                f"# Reviewed Source Library knowledge\n\n{item.get('statement', '')}\n\n"
                f"Confidence: {item.get('confidence')}\nTimestamp: {item.get('timestamp')}\n"
            ).encode("utf-8")
        self.library.adapter.create_file(
            self.library._binding(str(target["source_id"])),
            relative_path=destination,
            content=content,
        )
        return destination, None

    def refresh_human_exposure(self) -> dict[str, Any]:
        existing = {
            (share["target_id"], share.get("published_entry_id") or share["entry_id"])
            for share in self.library.index.list_shares(active_only=True)
        }
        detected = 0
        now = _now_ms()
        for target in self.library.index.list_share_targets():
            for entry in self.library.index.list_entries(str(target["source_id"])):
                if not _inside(entry.relative_path, str(target["relative_path"])):
                    continue
                if (target["target_id"], entry.entry_id) in existing:
                    continue
                digest = hashlib.sha256(
                    f"{target['target_id']}\x00{entry.entry_id}".encode()
                ).hexdigest()[:32]
                self.library.index.put_share({
                    "share_ref": f"human_share_{digest}",
                    "entry_id": entry.entry_id,
                    "published_entry_id": entry.entry_id,
                    "target_id": target["target_id"],
                    "mode": "reference_existing",
                    "status": "human_detected",
                    "pinned_revision": entry.content_revision,
                    "original_relative_path": entry.relative_path,
                    "published_relative_path": entry.relative_path,
                    "created_at": now,
                    "updated_at": now,
                    "provider_acl_changed": False,
                })
                detected += 1
        return {"success": True, "human_detected": detected}

    def list_active(self) -> dict[str, Any]:
        self.refresh_human_exposure()
        return {
            "success": True,
            "shares": self.library.index.list_shares(active_only=True),
        }

    def propose_revoke(
        self, *, share_ref: str, destination_relative_path: str | None = None
    ) -> dict[str, Any]:
        share = self.library.index.get_share(share_ref)
        if share.get("status") not in {
            "active",
            "provider_sync_pending",
            "human_detected",
        }:
            raise SourceLibraryError("The share is not active.")
        proposal = {
            "proposal_id": f"source_revoke_proposal_{uuid.uuid4().hex}",
            "operation_kind": "share:revoke",
            "share_ref": share_ref,
            "entry_id": share.get("published_entry_id") or share["entry_id"],
            "base_revision": self.library._entry(
                str(share.get("published_entry_id") or share["entry_id"])
            ).content_revision,
            "destination_relative_path": destination_relative_path,
            "created_at": _now_ms(),
        }
        self.library.index.put_proposal(proposal)
        return {
            "success": True,
            "proposal_id": proposal["proposal_id"],
            "share_ref": share_ref,
        }

    def commit_revoke(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.library.index.get_proposal(proposal_id)
        if proposal.get("operation_kind") != "share:revoke":
            raise SourceLibraryError("The revocation proposal is invalid.")
        share = self.library.index.get_share(str(proposal["share_ref"]))
        entry = self.library._entry(str(proposal["entry_id"]))
        if entry.content_revision != proposal.get("base_revision"):
            raise SourceLibraryError("The shared file changed after review.")
        decision = self.approve(
            f"Revoke {share['share_ref']} by removing its provider artifact?",
            "Removing only the SQLite mapping is not revocation; the mounted file must move or enter Trash.",
        )
        if decision != "accept":
            self._complete(proposal, "declined")
            raise SourceShareDeclined("The share revocation was not approved.")
        proposal = self.library.index.claim_proposal(proposal_id)
        binding = self.library._binding(entry.source_id)
        destination = str(proposal.get("destination_relative_path") or "")
        if destination:
            self.library.adapter.move_entry(
                binding,
                entry,
                destination_binding=binding,
                destination_relative_path=destination,
            )
        else:
            self.library.adapter.trash_entry(binding, entry)
        now = _now_ms()
        share.update(status="revocation_pending_provider_sync", updated_at=now)
        self.library.index.put_share(share)
        self._complete(proposal, "revocation_pending_provider_sync", claimed=True)
        self.library.scan(source_id=binding.source_id)
        return {
            "success": True,
            "share_ref": share["share_ref"],
            "status": share["status"],
            "provider_completion_verified": False,
        }

    def _complete(
        self, proposal: dict[str, Any], status: str, *, claimed: bool = False
    ) -> None:
        receipt = {
            "receipt_id": f"source_receipt_{uuid.uuid4().hex}",
            "proposal_id": proposal["proposal_id"],
            "operation_kind": proposal["operation_kind"],
            "status": status,
            "created_at": _now_ms(),
            "item_ref": proposal.get("entry_id"),
            "base_revision": proposal.get("base_revision"),
        }
        completion = (
            self.library.index.complete_claimed_proposal
            if claimed
            else self.library.index.complete_proposal
        )
        completion(
            str(proposal["proposal_id"]),
            status="committed" if "pending" in status else status,
            receipt=receipt,
        )
