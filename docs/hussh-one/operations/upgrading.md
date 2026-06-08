# Upgrading from Upstream Hermes

Hussh One is an overlay. Upstream Hermes can be merged in without losing our personality —
provided you follow this loop.

## The upgrade loop
```bash
git fetch upstream
git switch hussh-one-hermes
git merge upstream/main        # resolve conflicts favoring overlay modules
# run the guard — this is the contract that proves the overlay survived
scripts/hussh-one-guard.sh
python -m pytest tests/hermes_cli/test_hussh_one_*.py tests/gateway/test_whatsapp_*.py -q
```

## Why it's safe
- Hussh One logic lives in dedicated overlay modules (`hermes_cli/brand.py`,
  `hermes_cli/hussh_one_header.py`, `gateway/whatsapp_capsule.py`, `scripts/hussh-one-*`),
  not inlined in core files. Core files only *import and call* these.
- If a merge rewrites `gateway/run.py`, our header/capsule logic isn't there to lose —
  it's in the overlay, and the call sites are small and easy to re-add.

## What the guard checks (Contract C/D + branding/header/capsule)
- Brand profile, skin, dashboard theme, WhatsApp prefix default present.
- Stacked header composition intact.
- Capsule isolation invariants hold.
- Dashboard exposes the embedded real TUI (not a forked chat).

## Conflict resolution rules
1. Prefer the **overlay** version of brand/header/capsule modules.
2. Re-apply the small call sites in core files (search for `hussh_one`, `capsule`,
   `apply_whatsapp_header`).
3. Never accept an upstream change that removes a documented config knob without updating
   the corresponding [contract](../contracts/README.md) and feature page.

## After upgrading
- Restart the gateway (see operations runbook).
- Confirm `✓ whatsapp connected` and send a test `@One` ping.
- Commit with a clear message; push to `origin/hussh-one-hermes`.
