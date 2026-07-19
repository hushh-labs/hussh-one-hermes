#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
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
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from whatsapp_session_inventory import is_whatsapp_session_dir, scan_session_directory

SESSION_DIR = os.path.expanduser(
    os.environ.get(
        "WHATSAPP_SESSION_DIR",
        "~/.hermes/whatsapp/session",
    )
)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
KEEP_PER_FAMILY = int(os.environ.get("KEEP_PER_FAMILY", "8"))
# Hard count cap per family, applied REGARDLESS of age. This is what fixes
# *recent* bloat (thousands of pre-key-*/lid-mapping-* files all < RETENTION_DAYS
# old that the age-gate alone never catches). Keep the newest MAX_PER_FAMILY of
# each prunable family; Baileys regenerates pre-keys / sender-keys / lid-maps on
# demand. 0 disables the cap (age-only behaviour).
MAX_PER_FAMILY = int(os.environ.get("MAX_PER_FAMILY", "400"))

def main() -> int:
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv or dry_run

    # Hard safety guard: refuse to operate anywhere but a whatsapp/session dir.
    session_dir = Path(SESSION_DIR)
    if not is_whatsapp_session_dir(session_dir):
        print(
            f"ERROR: refusing to prune unexpected path: {SESSION_DIR!r}",
            file=sys.stderr,
        )
        return 2
    if not session_dir.is_dir():
        # Nothing to do (bridge never paired here). Silent success.
        return 0

    try:
        inventory = scan_session_directory(
            session_dir,
            retention_days=RETENTION_DAYS,
            keep_per_family=KEEP_PER_FAMILY,
            max_per_family=MAX_PER_FAMILY,
        )
    except OSError as exc:
        print(f"ERROR: cannot list {SESSION_DIR}: {exc}", file=sys.stderr)
        return 2

    deleted = 0
    errors = 0
    for path in inventory.prunable_files:
        if dry_run:
            if verbose:
                print(f"[dry-run] would delete: {path.name}")
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            errors += 1
            print(f"WARN: failed to delete {path.name}: {exc}", file=sys.stderr)

    remaining = inventory.total_files - deleted
    if dry_run:
        print(
            f"[dry-run] {inventory.prunable_count} prunable (>{RETENTION_DAYS}d, "
            f"keep-newest-{KEEP_PER_FAMILY}/family); {inventory.total_files} total files."
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
