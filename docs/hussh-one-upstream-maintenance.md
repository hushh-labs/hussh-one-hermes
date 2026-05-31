# hussh 🤫 One Upstream Maintenance

This fork is designed to keep Hermes updateable while carrying the Hussh One identity and Vertex Claude capability. It is not a 100% automatic guarantee: upstream can change provider contracts, config loaders, gateway behavior, or plugin discovery. The guarantee we can make is stronger and more useful: Hussh One has an explicit boundary and a guard script that must pass after every upstream or plugin update.

## Current Boundary

Fork-owned data and identity should stay in these surfaces:

- `HUSSH_ONE.md`
- `hermes_cli/brand.py`
- `hermes_cli/skins/hussh-one.yaml`
- `hermes_cli/dashboard_themes/hussh-one.yaml`
- `plugins/model-providers/google-vertex-claude/`
- Hussh One regression tests under `tests/`
- `scripts/hussh-one-guard.sh`

Generic Hermes changes should stay upstreamable:

- provider profile discovery in `hermes_cli/providers.py`
- auth profile registration in `hermes_cli/auth.py`
- model listing and switching in `hermes_cli/models.py` and `hermes_cli/model_switch.py`
- runtime client resolution in `hermes_cli/runtime_provider.py`
- Anthropic client construction in `agent/anthropic_adapter.py`
- agent runtime rebuild paths in `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py`, and `run_agent.py`
- auxiliary client resolution in `agent/auxiliary_client.py`
- provider contract typing in `providers/base.py`

The rule is: keep brand content in data/config/plugin files, and keep core edits generic enough that Hermes could accept them without Hussh-specific naming.

## Update Flow

Before pulling official Hermes changes, commit or stash local work. Then use an explicit merge branch so conflicts are reviewable:

```bash
git status --short
git fetch upstream main --tags
git switch main
git branch "backup/hussh-one-before-upstream-$(date +%Y%m%d-%H%M%S)"
git merge --no-ff upstream/main
scripts/hussh-one-guard.sh
```

`hermes update` already knows how to fetch the official repository through the `upstream` remote. For this fork, prefer the explicit flow above when Hussh One has carried commits, because it leaves merge conflicts visible and makes the guard mandatory before restart.

## Plugin Updates

Repo-shipped model providers live under `plugins/model-providers/`, and user-installed providers can override them from `$HERMES_HOME/plugins/model-providers/`. When upstream changes provider or plugin behavior:

- keep `google-vertex-claude` as a provider plugin, not a hardcoded branch
- keep `gcp_sdk` as a generic auth type, not a Hussh-specific switch
- keep provider aliases free of old product naming
- re-run the guard before restarting services

If a plugin API changes, fix the generic plugin/provider surface first, then verify the Hussh One provider still works through the same abstraction.

## Required Guard

Run this after every upstream merge, plugin update, or provider-runtime edit:

```bash
scripts/hussh-one-guard.sh
```

The guard checks:

- required Hussh One files still exist
- legacy brand strings did not reappear
- the old Vertex provider name did not reappear
- branding, WhatsApp prefix, model switching, provider discovery, runtime rebuild, and auxiliary-client tests pass
- the WhatsApp bridge remains syntactically valid

When credentials are available, also run the live smoke:

```bash
.venv/bin/hermes chat --provider=google-vertex-claude -m claude-opus-4-8 -q "reply with ok"
```

## Restart After Passing

Only restart the dashboard and gateway after the guard passes. If the guard fails, resolve the merge or plugin contract first; do not paper over it with local config.
