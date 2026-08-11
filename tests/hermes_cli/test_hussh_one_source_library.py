# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cli.hussh_one_pkm.pkm import PkmBridgeError, PkmClient, PkmProposal
from hermes_cli.hussh_one_source_library.contracts import (
    ScanLimits,
    SourceLibraryMemoryV2,
)
from hermes_cli.hussh_one_source_library.crypto_store import (
    EncryptedSourceStore,
    SourcePlaneCrypto,
    SourceStoreError,
)
from hermes_cli.hussh_one_source_library.pkm_service import (
    SourceKnowledgeDeclined,
    SourceLibraryPkmService,
)
from hermes_cli.hussh_one_source_library.index_store import (
    SourceLibraryIndexError,
    SourceLibraryIndexStore,
)
from hermes_cli.hussh_one_source_library.operations import (
    SourceLibraryOperationService,
)
from hermes_cli.hussh_one_source_library.policy import SOURCE_LIBRARY_MANIFEST_POLICY
from hermes_cli.hussh_one_source_library.service import (
    SourceLibraryError,
    SourceLibraryService,
)
from hermes_cli.hussh_one_source_library.sharing import SourceLibraryShareService
from hermes_cli.hussh_one_source_library.steward import (
    FILE_STEWARD_CONTRACT,
    SOURCE_LIBRARY_STEWARD_CONTRACT,
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


def _bound_library(
    tmp_path: Path, *, access_mode: str = "observe"
) -> tuple[SourceLibraryService, Path, str]:
    source_root = tmp_path / "mounted"
    source_root.mkdir(parents=True)
    library = _library(tmp_path / "profile")
    bound = library.bind_mounted_root(
        source_kind="icloud_drive",
        label="Test fixture",
        root_path=str(source_root),
        access_mode=access_mode,
    )
    return library, source_root, bound["source_id"]


def test_scan_is_deterministic_bounded_and_never_follows_symlinks(
    tmp_path: Path,
) -> None:
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


def test_partial_scan_never_marks_unseen_items_missing(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    assert library.scan(source_id=source_id)["complete"] is True

    partial = library.scan(
        source_id=source_id,
        limits=ScanLimits(max_entries=1, max_depth=3, max_seconds=2),
    )

    assert partial["complete"] is False
    assert partial["stop_reason"] == "entry_limit"
    assert {
        item["display_name"] for item in library.browse(source_id=source_id)["entries"]
    } == {
        "a.txt",
        "b.txt",
    }
    assert library.sync_status()["checkpoints"][0]["status"] == "partial"


def test_rename_preserves_item_ref_and_records_rename(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "before.md"
    source.write_text("stable", encoding="utf-8")
    library.scan(source_id=source_id)
    original = library.browse(source_id=source_id)["entries"][0]

    source.rename(root / "after.md")
    reconciled = library.scan(source_id=source_id)
    renamed = library.browse(source_id=source_id)["entries"][0]

    assert renamed["entry_id"] == original["entry_id"]
    assert renamed["relative_path"] == "after.md"
    assert reconciled["changes"] == [
        {"entry_id": original["entry_id"], "change_kind": "renamed"}
    ]


def test_depth_limited_scan_is_explicitly_partial(tmp_path: Path) -> None:
    library, root, source_id = _bound_library(tmp_path)
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "hidden.md").write_text("hidden", encoding="utf-8")

    result = library.scan(
        source_id=source_id,
        limits=ScanLimits(max_entries=20, max_depth=1, max_seconds=2),
    )

    assert result["complete"] is False
    assert result["stop_reason"] == "depth_limit"
    assert result["entry_count"] == 0


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


def test_catalog_and_artifact_are_encrypted_and_aad_profile_bound(
    tmp_path: Path,
) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "private-note.md"
    source.write_text("fixture-private-content", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    read = library.read(entry_id=entry_id)

    index_bytes = b"".join(
        path.read_bytes()
        for path in library.index.root.glob("source-library.db*")
        if path.is_file()
    )
    artifact_path = library.store.artifact_root / f"{read['artifact_id']}.enc.json"
    artifact_bytes = artifact_path.read_bytes()
    assert b"private-note" not in index_bytes
    assert str(root).encode() not in index_bytes
    assert b"fixture-private-content" not in index_bytes + artifact_bytes

    other_crypto = SourcePlaneCrypto(
        vault_key_provider=lambda: bytes(range(32)),
        profile_id="profile-other",
        user_id="user-test",
    )
    other_store = EncryptedSourceStore(library.bridge.profile_home, other_crypto)
    other_index = SourceLibraryIndexStore(library.bridge.profile_home, other_crypto)
    with pytest.raises(SourceStoreError, match="integrity"):
        other_store.read_artifact(read["artifact_id"])
    with pytest.raises(SourceLibraryIndexError, match="integrity"):
        other_index.get_binding(source_id)

    with sqlite3.connect(library.index.path) as conn:
        sealed = conn.execute(
            "SELECT sealed_record FROM source_bindings WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        envelope = json.loads(sealed)
        envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
        conn.execute(
            "UPDATE source_bindings SET sealed_record=? WHERE source_id=?",
            (json.dumps(envelope), source_id),
        )
    with pytest.raises(SourceLibraryIndexError, match="integrity"):
        library.index.get_binding(source_id)


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


def test_source_library_memory_v2_is_private_and_provider_neutral() -> None:
    memory = SourceLibraryMemoryV2.from_json({
        "knowledge": {
            "k_reviewed": {
                "kind": "summary",
                "statement": "A reviewed planning conclusion.",
                "confidence": 0.9,
                "timestamp": "2026-08-10T00:00:00Z",
                "provenance_ref": "prov_opaque",
            }
        }
    })
    assert memory.schema_version == 2
    assert memory.roots == {}
    assert set(memory.knowledge) == {"k_reviewed"}

    with pytest.raises(ValueError, match="record fields"):
        SourceLibraryMemoryV2.from_json({
            "items": {
                "item_opaque": {
                    "blob_ref": "blob_opaque",
                    "revision": "revision_opaque",
                    "availability": "available",
                    "semantic_type": "document",
                    "organization": {},
                    "knowledge_refs": [],
                    "lifecycle_state": "active",
                    "provider_id": "drive-file-id-must-not-enter-pkm",
                }
            }
        })
    with pytest.raises(ValueError, match="record fields"):
        SourceLibraryMemoryV2.from_json({
            "knowledge": {
                "k_bad": {
                    "kind": "fact",
                    "statement": "Reviewed",
                    "confidence": 1.0,
                    "timestamp": "2026-08-10T00:00:00Z",
                    "provenance_ref": "prov_opaque",
                    "artifact_id": "art_private",
                }
            }
        })


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
    assert manifest["top_level_scope_paths"] == []
    assert manifest["externalizable_paths"] == []
    assert all(not item["exposure_eligibility"] for item in manifest["paths"])
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


def test_v2_item_sync_uses_only_opaque_private_control_fields(
    monkeypatch, tmp_path: Path
) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "provider-title.md"
    source.write_text("private source body", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.pkm_service.PkmClient",
        _FakePkmClient,
    )
    service = SourceLibraryPkmService(library, approve=lambda *_args: "accept")

    proposed = service.propose_item_sync(entry_id=entry_id)
    stored = library.index.get_proposal(proposed["proposal_id"])
    patch_text = json.dumps(stored["pkm_proposal"]["merge_patch"], sort_keys=True)
    assert stored["operation_kind"] == "memory_item_sync"
    assert proposed["scope_path"].startswith("items.item_")
    assert source.name not in patch_text
    assert str(root) not in patch_text
    assert source_id not in patch_text
    assert entry_id not in patch_text
    assert "private source body" not in patch_text
    assert "artifact_id" not in patch_text
    SOURCE_LIBRARY_MANIFEST_POLICY.validate_write(
        scope_path=stored["pkm_proposal"]["scope_path"],
        merge_patch=stored["pkm_proposal"]["merge_patch"],
    )

    committed = service.commit(proposed["proposal_id"])
    assert committed["success"] is True
    assert committed["scope"] == "items"


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
    stored = library.index.get_proposal(proposed["proposal_id"])["pkm_proposal"]
    exported_shape = json.dumps(stored["merge_patch"], sort_keys=True)
    assert "note.md" not in exported_shape
    assert entry_id not in exported_shape
    assert "art_" not in exported_shape
    assert set(stored["merge_patch"]) == {"knowledge"}
    with pytest.raises(SourceKnowledgeDeclined):
        declined.commit(proposed["proposal_id"])
    with pytest.raises(SourceLibraryIndexError, match="unavailable"):
        library.index.get_proposal(proposed["proposal_id"])

    accepted = SourceLibraryPkmService(library, approve=lambda *_args: "accept")
    approved = accepted.propose(
        entry_id=entry_id,
        kind="summary",
        statement="Owner-reviewed summary",
        confidence=0.8,
    )
    result = accepted.commit(approved["proposal_id"])
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


def test_catalog_update_is_compare_and_swap_and_proposal_claim_is_single_winner(
    tmp_path: Path,
) -> None:
    library, root, source_id = _bound_library(tmp_path)
    source = root / "note.md"
    source.write_text("one", encoding="utf-8")
    library.scan(source_id=source_id)
    stale = library.index.list_entries(source_id)[0]
    source.write_text("version two", encoding="utf-8")
    stale_cursor = library.index.checkpoint_cursor(source_id)
    stale_snapshot, complete, stop_reason = library.adapter.enumerate_snapshot(
        library._binding(source_id), ScanLimits()
    )
    library.scan(source_id=source_id)

    with pytest.raises(SourceLibraryIndexError, match="catalog changed"):
        library.index.update_entry(
            replace(stale, content_hash="stale"),
            expected_revision=stale.content_revision,
        )
    with pytest.raises(SourceLibraryIndexError, match="newer mirror"):
        library.index.reconcile_entries(
            source_id,
            stale_snapshot,
            complete=complete,
            stop_reason=stop_reason,
            expected_cursor=stale_cursor,
        )

    proposal = {
        "proposal_id": "source_op_single_winner",
        "operation_kind": "create",
        "source_id": source_id,
        "created_at": 1,
    }
    library.index.put_proposal(proposal)
    second_index = SourceLibraryIndexStore(library.bridge.profile_home, library.crypto)

    def claim(index: SourceLibraryIndexStore) -> str:
        try:
            index.claim_proposal(proposal["proposal_id"])
            return "claimed"
        except SourceLibraryIndexError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (library.index, second_index)))
    assert sorted(outcomes) == ["claimed", "rejected"]


def test_observe_authority_and_destination_race_fail_closed(tmp_path: Path) -> None:
    observed, _root, source_id = _bound_library(tmp_path / "observe")
    observe_operations = SourceLibraryOperationService(
        observed, approve=lambda *_args: "accept"
    )
    with pytest.raises(SourceLibraryError, match="observe-only"):
        observe_operations.propose(
            operation_kind="create",
            source_id=source_id,
            destination_relative_path="new.md",
            content="new",
        )

    library, root, source_id = _bound_library(tmp_path / "manage", access_mode="manage")
    operations = SourceLibraryOperationService(library, approve=lambda *_args: "accept")
    raced = operations.propose(
        operation_kind="create",
        source_id=source_id,
        destination_relative_path="new.md",
        content="hermes",
    )
    (root / "new.md").write_text("human", encoding="utf-8")
    with pytest.raises(Exception, match="already exists"):
        operations.commit(raced["proposal_id"])
    assert (root / "new.md").read_text(encoding="utf-8") == "human"
    with pytest.raises(SourceLibraryIndexError, match="unavailable"):
        library.index.get_proposal(raced["proposal_id"])

    created = operations.propose(
        operation_kind="create",
        source_id=source_id,
        destination_relative_path="created.md",
        content="created safely",
    )
    result = operations.commit(created["proposal_id"])
    assert result["status"] == "provider_sync_pending"
    assert (root / "created.md").read_text(encoding="utf-8") == "created safely"


def test_hermetic_mirror_query_private_pkm_publish_list_and_trash_revoke(
    monkeypatch, tmp_path: Path
) -> None:
    library, root, source_id = _bound_library(tmp_path, access_mode="manage")
    share_folder = root / "team-share"
    share_folder.mkdir()
    source = root / "draft.md"
    source.write_text("initial planning note", encoding="utf-8")

    initial = library.scan(source_id=source_id)
    assert initial["changes"][0]["change_kind"] == "created"
    original_ref = library.browse(source_id=source_id)["entries"][0]["entry_id"]

    source.write_text("human edited planning note", encoding="utf-8")
    modified = library.scan(source_id=source_id)
    assert modified["changes"] == [
        {"entry_id": original_ref, "change_kind": "modified"}
    ]
    renamed_source = root / "reviewed-plan.md"
    source.rename(renamed_source)
    renamed = library.scan(source_id=source_id)
    assert renamed["changes"] == [{"entry_id": original_ref, "change_kind": "renamed"}]
    search = library.search(query="reviewed-plan", source_id=source_id)
    assert search["entries"][0]["entry_id"] == original_ref
    library.read(entry_id=original_ref)

    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.pkm_service.PkmClient",
        _FakePkmClient,
    )
    pkm = SourceLibraryPkmService(library, approve=lambda *_args: "accept")
    item_sync = pkm.propose_item_sync(entry_id=original_ref)
    assert pkm.commit(item_sync["proposal_id"])["scope"] == "items"
    knowledge = pkm.propose(
        entry_id=original_ref,
        kind="summary",
        statement="The owner reviewed a planning note for team publication.",
        confidence=0.95,
    )
    assert pkm.commit(knowledge["proposal_id"])["scope"] == "knowledge"

    sharing = SourceLibraryShareService(library, approve=lambda *_args: "accept")
    target = sharing.bind_target(
        source_id=source_id,
        relative_path="team-share",
        label="Team share fixture",
        audience_label="Provider-managed test audience",
    )
    proposed_share = sharing.propose_share(
        target_id=target["target_id"],
        mode="copy_revision",
        entry_id=original_ref,
        destination_name="published-plan.md",
    )
    published = sharing.commit_share(proposed_share["proposal_id"])
    assert published["status"] == "provider_sync_pending"
    published_path = share_folder / "published-plan.md"
    assert published_path.read_text(encoding="utf-8") == "human edited planning note"
    active = sharing.list_active()["shares"]
    assert [record["share_ref"] for record in active] == [published["share_ref"]]

    def fake_trash(args, **_kwargs):
        Path(args[-1]).unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.mounted_tree.subprocess.run",
        fake_trash,
    )
    revoke = sharing.propose_revoke(share_ref=published["share_ref"])
    revoked = sharing.commit_revoke(revoke["proposal_id"])
    assert revoked["status"] == "revocation_pending_provider_sync"
    assert not published_path.exists()
    assert sharing.list_active()["shares"] == []
    assert renamed_source.exists()


