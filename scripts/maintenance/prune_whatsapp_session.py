#!/usr/bin/env python3
"""Prune stale Baileys WhatsApp session files (🤫 Hussh One).

Baileys writes one JSON file per contact device, group sender-key, pre-key,
app-state mutation, and LID mapping into ~/.hermes/whatsapp/session/ and never
garbage-collects them. On a busy account in many groups this dir bloats into
thousands of files; the bloat correlates with the 408 "AwaitingInitialSync"
reconnect loop (the bridge re-reads the whole dir on every reconnect). A real
incident had 12,185 files / 8,655 orphan LID maps before a manual prune.

This janitor age-outs prunable file FAMILIES while ALWAYS protecting the files
that carry the pairing identity and recent live state, so it never forces a
re-pair (which would need scanning the QR again).

SAFETY MODEL
------------
* NEVER touch: creds.json, the session-dir root sentinel, or anything whose
  name doesn't match a known prunable family.
* Prunable families (regenerated automatically by Baileys on demand):
    - app-state-sync-key-*, app-state-sync-version-*  (CRDT app-state cache)
    - pre-key-*                                        (one-time pre-keys)
    - sender-key-*                                     (group sender keys)
    - sender-key-memory-*
    - lid-mapping-*                                    (LID<->phone maps)
    - device-list-*                                    (per-jid device lists)
    - session-*                                        (per-device E2EE session)
* Within each prunable family: only delete files older than RETENTION_DAYS,
  AND always keep the newest KEEP_PER_FAMILY regardless of age, so the active
  conversation's live state is never removed.
* identity-key-* is treated as protected (cheap, identity-adjacent) by default.
* Hard guard: refuses to run unless the target path ends in
  ``whatsapp/session`` so a mis-set var can never nuke $HOME.

USAGE
-----
    python3 prune_whatsapp_session.py             # apply
    python3 prune_whatsapp_session.py --dry-run   # preview only
    RETENTION_DAYS=14 KEEP_PER_FAMILY=10 python3 prune_whatsapp_session.py

Exit code 0 on success (silent unless files were pruned or --verbose).
Non-zero only on a real error so a watchdog wrapper can alert.
"""
from __future__ import annotations

import os
import re
import sys
import time

SESSION_DIR = os.path.expanduser(
    os.environ.get(
        "WHATSAPP_SESSION_DIR",
        "~/.hermes/whatsapp/session",
    )
)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
KEEP_PER_FAMILY = int(os.environ.get("KEEP_PER_FAMILY", "8"))

# Files that must NEVER be deleted (identity / pairing critical).
_PROTECTED_EXACT = {"creds.json", "bridge.pid"}
_PROTECTED_PREFIXES = ("identity-key-",)

# Prunable family prefixes — Baileys regenerates these on demand.
_PRUNABLE_PREFIXES = (
    "app-state-sync-key-",
    "app-state-sync-version-",
    "pre-key-",
    "sender-key-memory-",
    "sender-key-",
    "lid-mapping-",
    "device-list-",
    "session-",
)


def _family(fn: str) -> str | None:
    """Return the prunable family prefix for a filename, or None if protected."""
    if fn in _PROTECTED_EXACT:
        return None
    for p in _PROTECTED_PREFIXES:
        if fn.startswith(p):
            return None
    for p in _PRUNABLE_PREFIXES:
        if fn.startswith(p):
            return p
    return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv or dry_run

    # Hard safety guard: refuse to operate anywhere but a whatsapp/session dir.
    norm = SESSION_DIR.rstrip("/")
    if not norm.endswith("whatsapp/session"):
        print(
            f"ERROR: refusing to prune unexpected path: {SESSION_DIR!r}",
            file=sys.stderr,
        )
        return 2
    if not os.path.isdir(SESSION_DIR):
        # Nothing to do (bridge never paired here). Silent success.
        return 0

    now = time.time()
    cutoff = now - RETENTION_DAYS * 86400

    # Bucket files by family with their mtimes.
    buckets: dict[str, list[tuple[str, float]]] = {}
    try:
        names = os.listdir(SESSION_DIR)
    except OSError as exc:
        print(f"ERROR: cannot list {SESSION_DIR}: {exc}", file=sys.stderr)
        return 2

    for fn in names:
        fam = _family(fn)
        if fam is None:
            continue
        path = os.path.join(SESSION_DIR, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        buckets.setdefault(fam, []).append((fn, mtime))

    to_delete: list[str] = []
    for fam, items in buckets.items():
        # Newest first; always keep the freshest KEEP_PER_FAMILY.
        items.sort(key=lambda t: t[1], reverse=True)
        for fn, mtime in items[KEEP_PER_FAMILY:]:
            if mtime < cutoff:
                to_delete.append(fn)

    deleted = 0
    errors = 0
    for fn in to_delete:
        path = os.path.join(SESSION_DIR, fn)
        if dry_run:
            if verbose:
                print(f"[dry-run] would delete: {fn}")
            continue
        try:
            os.remove(path)
            deleted += 1
        except OSError as exc:
            errors += 1
            print(f"WARN: failed to delete {fn}: {exc}", file=sys.stderr)

    remaining = len(names) - deleted
    if dry_run:
        print(
            f"[dry-run] {len(to_delete)} prunable (>{RETENTION_DAYS}d, "
            f"keep-newest-{KEEP_PER_FAMILY}/family); {len(names)} total files."
        )
    elif deleted or verbose:
        # Non-empty stdout => the cron watchdog will surface this. Keep it terse.
        print(
            f"🧹 WhatsApp session prune: removed {deleted} stale file(s), "
            f"{remaining} remain (retention={RETENTION_DAYS}d, "
            f"keep={KEEP_PER_FAMILY}/family)."
        )

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
