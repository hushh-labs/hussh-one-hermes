# Hussh One Deployment

Use this flow for a fresh machine that should run the Hussh One variant by default.

```bash
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
git remote add upstream https://github.com/NousResearch/hermes-agent.git

scripts/hussh-one-bootstrap.sh --manager auto --start
scripts/hussh-one-doctor.sh --require-services
```

The bootstrap creates or updates `.venv`, installs Hermes dependencies, builds the TUI/dashboard assets when Node is available, sets the Hussh One config defaults, checks Google Application Default Credentials without printing tokens, and reports whether WhatsApp pairing is still needed on this machine.

## Supervisor Policy

`scripts/hussh-one-supervisor.sh` owns dashboard and gateway lifecycle through one manager:

- macOS: `launchd`
- Linux host: user `systemd`
- s6/container: existing container supervisor services when present
- fallback: `screen`

The dashboard service runs `hermes dashboard --tui --no-open` on `127.0.0.1:9119`. The gateway/WhatsApp bridge keeps its health endpoint on `127.0.0.1:3000/health`. The supervisor refuses mixed screen/service-manager state unless `--clean-conflicts` is passed.

## Daily Commands

```bash
scripts/hussh-one-supervisor.sh status
scripts/hussh-one-supervisor.sh restart
scripts/hussh-one-doctor.sh --require-services
scripts/hussh-one-guard.sh
```

Before merging official Hermes updates:

```bash
git fetch upstream main --tags
git switch hussh-one-hermes
git branch "backup/hussh-one-before-upstream-$(date +%Y%m%d-%H%M%S)"
git merge --no-ff upstream/main
scripts/hussh-one-guard.sh
```

Keep secrets in `$HERMES_HOME/.env` or your shell. `.env.example` only documents non-secret Vertex selectors such as `GOOGLE_CLOUD_PROJECT`, `GCP_PROJECT`, and `GOOGLE_CLOUD_LOCATION`.
