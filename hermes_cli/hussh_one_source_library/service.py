# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Composition root for mounted sources, encrypted artifacts, and metadata search."""

from __future__ import annotations

import hashlib
import hmac
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
            profile_id=bridge.identity.profile_id,
            user_id=state.user_id,
        )
        self.store = store or EncryptedSourceStore(bridge.profile_home, self.crypto)
        self.adapter = adapter or MountedTreeAdapter()

    def bind_mounted_root(
        self, *, source_kind: str, label: str, root_path: str
    ) -> dict[str, Any]:
        self.bridge.require_vault_key()
        source_id = f"source_{uuid.uuid4().hex}"
        binding = self.adapter.bind(
            source_id=source_id,
            source_kind=source_kind,
            label=label,
            root=Path(root_path).expanduser(),
            created_at=datetime.now(UTC).isoformat(),
        )
        with self.store.edit() as state:
            state.setdefault("bindings", {})[source_id] = binding.to_json()
        return {
            "success": True,
            "source_id": source_id,
            "source_kind": binding.source_kind,
            "label": binding.label,
            "read_only": True,
        }

    def list_sources(self) -> dict[str, Any]:
        state = self.store.load()
        sources = []
        for raw in state.get("bindings", {}).values():
            binding = SourceBinding.from_json(raw)
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
                "status": status,
            })
        return {"success": True, "sources": sorted(sources, key=lambda item: item["source_id"])}

    def _binding(self, state: dict[str, Any], source_id: str) -> SourceBinding:
        raw = state.get("bindings", {}).get(source_id)
        if not isinstance(raw, dict):
            raise SourceLibraryError("The requested source binding does not exist.")
        return SourceBinding.from_json(raw)

    def _entry(self, state: dict[str, Any], entry_id: str) -> CatalogEntry:
        raw = state.get("entries", {}).get(entry_id)
        if not isinstance(raw, dict):
            raise SourceLibraryError("The requested source entry does not exist.")
        return CatalogEntry.from_json(raw)

    def scan(self, *, source_id: str, limits: ScanLimits | None = None) -> dict[str, Any]:
        initial_state = self.store.load()
        binding = self._binding(initial_state, source_id)
        effective_limits = limits or ScanLimits()
        entries = list(self.adapter.enumerate(binding, effective_limits))
        with self.store.edit() as state:
            # Re-resolve the binding inside the transaction so a concurrent
            # local rebind cannot be overwritten by stale scan output.
            current_binding = self._binding(state, source_id)
            if current_binding != binding:
                raise SourceLibraryError("The source binding changed during scanning.")
            old_entries = state.setdefault("entries", {})
            replacement: dict[str, Any] = {}
            for entry in entries:
                prior_raw = old_entries.get(entry.entry_id)
                if isinstance(prior_raw, dict):
                    prior = CatalogEntry.from_json(prior_raw)
                    if prior.content_revision == entry.content_revision:
                        entry = replace(
                            entry,
                            content_hash=prior.content_hash,
                            artifact_id=prior.artifact_id,
                        )
                replacement[entry.entry_id] = entry.to_json()
            state["entries"] = {
                key: value
                for key, value in old_entries.items()
                if value.get("source_id") != source_id
            }
            state["entries"].update(replacement)
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.state] = counts.get(entry.state, 0) + 1
        return {
            "success": True,
            "source_id": source_id,
            "entry_count": len(entries),
            "counts_by_state": counts,
            "limit_reached": len(entries) >= effective_limits.max_entries,
        }

    @staticmethod
    def _page(items: list[CatalogEntry], cursor: str | None, limit: int) -> tuple[list[CatalogEntry], str | None]:
        if not 1 <= limit <= 100:
            raise SourceLibraryError("limit must be between 1 and 100.")
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise SourceLibraryError("The browse cursor is invalid.") from exc
        if offset < 0:
            raise SourceLibraryError("The browse cursor is invalid.")
        page = items[offset : offset + limit]
        next_cursor = str(offset + len(page)) if offset + len(page) < len(items) else None
        return page, next_cursor

    def browse(
        self,
        *,
        source_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        state = self.store.load()
        if source_id is not None:
            self._binding(state, source_id)
        items = [
            CatalogEntry.from_json(raw)
            for raw in state.get("entries", {}).values()
            if source_id is None or raw.get("source_id") == source_id
        ]
        items.sort(key=lambda item: (item.source_id, item.relative_path.casefold(), item.relative_path))
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
        normalized = query.strip().casefold()
        if len(normalized) < 2 or len(normalized) > 200:
            raise SourceLibraryError("A search query between 2 and 200 characters is required.")
        state = self.store.load()
        if source_id is not None:
            self._binding(state, source_id)
        items = [
            CatalogEntry.from_json(raw)
            for raw in state.get("entries", {}).values()
            if (source_id is None or raw.get("source_id") == source_id)
            and normalized in str(raw.get("relative_path") or "").casefold()
        ]
        items.sort(key=lambda item: (item.source_id, item.relative_path.casefold(), item.relative_path))
        page, next_cursor = self._page(items, cursor, limit)
        return {
            "success": True,
            "entries": [entry.metadata_view() for entry in page],
            "next_cursor": next_cursor,
        }

    def _verify_entry_snapshot(self, binding: SourceBinding, entry: CatalogEntry) -> Path:
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
            raise SourceLibraryError("The source changed after scanning; scan it again.")
        return path

    def read(self, *, entry_id: str, limits: ReadLimits | None = None) -> dict[str, Any]:
        state = self.store.load()
        entry = self._entry(state, entry_id)
        binding = self._binding(state, entry.source_id)
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
        artifact_id = "art_" + hashlib.sha256(
            f"{entry.entry_id}:{entry.content_revision}:{content_hash}".encode()
        ).hexdigest()[:32]
        self.store.write_artifact(artifact_id, {
            "artifact_id": artifact_id,
            "entry_id": entry.entry_id,
            "content_revision": entry.content_revision,
            "content_hash": content_hash,
            "text": text,
            "truncated": truncated,
        })
        updated = replace(entry, content_hash=content_hash, artifact_id=artifact_id)
        with self.store.edit() as latest:
            current = self._entry(latest, entry_id)
            if current.content_revision != entry.content_revision:
                raise SourceLibraryError(
                    "The catalog changed while the source was being read; scan it again."
                )
            latest["entries"][entry_id] = updated.to_json()
        return {
            "success": True,
            "entry_id": entry.entry_id,
            "artifact_id": artifact_id,
            "content_revision": entry.content_revision,
            "text": text,
            "truncated": truncated,
            "untrusted_source_text": True,
        }

    def artifact_for_entry(self, entry_id: str) -> tuple[CatalogEntry, ExtractedArtifact]:
        state = self.store.load()
        entry = self._entry(state, entry_id)
        if not entry.artifact_id or not entry.content_hash:
            raise SourceLibraryError("Read this source entry before proposing knowledge.")
        binding = self._binding(state, entry.source_id)
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

    def provenance_ref(self, entry: CatalogEntry, artifact: ExtractedArtifact) -> str:
        message = (
            f"{entry.source_id}\x00{entry.entry_id}\x00"
            f"{entry.content_revision}\x00{artifact.content_hash}"
        ).encode()
        digest = hmac.new(self.crypto.key("provenance"), message, hashlib.sha256).hexdigest()
        return f"prov_{digest[:32]}"
