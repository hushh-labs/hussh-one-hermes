# Local Worktree & Dependency Hygiene Reference

Canonical organization standard: [Local Worktree & Dependency Hygiene Standard](https://wiki.hussh.si/wiki/reference/local-worktree-and-dependency-hygiene)

## Why This Exists

Multi-agent coding sessions across hussh repositories previously saturated developer workstations to 100% disk capacity because temporary sibling git worktrees accumulated without teardown and each worktree materialized full ~1+ GB duplicate `node_modules` trees and build outputs.

## Mandatory Rules

1. **Pre-Flight Disk & Worktree Check:**
   Run `python3 scripts/worktree-hygiene.py --check` before creating a git worktree, installing dependencies, or running heavy web/desktop/bridge builds.
   - **Below 30 GB free (Warning):** Surface free disk space, active worktree count, and sizes before proceeding.
   - **Below 15 GB free (Hard Stop):** Do not create new worktrees or duplicate `node_modules` until space is reclaimed.
2. **Containment & Mandatory Remove-After-Use:**
   - Place ephemeral task worktrees in a dedicated user-local task pool (for example `~/.local/share/hushh-task-worktrees/<task>`), not ad-hoc sibling directories next to the primary repo clone.
   - Reuse an existing matching task worktree before spawning a new one. Parallel worktrees are allowed while actively in use (no artificial numeric cap), **but every temporary worktree MUST be permanently removed immediately after its task lands, merges, or finishes**.
3. **Zero Silent `node_modules` or Venv Duplication:**
   - Reuse the shared Python venv (`$HOME/.hermes/hermes-agent/venv`) and avoid blind fresh `npm install` copies inside temporary worktrees when lockfiles match the primary checkout.
   - Strip temporary `node_modules`, `dist`, and build caches before parking any checkout.
4. **Verified End-of-Task Teardown (Zero Orphaned Stubs):**
   A task is incomplete until every session-created worktree is either:
   - **Permanently removed:** confirm all unique commits are pushed or merged to `origin/main`, confirm zero active processes hold open handles in the checkout, unlink checkout-local symlinks without recursing, remove gitignored build/dependency caches (`git clean -fdX` after verifying clean tracked status), run `git worktree remove <path>` and `git worktree prune`, and verify the directory no longer exists on disk.
   - **Explicitly handed off** with its size, branch, and dirty/unpushed delta count.
5. **Preserve Unique Work Before Deletion:**
   Never delete a dirty worktree or unpushed commits without pushing a safety branch, exporting verified git bundles and diff patches, or getting explicit owner approval to discard.

## CLI Usage

```bash
python3 scripts/worktree-hygiene.py --audit
python3 scripts/worktree-hygiene.py --check
python3 scripts/worktree-hygiene.py --prune-safe
python3 scripts/worktree-hygiene.py --prune-safe --execute
```
