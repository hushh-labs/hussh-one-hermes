# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Owner-only local replica of cloud PKM ciphertext and its sync cursor."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


class EncryptedPkmReplica:
    """Persist only ciphertext snapshots; plaintext never crosses this port."""

    def __init__(self, profile_home: Path) -> None:
        self.root = profile_home / "hussh-one" / "pkm-replica"
        self.domains = self.root / "domains"
        self.state_path = self.root / "sync-state.json"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def cursor(self) -> int:
        if not self.state_path.exists():
            return 0
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return max(0, int(payload.get("cursor") or 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0

    def advance(self, cursor: int) -> None:
        self._atomic_json(
            self.state_path,
            {"schema_version": 1, "cursor": max(self.cursor(), int(cursor))},
        )

    def store_snapshot(self, domain: str, snapshot: dict[str, Any]) -> None:
        encrypted_blob = snapshot.get("encrypted_blob")
        if not isinstance(encrypted_blob, dict):
            raise ValueError("PKM replica snapshots require ciphertext.")
        safe_snapshot = {
            "schema_version": "hussh_one_encrypted_replica.v1",
            "domain": domain,
            "content_revision": int(snapshot.get("content_revision") or 0),
            "manifest_revision": int(snapshot.get("manifest_revision") or 0),
            "updated_at": snapshot.get("updated_at"),
            "etag": str(snapshot.get("etag") or ""),
            "encrypted_blob": encrypted_blob,
        }
        self._atomic_json(self.domains / f"{domain}.json", safe_snapshot)

    def delete_domain(self, domain: str) -> None:
        path = self.domains / f"{domain}.json"
        path.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
