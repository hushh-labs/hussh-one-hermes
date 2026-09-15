# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Trusted-main proposal builder; never execute code from the imported tree."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

REPO = "hushh-labs/hussh-one-hermes"
BRANCH = "sync/upstream-central"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> None:
    run("git", "config", "core.hooksPath", "/dev/null")
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("gh", "auth", "setup-git")
    run("git", "fetch", "origin", "main")
    run("git", "fetch", "https://github.com/NousResearch/hermes-agent.git", "main")
    upstream = run("git", "rev-parse", "FETCH_HEAD")
    if subprocess.run(["git", "merge-base", "--is-ancestor", upstream, "origin/main"]).returncode == 0:
        return
    existing = run("git", "ls-remote", "--heads", "origin", BRANCH)
    if existing:
        run("git", "fetch", "origin", BRANCH)
        run("git", "switch", "-c", BRANCH, "FETCH_HEAD")
        run("git", "merge", "--no-edit", "origin/main")
    else:
        run("git", "switch", "-c", BRANCH, "origin/main")
    run("git", "merge", "--no-ff", "--no-edit", upstream)
    path = Path("LICENSES/attribution.toml")
    text, count = re.subn(r'(?m)^upstream_base_commit = "[0-9a-f]{40}"$',
                         f'upstream_base_commit = "{upstream}"', path.read_text(encoding="utf-8"), count=1)
    if count != 1:
        raise RuntimeError("Cannot locate attribution base")
    path.write_text(text, encoding="utf-8")
    run("git", "add", "LICENSES/attribution.toml")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode:
        run("git", "commit", "-m", "chore(license): record proposed Hermes upstream base")
    run("git", "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    prs = json.loads(run("gh", "pr", "list", "--repo", REPO, "--head", BRANCH, "--state", "open", "--json", "number"))
    if not prs:
        run("gh", "pr", "create", "--repo", REPO, "--base", "main", "--head", BRANCH,
            "--title", "chore: reconcile official Hermes upstream",
            "--body", "Proposes official Hermes changes with the existing Hussh overlay. Imported code has not run in the write-token job. Require CI, Hussh guard, and exact-SHA Muse Glimmer acceptance through the merge queue before installation updates consume it.")


if __name__ == "__main__":
    main()
