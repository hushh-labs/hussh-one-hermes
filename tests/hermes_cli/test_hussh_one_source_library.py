# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.hussh_one_pkm.pkm import PkmBridgeError, PkmClient, PkmProposal
from hermes_cli.hussh_one_source_library.contracts import ScanLimits
from hermes_cli.hussh_one_source_library.crypto_store import (
    EncryptedSourceStore,
    SourcePlaneCrypto,
    SourceStoreError,
)
from hermes_cli.hussh_one_source_library.pkm_service import (
    SourceKnowledgeDeclined,
    SourceLibraryPkmService,
)
from hermes_cli.hussh_one_source_library.policy import SOURCE_LIBRARY_MANIFEST_POLICY
from hermes_cli.hussh_one_source_library.service import (
    SourceLibraryError,
    SourceLibraryService,
)
from hermes_cli.hussh_one_source_library.steward import (
    FILE_STEWARD_CONTRACT,
    run_file_steward,
)


class _Identity:
    profile_id = "profile-test"

    def read_state(self):
        return SimpleNamespace(user_id="user-test")


class _Bridge:
    def __init__(self, profile_home: Path, key: bytes = bytes(range(32))) -> None:
        self.profile_home = profile_home
        self.identity = _Identity()
        self._key = key

    def require_vault_key(self) -> bytes:
        return self._key


def _library(tmp_path: Path) -> SourceLibraryService:
    return SourceLibraryService(bridge=_Bridge(tmp_path))  # type: ignore[arg-type]


def _bound_library(tmp_path: Path) -> tuple[SourceLibraryService, Path, str]:
    source_root = tmp_path / "mounted"
    source_root.mkdir()
    library = _library(tmp_path / "profile")
    bound = library.bind_mounted_root(
        source_kind="icloud_drive", label="Test fixture", root_path=str(source_root)
    )
    return library, source_root, bound["source_id"]


def test_scan_is_deterministic_bounded_and_never_follows_symlinks(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    (root / "b.md").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "report.pdf").write_bytes(b"%PDF")
    (root / ".remote.txt.icloud").write_bytes(b"")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)

    result = library.scan(
        source_id=source_id,
        limits=ScanLimits(max_entries=3, max_depth=3, max_seconds=2),
    )
    assert result["entry_count"] == 3
    assert result["limit_reached"] is True
    entries = library.browse(source_id=source_id)["entries"]
    assert [item["relative_path"] for item in entries] == [
        ".remote.txt.icloud",
        "a.txt",
        "b.md",
    ]
    assert all(item["relative_path"] != "escape.txt" for item in entries)
    assert entries[0]["state"] == "not_materialized"