def test_approved_file_overwrite_move_and_trash_are_revision_pinned(
    monkeypatch, tmp_path: Path
) -> None:
    library, root, source_id = _bound_library(tmp_path, access_mode="manage")
    source = root / "mutable.md"
    source.write_text("v1", encoding="utf-8")
    library.scan(source_id=source_id)
    entry_id = library.browse(source_id=source_id)["entries"][0]["entry_id"]
    operations = SourceLibraryOperationService(library, approve=lambda *_args: "accept")

    overwrite = operations.propose(
        operation_kind="overwrite", entry_id=entry_id, content="v2"
    )
    assert (
        operations.commit(overwrite["proposal_id"])["status"] == "provider_sync_pending"
    )
    assert source.read_text(encoding="utf-8") == "v2"

    # Parent creation is deliberate owner setup, never model-selected implicit expansion.
    (root / "organized").mkdir()
    moved = operations.propose(
        operation_kind="move",
        entry_id=entry_id,
        destination_relative_path="organized/mutable.md",
    )
    assert operations.commit(moved["proposal_id"])["status"] == "provider_sync_pending"
    moved_path = root / "organized" / "mutable.md"
    assert moved_path.read_text(encoding="utf-8") == "v2"

    def fake_trash(args, **_kwargs):
        Path(args[-1]).unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "hermes_cli.hussh_one_source_library.mounted_tree.subprocess.run",
        fake_trash,
    )
    trash = operations.propose(operation_kind="trash", entry_id=entry_id)
    assert operations.commit(trash["proposal_id"])["status"] == "provider_sync_pending"
    assert not moved_path.exists()


