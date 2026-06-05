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

## Bootstrapping a new machine
```bash
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git switch hussh-one-hermes

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

## See also
- [Upgrading from upstream](./upgrading.md)
- [`docs/hussh-one-deployment.md`](../../hussh-one-deployment.md)
- [`docs/hussh-one-upstream-maintenance.md`](../../hussh-one-upstream-maintenance.md)
