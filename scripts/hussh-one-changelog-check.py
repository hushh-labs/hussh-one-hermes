#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Hussh One — Changelog freshness check.

Finds commits that touch a Hussh-One-only surface but whose short SHA is NOT
mentioned anywhere in docs/hussh-one/CHANGELOG.md. Zero output + exit 0 means
the changelog is current. Any output means: add a row (see the "How to add an
entry" section at the bottom of CHANGELOG.md) before shipping further work.

Usage:
  python3 scripts/hussh-one-changelog-check.py            # human report
  python3 scripts/hussh-one-changelog-check.py --json     # machine JSON
  python3 scripts/hussh-one-changelog-check.py --since <ref>   # override range

Exit code: 0 if nothing undocumented, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "docs" / "hussh-one" / "CHANGELOG.md"

# Paths that make a commit "Hussh-One-only" — i.e. it MUST be reflected in the
# changelog. Kept in sync with the "Keeping this file current" section of
# CHANGELOG.md; update both together.
HUSSH_ONE_PATHS = [
    "hermes_cli/hussh_one_header.py",
    "hermes_cli/hussh_one_identity.py",
    "hermes_cli/hussh_one_router.py",
    "hermes_cli/hussh_one_mcp_scan.py",
    "hermes_cli/brand.py",
    "hermes_cli/skins/hussh-one.yaml",
    "hermes_cli/dashboard_themes/hussh-one.yaml",
    "gateway/whatsapp_capsule.py",
    "scripts/whatsapp-bridge/",
    ":(glob)scripts/hussh-one-*",
    "scripts/setup_open_webui.sh",
    "scripts/copilot-byok/",
    "scripts/open-webui/",
    "plugins/model-providers/google-vertex-claude/",
    "docs/hussh-one/",
    "HUSSH_ONE.md",
]

CHANGELOG_ONLY_PATHS = {"docs/hussh-one/CHANGELOG.md"}
# A fresh clone of the Hussh distribution does not necessarily define the
# optional ``upstream`` remote. Keep the audit bounded to the fork's recorded
# ancestor in that case instead of treating all inherited Hermes history as
# undocumented Hussh work. Keep this aligned with LICENSES/attribution.toml.
RECORDED_FORK_BASE = "9de9c25f620ff7f1ce0fd5457d596052d5159596"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()


def _merge_base_with_upstream() -> str | None:
    remotes = _git("remote").split()
    if "upstream" not in remotes:
        return None
    return _git("merge-base", "HEAD", "upstream/main") or None


def _recorded_fork_base() -> str | None:
    """Return the committed fork ancestor when ``upstream`` is unavailable."""
    return _git("rev-parse", "--verify", f"{RECORDED_FORK_BASE}^{{commit}}") or None


def _changed_paths(sha: str) -> set[str]:
    """Return the tracked paths changed by one commit."""
    return {
        path
        for path in _git("show", "--format=", "--name-only", sha).splitlines()
        if path
    }


def is_changelog_only_commit(sha: str) -> bool:
    """Whether a commit updates only this changelog's own documentation."""
    changed = _changed_paths(sha)
    return bool(changed) and changed <= CHANGELOG_ONLY_PATHS


def find_candidate_commits(since_ref: str | None) -> list[tuple[str, str, str]]:
    """Return (short_sha, date, subject) for commits touching Hussh-One paths."""
    base = since_ref or _merge_base_with_upstream() or _recorded_fork_base()
    range_spec = f"{base}..HEAD" if base else "HEAD"
    fmt = "%h|%ad|%s"
    out = _git(
        "log", f"--format={fmt}", "--date=short", range_spec,
        "--", *HUSSH_ONE_PATHS,
    )
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and not is_changelog_only_commit(parts[0]):
            commits.append(tuple(parts))
    return commits


def changelog_text() -> str:
    if not CHANGELOG.exists():
        return ""
    return CHANGELOG.read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="Override the git ref to diff from (default: merge-base with upstream/main)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not CHANGELOG.exists():
        print(f"FAIL: changelog missing at {CHANGELOG}", file=sys.stderr)
        return 1

    text = changelog_text()
    candidates = find_candidate_commits(args.since)
    undocumented = [(sha, date, subj) for sha, date, subj in candidates if sha not in text]

    if args.json:
        print(json.dumps({
            "checked": len(candidates),
            "undocumented": [
                {"sha": s, "date": d, "subject": subj} for s, d, subj in undocumented
            ],
            "clean": not undocumented,
        }, indent=2))
    else:
        if not undocumented:
            print(f"✅ Changelog current — {len(candidates)} Hussh-One commit(s) checked, all documented.")
        else:
            print(f"⚠️  {len(undocumented)} undocumented Hussh-One commit(s) — add rows to {CHANGELOG}:\n")
            for sha, date, subj in undocumented:
                print(f"  {date}  {sha}  {subj}")
            print(f"\nSee \"How to add an entry\" at the bottom of {CHANGELOG}.")

    return 1 if undocumented else 0


if __name__ == "__main__":
    raise SystemExit(main())
