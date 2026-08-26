# Upgrading from Official Hermes

Hussh One is a tested overlay on the official Hermes source. `origin/main` is
the only Hussh One product trunk; `upstream/main` is comparison input only.
Never merge upstream directly into a running `main` checkout.

## Standard daily updater

Fresh Hussh One setup registers the guarded daily updater automatically. It
fetches both remotes, creates and pushes a safety tag, reconciles official
Hermes on a short-lived `sync/upstream-*` branch, runs the Hussh guard, merges
only a clean result into `origin/main`, then restarts local services.

```bash
# Inspect without changing anything.
scripts/hussh-one-upstream-update.sh --check

# Run the same guarded reconciliation now.
scripts/hussh-one-upstream-update.sh --apply --restart

# Manage the per-machine schedule (launchd, systemd user timer, or cron).
scripts/hussh-one-upstream-update.sh --status
scripts/hussh-one-upstream-update.sh --install-daily
```

If upstream conflicts or the guard fails, the script leaves `main` unchanged.
For a conflict, start the manual upgrade loop below; for a guard failure, the
script retains the sync branch for maintainer review. Setup can opt out on an
exceptional machine with `scripts/hussh-one-bootstrap.sh --no-daily-updater`.

## Manual upgrade loop

Use this when a conflict needs deliberate overlay reconciliation:

```bash
git switch main
git pull --ff-only origin main
git fetch origin upstream --tags --prune
TS=$(date +%Y%m%d-%H%M%S)
git tag "safety/main-$TS" main && git push origin "safety/main-$TS"
git switch -c "sync/upstream-$TS" main
git merge --no-ff upstream/main
# Resolve generic behavior upstream-first; reapply documented Hussh overlays.
scripts/hussh-one-guard.sh
git switch main
git merge --no-ff "sync/upstream-$TS"
git push origin main
git branch -d "sync/upstream-$TS"
git pull --ff-only origin main
```

## Why it is safe

- Hussh One logic lives in dedicated overlay modules (`hermes_cli/brand.py`,
  `hermes_cli/hussh_one_header.py`, `gateway/whatsapp_capsule.py`,
  `scripts/hussh-one-*`), not inlined in core files.
- Generic core changes remain upstream-first. A Hussh exception must be a
  small, documented, tested overlay rather than a divergent second core.
- The guard verifies branding, connector, license, model/provider, WhatsApp,
  embedded-TUI, and shell-entry-point contracts before the runtime is changed.

## Conflict-resolution rules

1. Prefer official Hermes behavior for generic core code.
2. Reapply only the documented Hussh overlay call sites (search for
   `hussh_one`, `capsule`, `apply_whatsapp_header`).
3. Keep the license boundary intact: inherited source is MIT, Hussh-added
   source is Apache-2.0 with SPDX headers, and modified inherited source is
   `MIT AND Apache-2.0`. Update `LICENSES/attribution.toml` only when the
   upstream comparison base changes deliberately.
4. End every completed update on clean, pushed `main`; delete the temporary
   sync branch before restarting the Hussh One runtime.

## After upgrading

- Restart the gateway from clean `main` (see the operations runbook).
- Confirm the dashboard’s embedded real TUI works and send a test `@One` ping.
- Before a release, inspect the built wheel/sdist for `LICENSE`, `NOTICE`,
  `LICENSES/*`, and `THIRD_PARTY_NOTICES.md`.
