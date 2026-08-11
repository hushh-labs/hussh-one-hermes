# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Approval-gated PKM projection of owner-reviewed source-derived knowledge."""

from __future__ import annotations

import copy
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
            raise SourceLibraryError("A knowledge statement of at most 4000 characters is required.")
        if isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
            raise SourceLibraryError("Knowledge confidence must be between 0 and 1.")
        entry, artifact = self.library.artifact_for_entry(entry_id)
        forbidden_values = {
            entry.entry_id,
            entry.relative_path,
            entry.display_name,
            artifact.artifact_id,
        }
        if any(value and value.casefold() in statement.casefold() for value in forbidden_values):
            raise SourceLibraryError(
                "Derived knowledge cannot include a source title, path, entry id, or artifact id."
            )
        if re.search(r"(?:^|\s)/(?:Users|home|Volumes|private|mnt|opt|var)/", statement):
            raise SourceLibraryError("Derived knowledge cannot include a local filesystem path.")
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
        with self.library.store.edit() as state:
            state.setdefault("proposals", {})[proposal_id] = {
                "proposal_id": proposal_id,
                "entry_id": entry.entry_id,
                "artifact_id": artifact.artifact_id,
                "artifact_content_hash": artifact.content_hash,
                "content_revision": entry.content_revision,
                "knowledge_id": knowledge_id,
                "knowledge_item": copy.deepcopy(item),
                "pkm_proposal": asdict(pkm_proposal),
            }
            state.setdefault("provenance", {})[provenance_ref] = {
                "entry_id": entry.entry_id,
                "artifact_id": artifact.artifact_id,
                "content_revision": entry.content_revision,
                "content_hash": artifact.content_hash,
            }
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

    def commit(self, proposal_id: str) -> dict[str, Any]:
        state = self.library.store.load()
        raw = state.get("proposals", {}).get(proposal_id)
        if not isinstance(raw, dict):
            raise SourceLibraryError("The Source Library proposal does not exist.")
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
        decision = self.approve(
            "\n".join([
                "Save this reviewed source-derived knowledge to Hussh One?",
                "Domain: source_library",
                f"Path: knowledge.{raw['knowledge_id']}",
                f"Kind: {item['kind']}",
                f"Statement: {item['statement']}",
                "Only the derived knowledge and opaque provenance reference will enter PKM.",
            ]),
            "A fresh approval is required for this single encrypted PKM write.",
        )
        if decision != "accept":
            raise SourceKnowledgeDeclined("The Source Library PKM write was not approved.")
        pkm_proposal = PkmProposal(**raw["pkm_proposal"])
        result = self.client.commit(pkm_proposal)
        with self.library.store.edit() as refreshed:
            refreshed.get("proposals", {}).pop(proposal_id, None)
        return {
            "success": bool(result.get("success")),
            "domain": "source_library",
            "scope": "knowledge",
            "data_version": result.get("data_version"),
            "exports_marked_for_refresh": bool(result.get("exports_marked_for_refresh")),
        }
