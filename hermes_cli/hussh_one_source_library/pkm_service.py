# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Approval-gated PKM projection of owner-reviewed source-derived knowledge."""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Callable

from hermes_cli.hussh_one_pkm.pkm import PkmClient, PkmProposal

from .policy import SOURCE_LIBRARY_MANIFEST_POLICY
from .service import SourceLibraryError, SourceLibraryService


class SourceKnowledgeDeclined(RuntimeError):
    pass


class SourceLibraryPkmService:
    def __init__(
        self,
        library: SourceLibraryService,
        *,
        approve: Callable[[str, str], str],
    ) -> None:
        self.library = library
        self.approve = approve
        self.client = PkmClient(
            library.bridge, manifest_policy=SOURCE_LIBRARY_MANIFEST_POLICY
        )

    def propose(
        self,
        *,
        entry_id: str,
        kind: str,
        statement: str,
        confidence: float,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        kind = kind.strip().lower()
        statement = statement.strip()
        if kind not in {"fact", "summary"}:
            raise SourceLibraryError("Knowledge kind must be fact or summary.")
        if not statement or len(statement) > 4_000:
            raise SourceLibraryError(
                "A knowledge statement of at most 4000 characters is required."
            )
        if isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
            raise SourceLibraryError("Knowledge confidence must be between 0 and 1.")
        entry, artifact = self.library.artifact_for_entry(entry_id)
        forbidden_values = {
            entry.entry_id,
            entry.relative_path,
            entry.display_name,
            artifact.artifact_id,
        }
        if any(
            value and value.casefold() in statement.casefold()
            for value in forbidden_values
        ):
            raise SourceLibraryError(
                "Derived knowledge cannot include a source title, path, entry id, or artifact id."
            )
        if re.search(
            r"(?:^|\s)/(?:Users|home|Volumes|private|mnt|opt|var)/", statement
        ):
            raise SourceLibraryError(
                "Derived knowledge cannot include a local filesystem path."
            )
        provenance_ref = self.library.provenance_ref(entry, artifact)
        knowledge_id = f"k_{uuid.uuid4().hex}"
        recorded_at = (timestamp or datetime.now(UTC).isoformat()).strip()
        item = {
            "kind": kind,
            "statement": statement,
            "confidence": round(float(confidence), 6),
            "timestamp": recorded_at,
            "provenance_ref": provenance_ref,
        }
        pkm_proposal = self.client.propose(
            domain="source_library",
            scope_path=f"knowledge.{knowledge_id}",
            merge_patch={"knowledge": {knowledge_id: item}},
            summary=f"Add one reviewed source-derived {kind}.",
        )
        proposal_id = f"source_proposal_{uuid.uuid4().hex}"
        local_proposal = {
            "proposal_id": proposal_id,
            "operation_kind": "knowledge_commit",
            "entry_id": entry.entry_id,
            "artifact_id": artifact.artifact_id,
            "artifact_content_hash": artifact.content_hash,
            "content_revision": entry.content_revision,
            "base_revision": entry.content_revision,
            "knowledge_id": knowledge_id,
            "knowledge_item": copy.deepcopy(item),
            "pkm_proposal": asdict(pkm_proposal),
            "created_at": int(datetime.now(UTC).timestamp() * 1000),
        }
        self.library.index.put_proposal(local_proposal)
        self.library.index.put_provenance(
            provenance_ref,
            {
                "entry_id": entry.entry_id,
                "artifact_id": artifact.artifact_id,
                "content_revision": entry.content_revision,
                "content_hash": artifact.content_hash,
            },
        )
        return {
            "success": True,
            "proposal_id": proposal_id,
            "knowledge_id": knowledge_id,
            "kind": kind,
            "statement": statement,
            "confidence": item["confidence"],
            "timestamp": recorded_at,
            "provenance_ref": provenance_ref,
            "sharing_impact": pkm_proposal.safe_view()["sharing_impact"],
        }

    def propose_item_sync(self, *, entry_id: str) -> dict[str, Any]:
        """Propose one bounded, provider-neutral V2 item control record.

        The durable record intentionally cannot reconstruct a filename, local
        path, provider identity, content digest, artifact, or source bytes.
        """
        entry = self.library._entry(entry_id)
        item_ref = self._opaque_ref("item", entry.entry_id)
        blob_ref = self._opaque_ref("blob", entry.entry_id)
        item = {
            "blob_ref": blob_ref,
            "revision": entry.content_revision,
            "availability": entry.state,
            "semantic_type": entry.media_kind,
            "organization": {},
            "knowledge_refs": [],
            "lifecycle_state": "active",
        }
        # Exercise the canonical V2 contract before any proposal is persisted.
        from .contracts import SourceLibraryMemoryV2

        SourceLibraryMemoryV2.from_json({"items": {item_ref: item}})
        pkm_proposal = self.client.propose(
            domain="source_library",
            scope_path=f"items.{item_ref}",
            merge_patch={"items": {item_ref: item}},
            summary="Synchronize one private provider-neutral Source Library item.",
        )
        proposal_id = f"source_proposal_{uuid.uuid4().hex}"
        local_proposal = {
            "proposal_id": proposal_id,
            "operation_kind": "memory_item_sync",
            "entry_id": entry.entry_id,
            "content_revision": entry.content_revision,
            "base_revision": entry.content_revision,
            "scope_path": f"items.{item_ref}",
            "item_ref": item_ref,
            "memory_item": copy.deepcopy(item),
            "pkm_proposal": asdict(pkm_proposal),
            "created_at": int(datetime.now(UTC).timestamp() * 1000),
        }
        self.library.index.put_proposal(local_proposal)
        return {
            "success": True,
            "proposal_id": proposal_id,
            "item_ref": item_ref,
            "scope_path": local_proposal["scope_path"],
            "requires_fresh_owner_approval": True,
            "sharing_impact": pkm_proposal.safe_view()["sharing_impact"],
        }

    def _opaque_ref(self, kind: str, value: str) -> str:
        digest = hmac.new(
            self.library.crypto.key(f"pkm-{kind}-ref"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{kind}_{digest[:32]}"

    def commit(self, proposal_id: str) -> dict[str, Any]:
        try:
            raw = self.library.index.get_proposal(proposal_id)
        except Exception as exc:
            raise SourceLibraryError(str(exc)) from exc
        operation_kind = str(raw.get("operation_kind") or "")
        if operation_kind not in {"knowledge_commit", "memory_item_sync"}:
            raise SourceLibraryError("The Source Library proposal is not executable.")
        if operation_kind == "knowledge_commit":
            entry, artifact = self.library.artifact_for_entry(str(raw["entry_id"]))
            if (
                entry.content_revision != raw.get("content_revision")
                or artifact.artifact_id != raw.get("artifact_id")
                or artifact.content_hash != raw.get("artifact_content_hash")
            ):
                raise SourceLibraryError(
                    "The source changed after review. Create a new knowledge proposal."
                )
            item = raw["knowledge_item"]
            approval_prompt = "\n".join([
                "Save this reviewed source-derived knowledge to Hussh One?",
                "Domain: source_library",
                f"Path: knowledge.{raw['knowledge_id']}",
                f"Kind: {item['kind']}",
                f"Statement: {item['statement']}",
                "Only the derived knowledge and opaque provenance reference will enter PKM.",
            ])
        else:
            entry = self.library._entry(str(raw["entry_id"]))
            if entry.content_revision != raw.get("content_revision"):
                raise SourceLibraryError(
                    "The source changed after review. Create a new memory sync proposal."
                )
            approval_prompt = "\n".join([
                "Synchronize this private Source Library control record to Hussh One?",
                f"Opaque item reference: {raw['item_ref']}",
                "No filename, path, provider id, content, digest, or artifact is included.",
                "This internal PKM capability exposes no consent scope.",
            ])
        decision = self.approve(
            approval_prompt,
            "A fresh approval is required for this single encrypted PKM write.",
        )
        if decision != "accept":
            receipt = {
                "receipt_id": f"source_receipt_{uuid.uuid4().hex}",
                "proposal_id": proposal_id,
                "operation_kind": operation_kind,
                "status": "declined",
                "created_at": int(datetime.now(UTC).timestamp() * 1000),
                "item_ref": raw["entry_id"],
                "base_revision": raw["content_revision"],
            }
            self.library.index.complete_proposal(
                proposal_id, status="declined", receipt=receipt
            )
            raise SourceKnowledgeDeclined(
                "The Source Library PKM write was not approved."
            )
        raw = self.library.index.claim_proposal(proposal_id)
        pkm_proposal = PkmProposal(**raw["pkm_proposal"])
        result = self.client.commit(pkm_proposal)
        receipt = {
            "receipt_id": f"source_receipt_{uuid.uuid4().hex}",
            "proposal_id": proposal_id,
            "operation_kind": operation_kind,
            "status": "committed",
            "created_at": int(datetime.now(UTC).timestamp() * 1000),
            "item_ref": raw["entry_id"],
            "base_revision": raw["content_revision"],
        }
        self.library.index.complete_claimed_proposal(
            proposal_id, status="committed", receipt=receipt
        )
        return {
            "success": bool(result.get("success")),
            "domain": "source_library",
            "scope": str(raw.get("scope_path") or "knowledge").split(".", 1)[0],
            "data_version": result.get("data_version"),
            "exports_marked_for_refresh": bool(
                result.get("exports_marked_for_refresh")
            ),
        }
