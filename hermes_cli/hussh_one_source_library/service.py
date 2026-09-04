# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Composition root for mounted sources, encrypted artifacts, and metadata search."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

from .contracts import (
    CatalogEntry,
    ExtractedArtifact,
    ReadLimits,
    ScanLimits,
    SourceBinding,
)
from .crypto_store import EncryptedSourceStore, SourcePlaneCrypto
from .extraction import SourceExtractionError, extract_bounded_bytes
from .index_store import SourceLibraryIndexError, SourceLibraryIndexStore
from .mounted_tree import MountedTreeAdapter, SourceAccessError


class SourceLibraryError(RuntimeError):
    pass


class SourceLibraryService:
    def __init__(
        self,
        *,
        bridge: HusshVaultBridge,
        store: EncryptedSourceStore | None = None,
        adapter: MountedTreeAdapter | None = None,
    ) -> None:
        state = bridge.identity.read_state()
        if state is None:
            raise SourceLibraryError("Connect this Hermes profile to Hussh One first.")
        self.bridge = bridge
        self.crypto = SourcePlaneCrypto(
            vault_key_provider=bridge.require_vault_key,
            device_custody_key_provider=bridge.require_source_library_custody_key,
            profile_id=bridge.identity.profile_id,
            user_id=state.user_id,
            device_id=str(getattr(state, "device_id", "")),
        )
        self.store = store or EncryptedSourceStore(bridge.profile_home, self.crypto)
        self.index = SourceLibraryIndexStore(bridge.profile_home, self.crypto)
        self.adapter = adapter or MountedTreeAdapter()
        self._custody_ready = False

    def _has_persisted_source_data(self) -> bool:
        return (
            self.store.state_path.exists()
            or any(self.store.artifact_root.glob("art_*.enc.json"))
            or self.index.has_persisted_records()
        )

    def _ensure_custody(self) -> None:
        """Open a source plane only after vault and device custody are present."""
        if self._custody_ready:
            return
        self.bridge.require_vault_key()
        self.bridge.require_source_library_custody_key(
            create_if_missing=not self._has_persisted_source_data()
        )
        if self.bridge.source_library_custody_phase() != 2:
            # v1 can be opened solely to migrate it. Once every legacy envelope
            # and keyed SQLite token is replaced, the Keychain phase latch makes
            # a replayed v1 database fail closed rather than silently downgrade.
            self.index.migrate_legacy(self.store)
            self.store.rekey_legacy_envelopes()
            self.index.rekey_legacy_envelopes()
            self.bridge.complete_source_library_custody_upgrade()
        self.crypto.reject_legacy_v1()
        self._custody_ready = True

    def has_binding_records(self) -> bool:
        """Side-effect-free availability hint for tool registration only."""
        return self.index.has_persisted_records()

    def bind_mounted_root(
        self,
        *,
        source_kind: str,
        label: str,
        root_path: str,
        access_mode: str = "observe",
    ) -> dict[str, Any]:
        self._ensure_custody()
        source_id = f"source_{uuid.uuid4().hex}"
        binding = self.adapter.bind(
            source_id=source_id,
            source_kind=source_kind,
            label=label,
            root=Path(root_path).expanduser(),
            created_at=datetime.now(UTC).isoformat(),
            access_mode=access_mode,
        )
        self.index.put_binding(binding)
        return {
            "success": True,
            "source_id": source_id,
            "source_kind": binding.source_kind,
            "label": binding.label,
            "read_only": binding.read_only,
            "access_mode": binding.access_mode,
        }

    def list_sources(self) -> dict[str, Any]:
        self._ensure_custody()
        sources = []
        for binding in self.index.list_bindings():
            try:
                self.adapter.validate(binding)
                status = "available"
            except SourceAccessError:
                status = "unavailable"
            sources.append({
                "source_id": binding.source_id,
                "source_kind": binding.source_kind,
                "label": binding.label,
                "read_only": binding.read_only,
                "access_mode": binding.access_mode,
                "status": status,
            })
        return {
            "success": True,
            "sources": sorted(sources, key=lambda item: item["source_id"]),
        }

    def _binding(self, source_id: str) -> SourceBinding:
        self._ensure_custody()
        try:
            return self.index.get_binding(source_id)
        except SourceLibraryIndexError as exc:
            raise SourceLibraryError(str(exc)) from exc

    def _entry(self, entry_id: str) -> CatalogEntry:
        self._ensure_custody()
        try:
            return self.index.get_entry(entry_id)
        except SourceLibraryIndexError as exc:
            raise SourceLibraryError(str(exc)) from exc

    def scan(
        self, *, source_id: str, limits: ScanLimits | None = None
    ) -> dict[str, Any]:
        binding = self._binding(source_id)
        starting_cursor = self.index.checkpoint_cursor(source_id)
        effective_limits = limits or ScanLimits()
        entries, complete, stop_reason = self.adapter.enumerate_snapshot(
            binding, effective_limits
        )
        current_binding = self._binding(source_id)
        if current_binding != binding:
            raise SourceLibraryError("The source binding changed during scanning.")
        try:
            changes = self.index.reconcile_entries(
                source_id,
                entries,
                complete=complete,
                stop_reason=stop_reason,
                expected_cursor=starting_cursor,
            )
        except SourceLibraryIndexError as exc:
            raise SourceLibraryError(str(exc)) from exc
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.state] = counts.get(entry.state, 0) + 1
        return {
            "success": True,
            "source_id": source_id,
            "entry_count": len(entries),
            "counts_by_state": counts,
            "limit_reached": not complete,
            "complete": complete,
            "stop_reason": stop_reason,
            "changes": changes,
        }

    @staticmethod
    def _page(
        items: list[CatalogEntry], cursor: str | None, limit: int
    ) -> tuple[list[CatalogEntry], str | None]:
        if not 1 <= limit <= 100:
            raise SourceLibraryError("limit must be between 1 and 100.")
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise SourceLibraryError("The browse cursor is invalid.") from exc
        if offset < 0:
            raise SourceLibraryError("The browse cursor is invalid.")
        page = items[offset : offset + limit]
        next_cursor = (
            str(offset + len(page)) if offset + len(page) < len(items) else None
        )
        return page, next_cursor

    def browse(
        self,
        *,
        source_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._ensure_custody()
        if source_id is not None:
            self._binding(source_id)
        items = self.index.list_entries(source_id)
        items.sort(
            key=lambda item: (
                item.source_id,
                item.relative_path.casefold(),
                item.relative_path,
            )
        )
        page, next_cursor = self._page(items, cursor, limit)
        return {
            "success": True,
            "entries": [entry.metadata_view() for entry in page],
            "next_cursor": next_cursor,
        }

    def search(
        self,
        *,
        query: str,
        source_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._ensure_custody()
        normalized = query.strip().casefold()
        if len(normalized) < 2 or len(normalized) > 200:
            raise SourceLibraryError(
                "A search query between 2 and 200 characters is required."
            )
        if source_id is not None:
            self._binding(source_id)
        query_terms = re.findall(r"[\w]+", normalized)
        items = [
            entry
            for entry in self.index.list_entries(source_id)
            if all(
                any(
                    term in token
                    for token in re.findall(r"[\w]+", entry.relative_path.casefold())
                )
                for term in query_terms
            )
        ]
        items.sort(
            key=lambda item: (
                item.source_id,
                item.relative_path.casefold(),
                item.relative_path,
            )
        )
        page, next_cursor = self._page(items, cursor, limit)
        return {
            "success": True,
            "entries": [entry.metadata_view() for entry in page],
            "next_cursor": next_cursor,
        }

    def _verify_entry_snapshot(
        self, binding: SourceBinding, entry: CatalogEntry
    ) -> Path:
        try:
            path = self.adapter.resolve_entry(binding, entry.relative_path)
        except SourceAccessError as exc:
            raise SourceLibraryError(str(exc)) from exc
        try:
            st = path.stat()
        except OSError as exc:
            raise SourceLibraryError("The source entry is unavailable.") from exc
        current = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        expected = (entry.device, entry.inode, entry.size_bytes, entry.modified_ns)
        if current != expected:
            raise SourceLibraryError(
                "The source changed after scanning; scan it again."
            )
        return path

    def read(
        self, *, entry_id: str, limits: ReadLimits | None = None
    ) -> dict[str, Any]:
        entry = self._entry(entry_id)
        binding = self._binding(entry.source_id)
        if entry.state == "not_materialized":
            raise SourceLibraryError(
                "The cloud source is not materialized locally; Source Library will not hydrate it."
            )
        if entry.state != "available":
            raise SourceLibraryError("This source type is metadata-only in V1.")
        self._verify_entry_snapshot(binding, entry)
        effective_limits = limits or ReadLimits()
        try:
            snapshot = self.adapter.read_snapshot(
                binding, entry, max_bytes=effective_limits.max_source_bytes
            )
            text, truncated = extract_bounded_bytes(
                snapshot, suffix=entry.suffix, limits=effective_limits
            )
        except (SourceAccessError, SourceExtractionError) as exc:
            raise SourceLibraryError(str(exc)) from exc
        self._verify_entry_snapshot(binding, entry)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        artifact_id = (
            "art_"
            + hashlib.sha256(
                f"{entry.entry_id}:{entry.content_revision}:{content_hash}".encode()
            ).hexdigest()[:32]
        )
        self.store.write_artifact(
            artifact_id,
            {
                "artifact_id": artifact_id,
                "entry_id": entry.entry_id,
                "content_revision": entry.content_revision,
                "content_hash": content_hash,
                "text": text,
                "truncated": truncated,
            },
        )
        updated = replace(entry, content_hash=content_hash, artifact_id=artifact_id)
        current = self._entry(entry_id)
        if current.content_revision != entry.content_revision:
            raise SourceLibraryError(
                "The catalog changed while the source was being read; scan it again."
            )
        try:
            self.index.update_entry(updated, expected_revision=entry.content_revision)
        except SourceLibraryIndexError as exc:
            raise SourceLibraryError(str(exc)) from exc
        return {
            "success": True,
            "entry_id": entry.entry_id,
            "artifact_id": artifact_id,
            "content_revision": entry.content_revision,
            "text": text,
            "truncated": truncated,
            "untrusted_source_text": True,
        }

    def artifact_for_entry(
        self, entry_id: str
    ) -> tuple[CatalogEntry, ExtractedArtifact]:
        entry = self._entry(entry_id)
        if not entry.artifact_id or not entry.content_hash:
            raise SourceLibraryError(
                "Read this source entry before proposing knowledge."
            )
        binding = self._binding(entry.source_id)
        self._verify_entry_snapshot(binding, entry)
        raw = self.store.read_artifact(entry.artifact_id)
        artifact = ExtractedArtifact(**raw)
        if (
            artifact.entry_id != entry.entry_id
            or artifact.content_revision != entry.content_revision
            or artifact.content_hash != entry.content_hash
        ):
            raise SourceLibraryError("The source artifact revision is stale.")
        return entry, artifact

    def sync_status(self) -> dict[str, Any]:
        self._ensure_custody()
        return {"success": True, "checkpoints": self.index.sync_status()}

    def provenance_ref(self, entry: CatalogEntry, artifact: ExtractedArtifact) -> str:
        self._ensure_custody()
        message = (
            f"{entry.source_id}\x00{entry.entry_id}\x00"
            f"{entry.content_revision}\x00{artifact.content_hash}"
        ).encode()
        digest = hmac.new(
            self.crypto.key("provenance"), message, hashlib.sha256
        ).hexdigest()
        return f"prov_{digest[:32]}"
