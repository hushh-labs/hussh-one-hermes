# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Proposal/approval boundary for provider-authoritative file mutations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from .contracts import ReadLimits
from .service import SourceLibraryError, SourceLibraryService


class SourceOperationDeclined(RuntimeError):
    pass


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class SourceLibraryOperationService:
    def __init__(
        self, library: SourceLibraryService, *, approve: Callable[[str, str], str]
    ) -> None:
        self.library = library
        self.approve = approve

    def propose(
        self,
        *,
        operation_kind: str,
        entry_id: str | None = None,
        source_id: str | None = None,
        destination_relative_path: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        kind = operation_kind.strip().lower()
        if kind not in {"create", "rename", "move", "overwrite", "trash"}:
            raise SourceLibraryError("The requested file operation is unsupported.")
        if content is not None and len(content.encode("utf-8")) > 256_000:
            raise SourceLibraryError("File content exceeds the bounded mutation limit.")
        entry = None
        if kind != "create":
            entry = self.library._entry(str(entry_id or ""))
            source_id = entry.source_id
        binding = self.library._binding(str(source_id or ""))
        if binding.access_mode != "manage":
            raise SourceLibraryError(
                "This source is observe-only. Bind it again with explicit manage authority."
            )
        if kind in {"create", "rename", "move"}:
            self.library.adapter.resolve_destination(
                binding,
                str(destination_relative_path or ""),
                create_parents=False,
            )
        if kind in {"create", "overwrite"} and content is None:
            raise SourceLibraryError("This operation requires explicit text content.")
        proposal = {
            "proposal_id": f"source_op_{uuid.uuid4().hex}",
            "operation_kind": kind,
            "entry_id": entry.entry_id if entry else None,
            "source_id": binding.source_id,
            "base_revision": entry.content_revision if entry else None,
            "destination_relative_path": destination_relative_path,
            "content": content,
            "created_at": _now_ms(),
        }
        self.library.index.put_proposal(proposal)
        return {
            "success": True,
            "proposal_id": proposal["proposal_id"],
            "operation_kind": kind,
            "item_ref": proposal["entry_id"],
            "base_revision": proposal["base_revision"],
            "destination": destination_relative_path,
            "requires_fresh_owner_approval": True,
        }

    def commit(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.library.index.get_proposal(proposal_id)
        kind = str(proposal["operation_kind"])
        entry = None
        if proposal.get("entry_id"):
            entry = self.library._entry(str(proposal["entry_id"]))
            if entry.content_revision != proposal.get("base_revision"):
                raise SourceLibraryError(
                    "The source changed after review. Create a new operation proposal."
                )
        decision = self.approve(
            "\n".join([
                "Apply this Source Library file operation?",
                f"Operation: {kind}",
                f"Item reference: {proposal.get('entry_id') or 'new file'}",
                f"Destination: {proposal.get('destination_relative_path') or 'provider Trash'}",
                "The mounted provider file is authoritative; PKM stores no file copy.",
            ]),
            "A fresh owner approval is required for this single provider-file mutation.",
        )
        if decision != "accept":
            receipt = self._receipt(proposal, "declined")
            self.library.index.complete_proposal(
                proposal_id, status="declined", receipt=receipt
            )
            raise SourceOperationDeclined("The file operation was not approved.")
        # Claim only after approval, immediately before the provider side
        # effect. This is the durable exactly-once gate across concurrent
        # Hermes processes; an executing proposal is never replayed blindly.
        proposal = self.library.index.claim_proposal(proposal_id)
        binding = self.library._binding(str(proposal["source_id"]))
        if binding.access_mode != "manage":
            raise SourceLibraryError("The source no longer has manage authority.")
        destination = str(proposal.get("destination_relative_path") or "")
        content = str(proposal.get("content") or "").encode("utf-8")
        if kind == "create":
            self.library.adapter.create_file(
                binding, relative_path=destination, content=content
            )
        elif kind in {"rename", "move"} and entry is not None:
            self.library.adapter.move_entry(
                binding,
                entry,
                destination_binding=binding,
                destination_relative_path=destination,
            )
        elif kind == "overwrite" and entry is not None:
            self.library.adapter.atomic_overwrite(binding, entry, content=content)
        elif kind == "trash" and entry is not None:
            self.library.adapter.trash_entry(binding, entry)
        else:
            raise SourceLibraryError("The file proposal is invalid.")
        receipt = self._receipt(proposal, "provider_sync_pending")
        self.library.index.complete_claimed_proposal(
            proposal_id, status="committed", receipt=receipt
        )
        reconcile = self.library.scan(source_id=binding.source_id)
        return {
            "success": True,
            "operation_kind": kind,
            "status": "provider_sync_pending",
            "receipt_id": receipt["receipt_id"],
            "reconcile_change_count": len(reconcile.get("changes") or []),
            "provider_completion_verified": False,
        }

    @staticmethod
    def _receipt(proposal: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "receipt_id": f"source_receipt_{uuid.uuid4().hex}",
            "proposal_id": proposal["proposal_id"],
            "operation_kind": proposal["operation_kind"],
            "status": status,
            "created_at": _now_ms(),
            "item_ref": proposal.get("entry_id"),
            "base_revision": proposal.get("base_revision"),
        }
