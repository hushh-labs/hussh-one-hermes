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
- if the dashboard is running, it was launched with embedded chat enabled

When credentials are available, also run the live smoke:

```bash
.venv/bin/hermes chat --provider=google-vertex-claude -m claude-opus-4-8 -q "reply with ok"
```

## Restart After Passing

Only restart the dashboard and gateway after the guard passes. For local Hussh One operation, use:

```bash
scripts/hussh-one-restart.sh
```

This starts the dashboard with `--tui`, so the browser Chat tab embeds the real Hermes TUI. If the guard fails, resolve the merge or plugin contract first; do not paper over it with local config.

## Post-Merge Display / TUI Sanity Checks

The guard covers brand/provider integrity but does NOT catch config-driven UX regressions. After a merge, also confirm two things that have bitten us before:

1. **Conflict markers resolved cleanly in merged Python.** Merges that touch `tui_gateway/server.py` (model switching, prompt-submit) or `tests/hermes_cli/test_web_server.py` can leave subtle resolution gaps. Run the TUI gateway suite directly:
   ```bash
   python -m pytest tests/test_tui_gateway_server.py -q
   ```
   A `result.provider_label`-style `AttributeError` in `_apply_model_switch` will surface here — guard against it with `getattr(result, "provider_label", "")`, never bare attribute access on the `switch_model` result.

2. **The TUI / dashboard tool-call panel still populates.** The right-hand tool feed (and the dashboard `/api/events` sidebar) is gated on the GLOBAL `display.tool_progress` key, read by `tui_gateway/server.py::_load_tool_progress_mode()`. If that global is set to `false`/`off` to keep a messaging channel quiet, the TUI panel goes blank too. **Never silence tool progress globally for one channel.** Keep the global default at `all` and scope the mute per-platform:
   ```yaml
   display:
     tool_progress: all          # TUI/dashboard tool panel works everywhere
     platforms:
       whatsapp:
         tool_progress: 'off'    # WhatsApp groups stay clean
   ```
   The gateway resolves this through `gateway/display_config.py::resolve_display_setting()` (per-platform override beats global), so quiet WhatsApp output and a live TUI tool panel coexist.
