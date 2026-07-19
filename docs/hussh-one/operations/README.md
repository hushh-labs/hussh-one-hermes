# Operations Runbook

How to bootstrap, supervise, audit, and upgrade a Hussh One instance. All scripts live
under `scripts/hussh-one-*`.

## Scripts at a glance
| Script | Purpose |
|--------|---------|
| `hussh-one-bootstrap.sh` | Fresh-clone setup; detects + optionally starts a supervisor |
| `hussh-one-supervisor.sh` | Owns lifecycle (launchd/systemd/s6/screen); install/restart/status |
| `hussh-one-doctor.sh` | Health check; `--require-services` for strict mode |
| `hussh-one-guard.sh` | Post-merge invariant guard (branding, header, capsule, dashboard) |
| `hussh-one-restart.sh` | Convenience restart wrapper |
| `hussh-one-copilot-setup.sh` | VS Code Copilot BYOK: LiteLLM proxy (:8643) + auth shim (:8644) + `chatLanguageModels.json` with live-probed context windows (see `scripts/copilot-byok/README.md`) |
| `hussh-one-license-audit.py` | Verifies SPDX metadata, notices, attribution coverage, and release-file inclusion |

## Bootstrapping a new machine
```bash
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git switch main

# Hussh One defaults
.venv/bin/hermes config set display.skin hussh-one
.venv/bin/hermes config set dashboard.theme hussh-one
.venv/bin/hermes config set model.provider gemini
.venv/bin/hermes config set model.default gemini-3.5-flash
.venv/bin/hermes config set cron.wrap_response false

# Strict, noise-free, secure messaging
.venv/bin/hermes config set whatsapp.require_mention_on_replies true
.venv/bin/hermes config set display.tool_progress false
.venv/bin/hermes config set display.interim_assistant_messages false
.venv/bin/hermes config set display.show_reasoning false
.venv/bin/hermes config set approvals.mode off

# Bootstrap + supervisor
scripts/hussh-one-bootstrap.sh --manager auto --start
```

> The bootstrap's `set_config_defaults()` also seeds the OOM-safe compression
> tuning (`compression.threshold=0.35`, `compression.hygiene_hard_message_limit=250`).
> See [Crash resilience](./crash-resilience.md). Optionally cap dashboard memory
> on small-RAM boxes: `export HUSSH_ONE_DASHBOARD_MEM_CAP_MB=4096` (default 6144).

## .env keys (local, git-ignored)
```
WHATSAPP_MODE=self-chat
WHATSAPP_ENABLED=true
WHATSAPP_ALLOWED_USERS=<number>,<lid>,<lid>
WHATSAPP_ALLOW_ALL_USERS=true
WHATSAPP_ALLOWED_GROUPS=<jid>,<jid>
WHATSAPP_CAPSULE_GROUPS=<capsule-jid>
WHATSAPP_CAPSULE_RATE_MAX=30
WHATSAPP_CAPSULE_RATE_WINDOW_MS=60000
```

## Restarting safely (macOS)
```bash
# The supervisor retires the legacy detached dashboard watchdog, its orphaned
# child, and then starts the single launchd-owned watchdog on :9119.
scripts/hussh-one-supervisor.sh restart --manager launchd
```
Then verify: `tail ~/.hermes/logs/gateway.log` for `✓ whatsapp connected`.

## Health & audit
```bash
scripts/hussh-one-doctor.sh --require-services
scripts/hussh-one-guard.sh
python3 scripts/hussh-one-license-audit.py
python -m pytest tests/hermes_cli/test_hussh_one_*.py tests/gateway/test_whatsapp_*.py -q
python3 scripts/hussh-one-changelog-check.py
```

## Automatic self-chat doctor

Bootstrap installs the deterministic no-agent doctor at
`~/.hermes/scripts/hussh_one_doctor_heal.py` and updates the existing
**Hussh One Self-Healing Doctor** cron job in place; its ID, schedule, and
self-chat delivery target are preserved. The script is deliberately silent
when health is unchanged, because cron delivers script stdout verbatim.

- A new failure sends one self-chat alert; a resolved failure sends one
  recovery notice; an unresolved failure is reminded at most once every six
  hours.
- Alert state is private local runtime data at
  `~/.hermes/health/hussh-one-doctor-alert-state.json`. Deleting it merely
  re-establishes the alert baseline on the next run.
- The doctor may run the conservative WhatsApp janitor once per day when at
  least 1,000 files are safe-prunable. It never removes `creds.json` or
  `identity-key-*` files and remains silent when cleanup succeeds.

For a local dry verification without sending a message, run:

```bash
HERMES_HOME="$HOME/.hermes" .venv/bin/python ~/.hermes/scripts/hussh_one_doctor_heal.py
```

## Onboarding checklist (new machine or new session)
Run these, in order, whenever standing up a fresh Hussh One instance or
resuming work on one after a gap:

1. **Confirm remotes + branch.** `git remote -v` → `origin` = `hushh-labs/hussh-one-hermes`,
   `upstream` = `NousResearch/hermes-agent`; `git branch --show-current` → `main`.
2. **Check upstream drift** (informational, don't merge yet):
   `git fetch upstream --quiet && git rev-list --left-right --count HEAD...upstream/main`.
   Large drift (500+) → read [`upgrading.md`](./upgrading.md) and the
   `fork-upstream-merge-maintenance` playbook before merging.
3. **Run the doctor.** `scripts/hussh-one-doctor.sh --require-services` — surfaces branding,
   config, supervisor, WhatsApp, Vertex, Copilot BYOK, and **changelog freshness** in one pass.
4. **Read [`CHANGELOG.md`](../CHANGELOG.md)** — the dated index of everything Hussh One adds on
   top of upstream (WhatsApp/capsules, Vertex ADC, Copilot BYOK, Open WebUI, dashboard/TUI
   reliability, branding infra). This is the fastest way to get oriented on what's *ours* vs
   what's stock Hermes before touching anything.
5. **Check the feature catalog.** [`features/README.md`](../features/README.md) — one row per
   shipped capability, links to the deep-dive page for each.
6. **Verify contracts.** [`contracts/README.md`](../contracts/README.md) — the machine-checkable
   invariants; run the command block at the bottom.
7. **If you ship anything new touching a Hussh-One-only surface**, add a row to
   `CHANGELOG.md` in the same commit/session — re-run
   `python3 scripts/hussh-one-changelog-check.py` before calling the work done.

## Cron job conventions
- **Silent runs**: agent-driven jobs suppress delivery by replying with the
  canonical `[SILENT]` marker. The scheduler also accepts a bare `SILENT`
  token defensively (regression 2026-07-06: a job prompt said "reply with
  exactly: SILENT" and the literal word was delivered to the user's chat),
  but **job prompts should always instruct `[SILENT]`** — bracketed.
  Prose that merely contains the word "silent" still delivers.
- **Delivery guardrail**: cron jobs deliver only to `local`, `origin`, or the
  owner's own DM. Never fan out to groups.
- Tests: `tests/cron/test_scheduler.py::TestSilentDelivery`.

## See also
- [Changelog — dated index of every Hussh-One capability](../CHANGELOG.md)
- [Crash resilience — dashboard OOM & session-model persistence](./crash-resilience.md)
- [Upgrading from upstream](./upgrading.md)
- [`docs/hussh-one-deployment.md`](../../hussh-one-deployment.md)
- [`docs/hussh-one-upstream-maintenance.md`](../../hussh-one-upstream-maintenance.md)
