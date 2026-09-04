# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Typed contracts for the local Hussh One Source Library operon."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


SourceKind = Literal["icloud_drive", "google_drive", "local_drive"]
EntryState = Literal["available", "metadata_only", "not_materialized"]
BindingAccess = Literal["observe", "manage"]
FileOperationKind = Literal["create", "rename", "move", "overwrite", "trash"]
ShareMode = Literal[
    "reference_existing", "copy_revision", "move_original", "knowledge_snapshot"
]


@dataclass(frozen=True)
class SourceBinding:
    source_id: str
    source_kind: SourceKind
    label: str
    root_locator: str
    root_device: int
    root_inode: int
    created_at: str
    read_only: bool = True
    access_mode: BindingAccess = "observe"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "SourceBinding":
        return cls(**value)


@dataclass(frozen=True)
class CatalogEntry:
    entry_id: str
    source_id: str
    relative_path: str
    display_name: str
    suffix: str
    size_bytes: int
    modified_ns: int
    device: int
    inode: int
    state: EntryState
    media_kind: str
    content_revision: str
    content_hash: str | None = None
    artifact_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "CatalogEntry":
        return cls(**value)

    def metadata_view(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "display_name": self.display_name,
            "suffix": self.suffix,
            "size_bytes": self.size_bytes,
            "modified_ns": self.modified_ns,
            "state": self.state,
            "media_kind": self.media_kind,
            "content_revision": self.content_revision,
        }


@dataclass(frozen=True)
class ScanLimits:
    max_entries: int = 2_000
    max_depth: int = 16
    max_seconds: float = 10.0

    def validate(self) -> None:
        if not 1 <= self.max_entries <= 10_000:
            raise ValueError("max_entries must be between 1 and 10000.")
        if not 1 <= self.max_depth <= 32:
            raise ValueError("max_depth must be between 1 and 32.")
        if not 0.1 <= self.max_seconds <= 60.0:
            raise ValueError("max_seconds must be between 0.1 and 60.")


@dataclass(frozen=True)
class ReadLimits:
    max_source_bytes: int = 8 * 1024 * 1024
    max_text_chars: int = 64_000

    def validate(self) -> None:
        if not 1 <= self.max_source_bytes <= 32 * 1024 * 1024:
            raise ValueError("max_source_bytes is outside the supported bound.")
        if not 1 <= self.max_text_chars <= 256_000:
            raise ValueError("max_text_chars is outside the supported bound.")


@dataclass(frozen=True)
class ExtractedArtifact:
    artifact_id: str
    entry_id: str
    content_revision: str
    content_hash: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class SourceLibraryMemoryV2:
    """Private, provider-neutral semantic/control memory.

    Device locators and source bytes deliberately live outside this payload.
    """

    schema_version: int
    roots: dict[str, dict[str, Any]]
    items: dict[str, dict[str, Any]]
    collections: dict[str, dict[str, Any]]
    knowledge: dict[str, dict[str, Any]]
    relationships: dict[str, dict[str, Any]]

    @classmethod
    def empty(cls) -> "SourceLibraryMemoryV2":
        return cls(
            schema_version=2,
            roots={},
            items={},
            collections={},
            knowledge={},
            relationships={},
        )

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "SourceLibraryMemoryV2":
        if not isinstance(value, dict):
            raise ValueError("Source Library memory must be a JSON object.")
        allowed = {
            "schema_version",
            "roots",
            "items",
            "collections",
            "knowledge",
            "relationships",
        }
        if set(value) - allowed:
            raise ValueError("Source Library memory contains an unsupported branch.")
        memory = cls(
            schema_version=int(value.get("schema_version", 2)),
            roots=value.get("roots", {}),
            items=value.get("items", {}),
            collections=value.get("collections", {}),
            knowledge=value.get("knowledge", {}),
            relationships=value.get("relationships", {}),
        )
        memory.validate()
        return memory

    def validate(self) -> None:
        """Reject source-plane details from durable private PKM memory."""
        if self.schema_version != 2:
            raise ValueError("Source Library memory schema_version must be 2.")
        branches = {
            "roots": self.roots,
            "items": self.items,
            "collections": self.collections,
            "knowledge": self.knowledge,
            "relationships": self.relationships,
        }
        if any(not isinstance(branch, dict) for branch in branches.values()):
            raise ValueError("Every Source Library memory branch must be an object.")
        self._validate_records(
            self.roots,
            prefix="root_",
            allowed={"logical_kind", "synchronization_posture", "lifecycle_state"},
        )
        self._validate_records(
            self.items,
            prefix="item_",
            allowed={
                "blob_ref",
                "revision",
                "availability",
                "semantic_type",
                "organization",
                "knowledge_refs",
                "lifecycle_state",
            },
        )
        self._validate_records(
            self.collections,
            prefix="collection_",
            allowed={"aliases", "tags", "ordering", "lifecycle_state", "item_refs"},
        )
        self._validate_records(
            self.relationships,
            prefix="relationship_",
            allowed={"kind", "from_ref", "to_ref", "created_at"},
        )
        knowledge_fields = {
            "kind",
            "statement",
            "confidence",
            "timestamp",
            "provenance_ref",
        }
        self._validate_records(
            self.knowledge,
            prefix="k_",
            allowed=knowledge_fields,
            exact=True,
        )

    @staticmethod
    def _validate_records(
        records: dict[str, dict[str, Any]],
        *,
        prefix: str,
        allowed: set[str],
        exact: bool = False,
    ) -> None:
        forbidden_fragments = {
            "path",
            "title",
            "filename",
            "provider",
            "locator",
            "artifact",
            "content_hash",
            "raw_extract",
        }
        for reference, record in records.items():
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ValueError("Source Library memory requires opaque references.")
            if not isinstance(record, dict):
                raise ValueError("Source Library memory records must be JSON objects.")
            keys = set(record)
            if keys - allowed or (exact and keys != allowed):
                raise ValueError("Source Library memory record fields are invalid.")
            if any(
                fragment in key.casefold()
                for key in keys
                for fragment in forbidden_fragments
            ):
                raise ValueError(
                    "Source-plane details cannot enter Source Library memory."
                )