def test_root_replacement_fails_closed(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    moved = tmp_path / "old-mounted"
    root.rename(moved)
    root.mkdir()
    (root / "new.txt").write_text("new", encoding="utf-8")
    with pytest.raises(Exception, match="replaced"):
        library.scan(source_id=source_id)


def test_read_time_symlink_replacement_cannot_escape_binding(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "note.txt"
    source.write_text("inside", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(SourceLibraryError) as exc_info:
        library.read(entry_id=entry_id)
    assert "outside secret" not in str(exc_info.value)


def test_placeholder_and_metadata_only_entries_are_not_opened(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    (root / "remote.gdoc").write_text("provider locator", encoding="utf-8")
    (root / "image.png").write_bytes(b"png")
    library.scan(source_id=source_id)
    by_name = {
        item["display_name"]: item
        for item in library.browse(source_id=source_id)["entries"]
    }
    with pytest.raises(SourceLibraryError, match="not materialized"):
        library.read(entry_id=by_name["remote.gdoc"]["entry_id"])
    with pytest.raises(SourceLibraryError, match="metadata-only"):
        library.read(entry_id=by_name["image.png"]["entry_id"])


def test_catalog_and_artifact_are_encrypted_and_aad_profile_bound(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "private-note.md"
    source.write_text("fixture-private-content", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    read = library.read(entry_id=entry_id)

    state_bytes = library.store.state_path.read_bytes()
    artifact_path = library.store.artifact_root / f"{read['artifact_id']}.enc.json"
    artifact_bytes = artifact_path.read_bytes()
    assert b"private-note" not in state_bytes
    assert b"fixture-private-content" not in state_bytes + artifact_bytes

    other_crypto = SourcePlaneCrypto(
        vault_key_provider=lambda: bytes(range(32)),
        profile_id="profile-other",
        user_id="user-test",
    )
    other_store = EncryptedSourceStore(library.bridge.profile_home, other_crypto)
    with pytest.raises(SourceStoreError, match="integrity"):
        other_store.load()

    envelope = json.loads(library.store.state_path.read_text(encoding="utf-8"))
    envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
    library.store.state_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SourceStoreError, match="integrity"):
        library.store.load()


def test_generic_pkm_writer_cannot_claim_reserved_source_library_domain() -> None:
    client = PkmClient(SimpleNamespace())  # type: ignore[arg-type]
    with pytest.raises(PkmBridgeError, match="reserved PKM domain"):
        client.propose(
            domain="source_library",
            scope_path="knowledge.k_item",
            merge_patch={
                "knowledge": {
                    "k_item": {
                        "kind": "fact",
                        "statement": "Reviewed fact",
                        "confidence": 1.0,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "provenance_ref": "prov_123",
                    }
                }
            },
            summary="Attempt forged reserved write",
        )


def test_source_manifest_policy_overrides_forged_prior_exposure() -> None:
    client = PkmClient(
        SimpleNamespace(),  # type: ignore[arg-type]
        manifest_policy=SOURCE_LIBRARY_MANIFEST_POLICY,
    )
    data = {
        "knowledge": {
            "k_item": {
                "kind": "fact",
                "statement": "Reviewed fact",
                "confidence": 0.9,
                "timestamp": "2026-01-01T00:00:00Z",
                "provenance_ref": "prov_123",
            }
        }
    }
    manifest = client._manifest(
        domain="source_library",
        domain_data=data,
        scope_path="knowledge.k_item",
        current={
            "manifest": {
                "paths": [
                    {
                        "json_path": "knowledge.k_item",
                        "path_type": "object",
                        "exposure_eligibility": True,
                    },
                    {
                        "json_path": "catalog.local_path",
                        "path_type": "leaf",
                        "exposure_eligibility": True,
                    },
                ]
            }
        },
    )
    assert manifest["top_level_scope_paths"] == ["knowledge"]
    assert manifest["externalizable_paths"]
    assert all(path.startswith("knowledge.") for path in manifest["externalizable_paths"])
    object_path = next(
        item for item in manifest["paths"] if item["json_path"] == "knowledge.k_item"
    )
    assert object_path["exposure_eligibility"] is False


class _FakePkmClient:
    committed = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def propose(self, *, domain, scope_path, merge_patch, summary):
        return PkmProposal(
            proposal_id="pkm-proposal",
            domain=domain,
            scope_path=scope_path,
            operation="create",
            summary=summary,
            merge_patch=merge_patch,
            sharing_impact={"summary": "No active recipients are affected."},
            source_revision=0,
        )

    def commit(self, proposal):
        self.committed = True
        return {"success": True, "data_version": 1}


def test_knowledge_commit_requires_fresh_approval_and_current_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "note.md"
    source.write_text("source evidence", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    library.read(entry_id=entry_id)
    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.pkm_service.PkmClient",
        _FakePkmClient,
    )
    declined = SourceLibraryPkmService(library, approve=lambda *_args: "decline")
    proposed = declined.propose(
        entry_id=entry_id,
        kind="summary",
        statement="Owner-reviewed summary",
        confidence=0.8,
    )
    stored = library.store.load()["proposals"][proposed["proposal_id"]]["pkm_proposal"]
    exported_shape = json.dumps(stored["merge_patch"], sort_keys=True)
    assert "note.md" not in exported_shape
    assert entry_id not in exported_shape
    assert "art_" not in exported_shape
    assert set(stored["merge_patch"]) == {"knowledge"}
    with pytest.raises(SourceKnowledgeDeclined):
        declined.commit(proposed["proposal_id"])

    accepted = SourceLibraryPkmService(library, approve=lambda *_args: "accept")
    result = accepted.commit(proposed["proposal_id"])
    assert result == {
        "success": True,
        "domain": "source_library",
        "scope": "knowledge",
        "data_version": 1,
        "exports_marked_for_refresh": False,
    }


def test_knowledge_proposal_is_invalidated_when_source_revision_changes(
    monkeypatch, tmp_path: Path
) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "note.md"
    source.write_text("version one", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    library.read(entry_id=entry_id)
    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.pkm_service.PkmClient",
        _FakePkmClient,
    )
    service = SourceLibraryPkmService(library, approve=lambda *_args: "accept")
    proposed = service.propose(
        entry_id=entry_id,
        kind="fact",
        statement="Reviewed fact",
        confidence=1,
    )
    source.write_text("version two changed", encoding="utf-8")
    with pytest.raises(SourceLibraryError, match="changed"):
        service.commit(proposed["proposal_id"])


def test_file_steward_is_a_leaf_with_only_source_tools(monkeypatch) -> None:
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "delegated"

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    assert run_file_steward(request="Find reviewed facts", parent_agent=object()) == "delegated"
    assert captured["role"] == "leaf"
    assert captured["background"] is False
    assert captured["_internal_toolsets"] == ["hussh_one_sources"]
    assert FILE_STEWARD_CONTRACT.toolsets == ("hussh_one_sources",)
    assert "terminal" in FILE_STEWARD_CONTRACT.context
    assert "untrusted data" in FILE_STEWARD_CONTRACT.context