def test_file_steward_is_a_leaf_with_only_source_tools(monkeypatch) -> None:
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "delegated"

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    assert (
        run_file_steward(request="Find reviewed facts", parent_agent=object())
        == "delegated"
    )
    assert captured["role"] == "leaf"
    assert captured["background"] is False
    assert captured["_internal_toolsets"] == ["hussh_one_sources"]
    assert FILE_STEWARD_CONTRACT.toolsets == ("hussh_one_sources",)
    assert "terminal" in FILE_STEWARD_CONTRACT.context
    assert "untrusted data" in FILE_STEWARD_CONTRACT.context


class _ReviewerResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ReviewerSharedDriveHttp:
    """Deterministic encrypted-PKM transport for the non-production reviewer fixture."""

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None
        self.store_payload: dict[str, Any] | None = None

    def get(self, url: str, **_kwargs: Any) -> _ReviewerResponse:
        if "/domain-snapshot/" in url:
            if self.snapshot is None:
                return _ReviewerResponse(404, {})
            return _ReviewerResponse(200, self.snapshot)
        if "/memory/mutation-impact/" in url:
            return _ReviewerResponse(
                200,
                {
                    "active_recipient_count": 0,
                    "recipient_labels": [],
                    "enters_next_export_revision": False,
                    "summary": "No active recipients are affected.",
                    "affected_grant_ids": [],
                    "affected_export_ids": [],
                },
            )
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> _ReviewerResponse:
        if url.endswith("/api/pkm/store-domain/validate"):
            return _ReviewerResponse(200, {"success": True})
        if url.endswith("/api/pkm/store-domain"):
            payload = kwargs["json"]
            self.store_payload = payload
            self.snapshot = {
                "content_revision": 1,
                "manifest_revision": 1,
                "encrypted_blob": payload["encrypted_blob"],
                "manifest": payload["manifest"],
                "scopes": [],
            }
            return _ReviewerResponse(200, {"success": True, "data_version": 1})
        raise AssertionError(f"Unexpected POST {url}")


