# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Typed contracts for the local Hussh One Source Library operon."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


SourceKind = Literal["icloud_drive", "google_drive"]
EntryState = Literal["available", "metadata_only", "not_materialized"]


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
