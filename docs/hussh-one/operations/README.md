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
# kill orphaned bridge first to avoid stale code on port 3000
ps aux | grep bridge.js     # find PID
launchctl stop ai.hermes.gateway && kill -9 <bridge_pid> && launchctl start ai.hermes.gateway
```
Then verify: `tail ~/.hermes/logs/gateway.log` for `✓ whatsapp connected`.

## Health & audit
```bash
scripts/hussh-one-doctor.sh --require-services
scripts/hussh-one-guard.sh
python -m pytest tests/hermes_cli/test_hussh_one_*.py tests/gateway/test_whatsapp_*.py -q
```

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
- [Crash resilience — dashboard OOM & session-model persistence](./crash-resilience.md)
- [Upgrading from upstream](./upgrading.md)
- [`docs/hussh-one-deployment.md`](../../hussh-one-deployment.md)
- [`docs/hussh-one-upstream-maintenance.md`](../../hussh-one-upstream-maintenance.md)