class _ReviewerReplica:
    def delete_domain(self, _domain: str) -> None:
        return None

    def store_snapshot(self, _domain: str, _snapshot: dict[str, Any]) -> None:
        return None


class _ReviewerSharedDriveBridge:
    """Non-production reviewer identity with no credentials or external I/O."""

    def __init__(self, profile_home: Path) -> None:
        self.profile_home = profile_home
        self.identity = SimpleNamespace(
            profile_id="reviewer-source-library-profile-v1",
            read_state=lambda: SimpleNamespace(
                user_id="reviewer-source-library-fixture-v1",
                api_base="https://reviewer-fixture.invalid",
            ),
        )
        self.http = _ReviewerSharedDriveHttp()
        self.replica = _ReviewerReplica()

    def require_vault_key(self) -> bytes:
        return bytes(range(32))

    def acquire_vault_owner_token(self) -> str:
        return "fixture-owner-token"


def test_reviewer_shared_drive_fixture_rehearses_source_to_encrypted_pkm(
    tmp_path: Path,
) -> None:
    """Prove the local Source Library flow without a real reviewer or cloud file."""
    bridge = _ReviewerSharedDriveBridge(tmp_path / "reviewer-profile")
    library = SourceLibraryService(bridge=bridge)  # type: ignore[arg-type]
    shared_drive_root = tmp_path / "shared-drives"
    shared_drive_root.mkdir()
    source = shared_drive_root / "reviewer-source.md"
    source_text = "Fixture evidence is retained only in the encrypted source plane."
    source.write_text(source_text, encoding="utf-8")

    binding = library.bind_mounted_root(
        source_kind="google_drive",
        label="Reviewer Shared Drive fixture",
        root_path=str(shared_drive_root),
    )
    scan = library.scan(source_id=binding["source_id"])
    assert scan["success"] is True
    assert scan["source_id"] == binding["source_id"]
    assert scan["entry_count"] == 1
    assert scan["counts_by_state"] == {"available": 1}
    assert scan["limit_reached"] is False
    assert scan["complete"] is True
    assert scan["changes"][0]["change_kind"] == "created"
    entry_id = library.browse(source_id=binding["source_id"])["entries"][0]["entry_id"]
    read = library.read(entry_id=entry_id)

    pkm = SourceLibraryPkmService(library, approve=lambda *_args: "accept")
    proposal = pkm.propose(
        entry_id=entry_id,
        kind="summary",
        statement="The owner has reviewed a durable source-derived planning summary.",
        confidence=0.9,
        timestamp="2026-08-10T00:00:00+00:00",
    )
    committed = pkm.commit(proposal["proposal_id"])
    assert committed == {
        "success": True,
        "domain": "source_library",
        "scope": "knowledge",
        "data_version": 1,
        "exports_marked_for_refresh": False,
    }

    read_back = PkmClient(bridge, manifest_policy=SOURCE_LIBRARY_MANIFEST_POLICY).read(
        domain="source_library", scope_path="knowledge"
    )
    knowledge = read_back["value"]
    assert set(knowledge) == {proposal["knowledge_id"]}
    assert knowledge[proposal["knowledge_id"]] == {
        "kind": "summary",
        "statement": "The owner has reviewed a durable source-derived planning summary.",
        "confidence": 0.9,
        "timestamp": "2026-08-10T00:00:00+00:00",
        "provenance_ref": proposal["provenance_ref"],
    }

    payload = bridge.http.store_payload
    assert payload is not None
    manifest = payload["manifest"]
    assert manifest["top_level_scope_paths"] == []
    assert manifest["externalizable_paths"] == []
    assert all(not item["exposure_eligibility"] for item in manifest["paths"])
    transmitted = json.dumps(payload, sort_keys=True)
    assert source.name not in transmitted
    assert source_text not in transmitted
    assert str(shared_drive_root) not in transmitted
    assert read["artifact_id"] not in transmitted
