# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared, conservative inventory for Baileys WhatsApp session files.

The pruner and Hussh health index must agree on what is safe to remove.  This
module only classifies the flat session directory; it never mutates it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time


PROTECTED_EXACT = frozenset({"creds.json", "bridge.pid"})
PROTECTED_PREFIXES = ("identity-key-",)
PRUNABLE_PREFIXES = (
    "app-state-sync-key-",
    "app-state-sync-version-",
    "pre-key-",
    "sender-key-memory-",
    "sender-key-",
    "lid-mapping-",
    "device-list-",
    "session-",
)


@dataclass(frozen=True)
class SessionInventory:
    """One non-mutating scan of a Baileys session directory."""

    total_files: int
    protected_files: int
    unmanaged_files: int
    prunable_files: tuple[Path, ...]

    @property
    def prunable_count(self) -> int:
        return len(self.prunable_files)


def is_whatsapp_session_dir(path: Path) -> bool:
    """Reject every path except the explicitly expected session directory."""
    return path.as_posix().rstrip("/").endswith("whatsapp/session")


def family_for(name: str) -> str | None:
    """Return a regenerable family name, never a protected identity file."""
    if name in PROTECTED_EXACT or name.startswith(PROTECTED_PREFIXES):
        return None
    return next((prefix for prefix in PRUNABLE_PREFIXES if name.startswith(prefix)), None)


def is_protected(name: str) -> bool:
    return name in PROTECTED_EXACT or name.startswith(PROTECTED_PREFIXES)


def scan_session_directory(
    session_dir: Path,
    *,
    now: float | None = None,
    retention_days: int = 7,
    keep_per_family: int = 8,
    max_per_family: int = 400,
) -> SessionInventory:
    """Find files eligible for the janitor without changing the filesystem.

    The newest ``keep_per_family`` files are always retained.  The count cap
    additionally bounds the high-churn key families, while app-state and
    device-list files remain age-only because they are identity-adjacent.
    """
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    buckets: dict[str, list[tuple[Path, float]]] = {}
    total = protected = unmanaged = 0

    for path in session_dir.iterdir():
        total += 1
        name = path.name
        family = family_for(name)
        if family is None:
            if is_protected(name):
                protected += 1
            else:
                unmanaged += 1
            continue
        try:
            buckets.setdefault(family, []).append((path, path.stat().st_mtime))
        except OSError:
            # A concurrently replaced session file is left alone until the next scan.
            continue

    candidates: list[Path] = []
    for family, files in buckets.items():
        files.sort(key=lambda item: item[1], reverse=True)
        selected: set[Path] = {
            path for path, mtime in files[keep_per_family:] if mtime < cutoff
        }
        count_cap_exempt = family.startswith(("app-state-sync-", "device-list-"))
        if max_per_family > 0 and not count_cap_exempt:
            selected.update(path for path, _mtime in files[max_per_family:])
        candidates.extend(sorted(selected, key=lambda path: path.name))

    return SessionInventory(
        total_files=total,
        protected_files=protected,
        unmanaged_files=unmanaged,
        prunable_files=tuple(candidates),
    )
