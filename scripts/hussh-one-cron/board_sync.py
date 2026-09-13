#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Run Board Sync and emit a bounded owner-facing WhatsApp report.

Runs the STABLE, cron-owned copy of board_sync_cycle.py from
~/.hermes/scripts/board_lib/ — NOT the repo working tree.

Why: the hushh-research checkout is branch-switched by concurrent agents/PR
trains, so reading the script straight from .codex/skills/... made the cron
non-deterministic (it could run an old version depending on the active branch).
The board_lib/ copy is the authoritative version the cron always runs.

To update the logic: edit the versioned ``scripts/hussh-one-cron/board_lib``
source, test it, then install it through ``hussh-one-cron-sync.py``.
``board_ops.py`` is imported by ``board_sync_cycle.py`` from the same directory.
"""

import os
import sys
import re

BOARD_LIB = os.path.expanduser("~/.hermes/scripts/board_lib")
SCRIPT_PATH = os.path.join(BOARD_LIB, "board_sync_cycle.py")

HEADER = "*🤫 Hussh One · Board Sync*\n======================================"
MAX_REPORT_CHARS = 1800


def _fit_line(text: str, available: int) -> str | None:
    """Fit one bullet without cutting a report in the middle of a word."""
    if available < 4:
        return None
    if len(text) <= available:
        return text
    clipped = text[: available - 1].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return f"{clipped}…" if clipped else None


def format_report(report: str, *, dry_run: bool = False) -> str:
    """Reduce the verbose board trace to a stable, readable owner report."""
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    selected: list[str] = []
    for line in lines:
        plain = re.sub(r"[*#`]", "", line).strip()
        if not plain or plain.startswith("> Scope guardrail"):
            continue
        if any(token in plain for token in (
            "Completion & Delivery", "Active Work & Backlog", "Board Hygiene",
            "Hierarchy & Taxonomy", "Date Alignment", "Board Statistics",
            "No owner-scoped", "Total tasks in", "Start date", "Target date",
            "items scanned", "set on", "would set", "failed", "Warning",
        )):
            selected.append(plain)
        elif selected and len(selected) < 18 and (plain.startswith(("•", "-", "⚠", "✓", "❌")) or "#" in plain):
            selected.append(plain)
    if not selected:
        selected = [
            "Board sync produced no report; status is unverified."
            if not dry_run
            else "Board sync dry-run produced no report; status is unverified."
        ]

    suffix = "\n\n• Mode: dry-run; no board changes were made." if dry_run else ""
    # Keep the entire message below the mobile contract. The previous final
    # slice could leave a half ticket (for example, ``hushh-``) at the end.
    limit = MAX_REPORT_CHARS - 1
    prefix = f"{HEADER}\n\n"
    body_budget = limit - len(prefix) - len(suffix)
    bullets: list[str] = []
    for line in selected[:18]:
        content = line.lstrip("•- ").strip()
        separator = 2 if bullets else 0
        fitted = _fit_line(content, body_budget - separator - 2)
        if fitted is None:
            break
        bullets.append(f"• {fitted}")
        body_budget -= separator + len(bullets[-1])
    if not bullets:
        bullets = ["• Report was too large to present safely; review the local run output."]
    body = "\n\n".join(bullets)
    return f"{prefix}{body}{suffix}"


def format_failure(exc: Exception) -> str:
    """Render a bounded failure without dumping a traceback into chat."""
    detail = " ".join(str(exc).split())
    detail = _fit_line(detail, 320) or "unknown error"
    return (
        f"{HEADER}\n\n"
        "• The scheduled board sync failed before completion.\n\n"
        f"• Error: {type(exc).__name__}: {detail}"
    )


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
        print(format_report(report, dry_run=dry))
    except Exception as exc:
        print(format_failure(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
