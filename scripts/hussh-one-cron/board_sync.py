#!/usr/bin/env python3
"""Wrapper for the scheduled daily Board Sync job.

Runs the STABLE, cron-owned copy of board_sync_cycle.py from
~/.hermes/scripts/board_lib/ — NOT the repo working tree.

Why: the hushh-research checkout is branch-switched by concurrent agents/PR
trains, so reading the script straight from .codex/skills/... made the cron
non-deterministic (it could run an old version depending on the active branch).
The board_lib/ copy is the authoritative version the cron always runs.

To update the logic: edit ~/.hermes/scripts/board_lib/*.py (and mirror the
change back into the repo's .codex/skills/planning-board/scripts/ when you want
it version-controlled). board_ops.py is imported by board_sync_cycle.py from the
same directory, so both must live in board_lib/.
"""

import os
import subprocess
import sys

BOARD_LIB = os.path.expanduser("~/.hermes/scripts/board_lib")
SCRIPT_PATH = os.path.join(BOARD_LIB, "board_sync_cycle.py")


def main():
    if not os.path.exists(SCRIPT_PATH):
        print(f"Error: stable board script missing at {SCRIPT_PATH}", file=sys.stderr)
        sys.exit(1)
    try:
        # Run in-process to avoid double-nested subprocess hang on MacOS
        sys.path.insert(0, BOARD_LIB)
        import board_sync_cycle
        dry = "--dry-run" in sys.argv
        report, changed = board_sync_cycle.sync_board_cycle(dry_run=dry)
        if "--watchdog" in sys.argv and not changed:
            sys.exit(0)
        print(report)
    except Exception as exc:
        print(f"Error: In-process Board sync script failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
