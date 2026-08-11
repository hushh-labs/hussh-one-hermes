# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Profile-scoped SQLite mapping and operation state for Source Library.

The database is deliberately metadata-only.  Sensitive device locators and
display metadata are sealed with the Source Library vault-derived key before
they cross the SQLite boundary; source bytes and extracted text never enter it.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.sqlite_util import write_txn

from .contracts import CatalogEntry, SourceBinding
from .crypto_store import EncryptedSourceStore, SourcePlaneCrypto


SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_library_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_bindings (
    source_id TEXT PRIMARY KEY,
    lifecycle_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_items (
    entry_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_bindings(source_id) ON DELETE CASCADE,
    content_revision TEXT NOT NULL,
    entry_state TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    modified_ns INTEGER NOT NULL,
    locator_token TEXT NOT NULL,
    dedup_token TEXT,
    sealed_record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_items_source_state
    ON observed_items(source_id, lifecycle_state, modified_ns DESC);
CREATE INDEX IF NOT EXISTS idx_source_items_locator
    ON observed_items(source_id, locator_token);
CREATE TABLE IF NOT EXISTS sync_checkpoints (
    source_id TEXT PRIMARY KEY REFERENCES source_bindings(source_id) ON DELETE CASCADE,
    cursor INTEGER NOT NULL,
    status TEXT NOT NULL,
    last_scan_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    entry_id TEXT,
    change_kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_changes_cursor
    ON change_events(source_id, created_at, event_id);
CREATE TABLE IF NOT EXISTS share_targets (
    target_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_bindings(source_id) ON DELETE CASCADE,
    lifecycle_state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS share_records (
    share_ref TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES share_targets(target_id) ON DELETE RESTRICT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    pinned_revision TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_shares_status
    ON share_records(status, target_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS operation_proposals (
    proposal_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    base_revision TEXT,
    created_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_receipts (
    receipt_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES operation_proposals(proposal_id) ON DELETE RESTRICT,
    operation_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_records (
    provenance_ref TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sealed_record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_ref TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(collection_ref, entry_id)
);
"""


class SourceLibraryIndexError(RuntimeError):
    pass


class SourceLibraryIndexStore:
    def __init__(self, profile_home: Path, crypto: SourcePlaneCrypto) -> None:
        self.root = profile_home / "hussh-one" / "source-library"
        self.path = self.root / "source-library.db"
        self.crypto = crypto
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="source-library.db")
        return conn

    def _initialize(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO source_library_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _encoded(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    def _seal(self, value: dict[str, Any], *, purpose: str, identifier: str) -> str:
        return json.dumps(
            self.crypto.seal(
                self._encoded(value), purpose=purpose, identifier=identifier
            ),
            separators=(",", ":"),
            sort_keys=True,
        )

    def _open(self, value: str, *, purpose: str, identifier: str) -> dict[str, Any]:
        try:
            envelope = json.loads(value)
            decoded = self.crypto.open(envelope, purpose=purpose, identifier=identifier)
            result = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise SourceLibraryIndexError(
                "The local Source Library index failed its integrity check."
            ) from exc
        if not isinstance(result, dict):
            raise SourceLibraryIndexError("The local Source Library index is invalid.")
        return result

    def migrate_legacy(self, legacy: EncryptedSourceStore) -> None:
        """Idempotently copy legacy catalog state without deleting rollback data."""
        with self._lock, self._connect() as conn:
            migrated = conn.execute(
                "SELECT value FROM source_library_meta WHERE key='legacy_migrated_revision'"
            ).fetchone()
        state = legacy.load()
        revision = str(int(state.get("revision") or 0))
        if migrated is not None and str(migrated["value"]) == revision:
            return
        bindings = [
            SourceBinding.from_json(value)
            for value in state.get("bindings", {}).values()
            if isinstance(value, dict)
        ]
        entries = [
            CatalogEntry.from_json(value)
            for value in state.get("entries", {}).values()
            if isinstance(value, dict)
        ]
        proposals = [
            value
            for value in state.get("proposals", {}).values()
            if isinstance(value, dict) and value.get("proposal_id")
        ]
        provenance = state.get("provenance", {})
        with self._lock, self._connect() as conn, write_txn(conn):
            for binding in bindings:
                self._upsert_binding(conn, binding)
            for entry in entries:
                self._upsert_entry(conn, entry, lifecycle_state="active")
            for proposal in proposals:
                proposal_id = str(proposal["proposal_id"])
                retired = {
                    **proposal,
                    "operation_kind": "legacy_knowledge_commit",
                    "created_at": 0,
                    "migration_status": "retired_requires_reproposal",
                }
                conn.execute(
                    "INSERT OR IGNORE INTO operation_proposals VALUES (?, ?, 'retired_requires_reproposal', NULL, 0, ?)",
                    (
                        proposal_id,
                        "legacy_knowledge_commit",
                        self._seal(retired, purpose="proposal", identifier=proposal_id),
                    ),
                )
            if isinstance(provenance, dict):
                for provenance_ref, record in provenance.items():
                    if not str(provenance_ref).startswith("prov_") or not isinstance(
                        record, dict
                    ):
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO provenance_records VALUES (?, ?, 0, ?)",
                        (
                            str(provenance_ref),
                            str(record.get("entry_id") or ""),
                            self._seal(
                                record,
                                purpose="provenance",
                                identifier=str(provenance_ref),
                            ),
                        ),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO source_library_meta(key, value) VALUES('legacy_migrated_revision', ?)",
                (revision,),
            )

    def _upsert_binding(self, conn: sqlite3.Connection, binding: SourceBinding) -> None:
        conn.execute(
            """INSERT INTO source_bindings(
                   source_id, lifecycle_state, created_at, sealed_record
               ) VALUES (?, 'active', ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                   lifecycle_state='active',
                   created_at=excluded.created_at,
                   sealed_record=excluded.sealed_record""",
            (
                binding.source_id,
                binding.created_at,
                self._seal(
                    binding.to_json(), purpose="binding", identifier=binding.source_id
                ),
            ),
        )

    def put_binding(self, binding: SourceBinding) -> None:
        with self._lock, self._connect() as conn, write_txn(conn):
            self._upsert_binding(conn, binding)

    def get_binding(self, source_id: str) -> SourceBinding:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT sealed_record FROM source_bindings WHERE source_id=? AND lifecycle_state='active'",
                (source_id,),
            ).fetchone()
        if row is None:
            raise SourceLibraryIndexError(
                "The requested source binding does not exist."
            )
        return SourceBinding.from_json(
            self._open(row["sealed_record"], purpose="binding", identifier=source_id)
        )

    def list_bindings(self) -> list[SourceBinding]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, sealed_record FROM source_bindings WHERE lifecycle_state='active' ORDER BY source_id"
            ).fetchall()
        return [
            SourceBinding.from_json(
                self._open(
                    row["sealed_record"], purpose="binding", identifier=row["source_id"]
                )
            )
            for row in rows
        ]

    def _upsert_entry(
        self, conn: sqlite3.Connection, entry: CatalogEntry, *, lifecycle_state: str
    ) -> None:
        conn.execute(
            """INSERT INTO observed_items(
                   entry_id, source_id, content_revision, entry_state,
                   lifecycle_state, modified_ns, locator_token, dedup_token, sealed_record
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entry_id) DO UPDATE SET
                   source_id=excluded.source_id,
                   content_revision=excluded.content_revision,
                   entry_state=excluded.entry_state,
                   lifecycle_state=excluded.lifecycle_state,
                   modified_ns=excluded.modified_ns,
                   locator_token=excluded.locator_token,
                   dedup_token=excluded.dedup_token,
                   sealed_record=excluded.sealed_record""",
            (
                entry.entry_id,
                entry.source_id,
                entry.content_revision,
                entry.state,
                lifecycle_state,
                entry.modified_ns,
                self._token("locator", entry.relative_path),
                self._token("dedup", entry.content_hash)
                if entry.content_hash
                else None,
                self._seal(entry.to_json(), purpose="item", identifier=entry.entry_id),
            ),
        )

    def _token(self, purpose: str, value: str | None) -> str:
        return hmac.new(
            self.crypto.key(f"index-{purpose}"),
            str(value or "").encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def get_entry(
        self, entry_id: str, *, include_trashed: bool = False
    ) -> CatalogEntry:
        query = "SELECT sealed_record FROM observed_items WHERE entry_id=?"
        params: list[Any] = [entry_id]
        if not include_trashed:
            query += " AND lifecycle_state='active'"
        with self._lock, self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            raise SourceLibraryIndexError("The requested source entry does not exist.")
        return CatalogEntry.from_json(
            self._open(row["sealed_record"], purpose="item", identifier=entry_id)
        )

    def list_entries(self, source_id: str | None = None) -> list[CatalogEntry]:
        query = "SELECT entry_id, sealed_record FROM observed_items WHERE lifecycle_state='active'"
        params: tuple[Any, ...] = ()
        if source_id is not None:
            query += " AND source_id=?"
            params = (source_id,)
        query += " ORDER BY source_id, modified_ns DESC, entry_id"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            CatalogEntry.from_json(
                self._open(
                    row["sealed_record"], purpose="item", identifier=row["entry_id"]
                )
            )
            for row in rows
        ]

    def reconcile_entries(
        self,
        source_id: str,
        entries: Iterable[CatalogEntry],
        *,
        complete: bool,
        stop_reason: str | None = None,
        expected_cursor: int | None = None,
    ) -> list[dict[str, Any]]:
        observed_list = list(entries)
        now = int(time.time() * 1000)
        changes: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn, write_txn(conn):
            cursor_row = conn.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source_id=?", (source_id,)
            ).fetchone()
            current_cursor = int(cursor_row["cursor"] if cursor_row else 0)
            if expected_cursor is not None and current_cursor != expected_cursor:
                raise SourceLibraryIndexError(
                    "A newer mirror reconciliation completed while this scan was running."
                )
            # Read the prior mirror only after BEGIN IMMEDIATE. Two Hermes
            # service instances can therefore reconcile the same profile
            # without computing changes from the same stale checkpoint.
            prior_rows = conn.execute(
                "SELECT entry_id, lifecycle_state, sealed_record FROM observed_items WHERE source_id=?",
                (source_id,),
            ).fetchall()
            prior = {
                str(row["entry_id"]): CatalogEntry.from_json(
                    self._open(
                        row["sealed_record"],
                        purpose="item",
                        identifier=str(row["entry_id"]),
                    )
                )
                for row in prior_rows
            }
            prior_lifecycle = {
                str(row["entry_id"]): str(row["lifecycle_state"]) for row in prior_rows
            }
            identity_candidates: dict[tuple[int, int], list[CatalogEntry]] = {}
            for old in prior.values():
                if old.device and old.inode:
                    identity_candidates.setdefault((old.device, old.inode), []).append(
                        old
                    )

            normalized: list[CatalogEntry] = []
            claimed_prior_ids: set[str] = set()
            for entry in observed_list:
                candidates = [
                    old
                    for old in identity_candidates.get((entry.device, entry.inode), [])
                    if old.entry_id not in claimed_prior_ids
                ]
                # Device+inode is a stable rename identity only when it is
                # unambiguous. Hard links deliberately retain separate refs.
                if len(candidates) == 1:
                    stable = candidates[0]
                    claimed_prior_ids.add(stable.entry_id)
                    if stable.entry_id != entry.entry_id:
                        entry = CatalogEntry.from_json({
                            **entry.to_json(),
                            "entry_id": stable.entry_id,
                        })
                normalized.append(entry)
            observed = {entry.entry_id: entry for entry in normalized}
            for entry_id, entry in observed.items():
                old = prior.get(entry_id)
                if old is None:
                    kind = "created"
                elif prior_lifecycle.get(entry_id) != "active":
                    kind = "restored"
                elif old.relative_path != entry.relative_path:
                    kind = "renamed"
                elif old.content_revision != entry.content_revision:
                    kind = "modified"
                else:
                    kind = "unchanged"
                if old is not None and old.content_revision == entry.content_revision:
                    if old.content_hash and not entry.content_hash:
                        entry = CatalogEntry.from_json({
                            **entry.to_json(),
                            "content_hash": old.content_hash,
                            "artifact_id": old.artifact_id,
                        })
                self._upsert_entry(conn, entry, lifecycle_state="active")
                if kind != "unchanged":
                    changes.append({"entry_id": entry_id, "change_kind": kind})
            if complete:
                for entry_id in prior.keys() - observed.keys():
                    conn.execute(
                        "UPDATE observed_items SET lifecycle_state='missing' WHERE entry_id=?",
                        (entry_id,),
                    )
                    changes.append({"entry_id": entry_id, "change_kind": "missing"})
            cursor = current_cursor + 1
            for change in changes:
                event_id = f"change_{uuid.uuid4().hex}"
                record = {**change, "source_id": source_id, "cursor": cursor}
                conn.execute(
                    "INSERT INTO change_events VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        source_id,
                        change["entry_id"],
                        change["change_kind"],
                        now,
                        self._seal(record, purpose="change", identifier=event_id),
                    ),
                )
            checkpoint = {
                "source_id": source_id,
                "cursor": cursor,
                "change_count": len(changes),
                "complete": complete,
                "stop_reason": stop_reason,
            }
            conn.execute(
                """INSERT INTO sync_checkpoints(source_id, cursor, status, last_scan_at, sealed_record)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,
                       status=excluded.status, last_scan_at=excluded.last_scan_at,
                       sealed_record=excluded.sealed_record""",
                (
                    source_id,
                    cursor,
                    "current" if complete else "partial",
                    now,
                    self._seal(checkpoint, purpose="checkpoint", identifier=source_id),
                ),
            )
        return changes

    def checkpoint_cursor(self, source_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source_id=?", (source_id,)
            ).fetchone()
        return int(row["cursor"] if row else 0)

    def update_entry(self, entry: CatalogEntry, *, expected_revision: str) -> None:
        with self._lock, self._connect() as conn, write_txn(conn):
            current = conn.execute(
                "SELECT content_revision FROM observed_items WHERE entry_id=? AND lifecycle_state='active'",
                (entry.entry_id,),
            ).fetchone()
            if current is None or current["content_revision"] != expected_revision:
                raise SourceLibraryIndexError(
                    "The catalog changed while the item was being processed."
                )
            self._upsert_entry(conn, entry, lifecycle_state="active")

    def sync_status(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, cursor, status, last_scan_at FROM sync_checkpoints ORDER BY source_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def put_proposal(self, proposal: dict[str, Any]) -> None:
        proposal_id = str(proposal["proposal_id"])
        with self._lock, self._connect() as conn, write_txn(conn):
            try:
                conn.execute(
                    "INSERT INTO operation_proposals VALUES (?, ?, 'pending', ?, ?, ?)",
                    (
                        proposal_id,
                        str(proposal["operation_kind"]),
                        proposal.get("base_revision"),
                        int(proposal["created_at"]),
                        self._seal(
                            proposal, purpose="proposal", identifier=proposal_id
                        ),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SourceLibraryIndexError(
                    "The Source Library proposal id already exists."
                ) from exc

    def claim_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Atomically make one approved proposal execution-exclusive."""
        with self._lock, self._connect() as conn, write_txn(conn):
            row = conn.execute(
                "SELECT sealed_record FROM operation_proposals WHERE proposal_id=? AND status='pending'",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise SourceLibraryIndexError(
                    "The Source Library proposal is unavailable."
                )
            updated = conn.execute(
                "UPDATE operation_proposals SET status='executing' WHERE proposal_id=? AND status='pending'",
                (proposal_id,),
            )
            if updated.rowcount != 1:
                raise SourceLibraryIndexError(
                    "The Source Library proposal was already claimed."
                )
            return self._open(
                row["sealed_record"], purpose="proposal", identifier=proposal_id
            )

    def complete_claimed_proposal(
        self, proposal_id: str, *, status: str, receipt: dict[str, Any]
    ) -> None:
        self._complete_proposal_from_status(
            proposal_id,
            expected_status="executing",
            status=status,
            receipt=receipt,
        )

    def _complete_proposal_from_status(
        self,
        proposal_id: str,
        *,
        expected_status: str,
        status: str,
        receipt: dict[str, Any],
    ) -> None:
        if status in {"pending", "executing"}:
            raise SourceLibraryIndexError(
                "A proposal completion status must be terminal."
            )
        receipt_id = str(receipt["receipt_id"])
        with self._lock, self._connect() as conn, write_txn(conn):
            updated = conn.execute(
                "UPDATE operation_proposals SET status=? WHERE proposal_id=? AND status=?",
                (
                    status,
                    proposal_id,
                    expected_status,
                ),
            )
            if updated.rowcount != 1:
                raise SourceLibraryIndexError("The proposal changed before completion.")
            conn.execute(
                "INSERT INTO operation_receipts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    proposal_id,
                    str(receipt["operation_kind"]),
                    str(receipt["status"]),
                    int(receipt["created_at"]),
                    self._seal(receipt, purpose="receipt", identifier=receipt_id),
                ),
            )

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, sealed_record FROM operation_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None or row["status"] != "pending":
            raise SourceLibraryIndexError("The Source Library proposal is unavailable.")
        return self._open(
            row["sealed_record"], purpose="proposal", identifier=proposal_id
        )

    def complete_proposal(
        self, proposal_id: str, *, status: str, receipt: dict[str, Any]
    ) -> None:
        self._complete_proposal_from_status(
            proposal_id,
            expected_status="pending",
            status=status,
            receipt=receipt,
        )

    def put_share_target(self, target: dict[str, Any]) -> None:
        target_id = str(target["target_id"])
        with self._lock, self._connect() as conn, write_txn(conn):
            conn.execute(
                """INSERT INTO share_targets VALUES (?, ?, 'active', ?, ?)
                   ON CONFLICT(target_id) DO UPDATE SET lifecycle_state='active', sealed_record=excluded.sealed_record""",
                (
                    target_id,
                    str(target["source_id"]),
                    int(target["created_at"]),
                    self._seal(target, purpose="share_target", identifier=target_id),
                ),
            )

    def list_share_targets(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT target_id, sealed_record FROM share_targets WHERE lifecycle_state='active' ORDER BY created_at"
            ).fetchall()
        return [
            self._open(
                row["sealed_record"],
                purpose="share_target",
                identifier=row["target_id"],
            )
            for row in rows
        ]

    def get_share_target(self, target_id: str) -> dict[str, Any]:
        for target in self.list_share_targets():
            if target.get("target_id") == target_id:
                return target
        raise SourceLibraryIndexError("The share target does not exist.")

    def put_share(self, share: dict[str, Any]) -> None:
        share_ref = str(share["share_ref"])
        with self._lock, self._connect() as conn, write_txn(conn):
            conn.execute(
                """INSERT INTO share_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(share_ref) DO UPDATE SET status=excluded.status,
                     updated_at=excluded.updated_at, sealed_record=excluded.sealed_record""",
                (
                    share_ref,
                    str(share["entry_id"]),
                    str(share["target_id"]),
                    str(share["mode"]),
                    str(share["status"]),
                    str(share["pinned_revision"]),
                    int(share["created_at"]),
                    int(share["updated_at"]),
                    self._seal(share, purpose="share", identifier=share_ref),
                ),
            )

    def list_shares(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT share_ref, sealed_record FROM share_records"
        if active_only:
            query += (
                " WHERE status IN ('active', 'provider_sync_pending', 'human_detected')"
            )
        query += " ORDER BY updated_at DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [
            self._open(
                row["sealed_record"], purpose="share", identifier=row["share_ref"]
            )
            for row in rows
        ]

    def get_share(self, share_ref: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT sealed_record FROM share_records WHERE share_ref=?",
                (share_ref,),
            ).fetchone()
        if row is None:
            raise SourceLibraryIndexError("The share record does not exist.")
        return self._open(row["sealed_record"], purpose="share", identifier=share_ref)

    def put_provenance(self, provenance_ref: str, record: dict[str, Any]) -> None:
        if not provenance_ref.startswith("prov_"):
            raise SourceLibraryIndexError("The provenance reference is invalid.")
        with self._lock, self._connect() as conn, write_txn(conn):
            conn.execute(
                "INSERT OR REPLACE INTO provenance_records VALUES (?, ?, ?, ?)",
                (
                    provenance_ref,
                    str(record["entry_id"]),
                    int(time.time() * 1000),
                    self._seal(record, purpose="provenance", identifier=provenance_ref),
                ),
            )
