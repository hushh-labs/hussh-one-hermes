# hussh 🤫 One Upstream Maintenance

This fork is designed to keep Hermes updateable while carrying the Hussh One identity and Vertex Claude capability. It is not a 100% automatic guarantee: upstream can change provider contracts, config loaders, gateway behavior, or plugin discovery. The guarantee we can make is stronger and more useful: Hussh One has an explicit boundary and a guard script that must pass after every upstream or plugin update.

## Current Boundary

Fork-owned data and identity should stay in these surfaces:

- `HUSSH_ONE.md`
- `hermes_cli/brand.py`
- `hermes_cli/hussh_one_header.py`
- `hermes_cli/skins/hussh-one.yaml`
- `hermes_cli/dashboard_themes/hussh-one.yaml`
- `plugins/model-providers/google-vertex-claude/`
- `plugins/platforms/whatsapp/adapter.py` (final delivery normalization only)
- `scripts/whatsapp-bridge/bridge.js` (transport, self-chat/capsule gates, and
  no presentation policy)
- `scripts/hussh-one-bootstrap.sh`
- `scripts/hussh-one-supervisor.sh`
- `scripts/hussh-one-doctor.sh`
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

## Canonical Branch Model (READ THIS FIRST)

There is exactly **ONE** long-lived branch for this fork:

- **`main`** on `hushh-labs/hussh-one-hermes` (a PRIVATE repo) is the single
  canonical trunk **and** the GitHub default branch. Everything ships from here:
  the Hussh One identity, the Vertex Claude capability, the WhatsApp gateway
  customizations, and every merge of official upstream Hermes.

History note (why this matters): there used to be a second branch,
`hussh-one-hermes`, that was the GitHub default. The two drifted apart — `main`
carried the upstream merges + recent work while `hussh-one-hermes` carried 9
unique Hussh One features (capsules, the upgrade-safe header module, the
bootstrap/doctor/supervisor scripts). On 2026-06-07 they were reconciled: all
9 features were merged into `main`, the GitHub default was repointed to `main`,
and `hussh-one-hermes` was **deleted** (local + remote). Its content is
preserved forever in the tag `safety/hussh-one-hermes-20260607-232148`.

**Hard rules to never repeat the drift:**

1. **Never create a second long-lived "trunk-like" branch.** Feature work goes
   on short-lived `feat/*` / `fix/*` branches that merge into `main` and are
   then deleted. `main` is the only place that accumulates Hussh One identity.
2. **Always commit/push to `main`.** Before starting work, confirm you're on it:
   `git branch --show-current` must print `main`.
3. **The running gateway uses THIS checkout's venv** (`.venv`), so whatever is on
   `main` here is what actually runs after a restart. There is no separate
   "runtime" branch to keep in sync.
4. The remotes are fixed: `origin` → `hushh-labs/hussh-one-hermes` (our private
   canonical repo), `upstream` → `NousResearch/hermes-agent` (official Hermes).
   `upstream` is input only. Disable its push URL once per clone so an
   accidental `git push upstream` cannot target the official repository:
   `git remote set-url --push upstream DISABLED`.
5. Official Hermes is authoritative for every generic core contract. A Hussh
   difference must be either a small overlay at the boundary above or a
   documented, tested exception; it is never a reason to freeze upstream.

## Remotes & Sync State (quick check)

```bash
git remote -v                                   # origin = hushh-labs, upstream = NousResearch
git branch --show-current                       # must be: main
git fetch upstream --quiet && git fetch origin --quiet
git rev-list --left-right --count upstream/main...HEAD
#   left  = commits we are BEHIND upstream (need to merge)
#   right = our Hussh One commits AHEAD of upstream (expected, large)
git log origin/main..HEAD --oneline             # unpushed local commits (should be empty after a push)
```

## Update Flow (pulling latest official Hermes)

Never test a large upstream merge on the running `main` checkout. Reconcile it
on a short-lived `sync/upstream-<date>` branch, where upstream changes win by
default and every Hussh exception must be reapplied deliberately. Only merge
that verified branch into `main` after the guard and live smoke pass.

```bash
git switch main
git status --short                              # working tree must be clean (stash if not)
git fetch upstream --tags --quiet

# 1. Safety net — immutable, survives even a botched merge. Push it offsite too.
TS=$(date +%Y%m%d-%H%M%S)
git tag "safety/main-$TS" main
git push origin "safety/main-$TS"

# 2. Reconcile official Hermes away from the running trunk.
git switch -c "sync/upstream-$(date +%Y%m%d)" main
git merge --no-ff upstream/main

# 3. Resolve conflicts by taking upstream's generic behavior first, then add
#    only documented Hussh overlays (see the playbook below).
scripts/hussh-one-guard.sh

# 4. Run the real local route smoke before changing the runtime branch.
.venv/bin/hermes chat --provider=google-vertex-claude -m claude-opus-4-8 -q "reply with ok"

# 5. Fast, reviewable handoff into the sole runtime trunk, then restart.
git switch main
git merge --no-ff "sync/upstream-$(date +%Y%m%d)"
git push origin main
scripts/hussh-one-restart.sh        # or: hermes gateway restart
```

To abort a merge that has gone wrong before committing: `git merge --abort`.
To roll back a bad merge that was already committed but not pushed:
`git reset --hard safety/main-<TS>`.

## Conflict-Resolution Playbook

Conflicts almost always land in the same files. Resolve by intent, not by
blindly picking a side:

| File | What to keep |
|------|--------------|
| `hermes_cli/brand.py` | OUR emoji-first `BRAND_DISPLAY_NAME = "🤫 Hussh One"`. Never let upstream's generic Hermes branding win. |
| `hermes_cli/skins/hussh-one.yaml`, `dashboard_themes/hussh-one.yaml` | OUR `🤫 Hussh One` branding strings. |
| `gateway/run.py` (WhatsApp header block) | Keep the generic upstream gateway flow and its call into `ensure_single_whatsapp_header(...)`; do not duplicate formatting in the gateway. |
| `hermes_cli/hussh_one_header.py` | Hussh-owned canonical header/finalizer. Extend its parsers only for verified new echo forms. |
| `plugins/platforms/whatsapp/adapter.py` | Keep upstream transport changes and retain only final idempotent normalization before a payload reaches the bridge. |
| `scripts/whatsapp-bridge/bridge.js` | Keep upstream/capsule/rate-limit behavior, but it must never compose a header or honor a fallback prefix. Run `node --check` after. |
| `agent/conversation_loop.py` | Adopt upstream refactors (e.g. `turn_context.py`, `TurnRetryState`) but RE-PORT our Vertex Claude pre-turn access check and `vertex_claude_locations_attempted` location recovery on top. |
| `scripts/install.sh`, `install.ps1` | `BRANCH="main"` and `origin` pointing at `hushh-labs/hussh-one-hermes`. |

After resolving, ALWAYS:

```bash
# 0 conflict markers anywhere:
grep -rnE '^(<<<<<<< |>>>>>>> |=======$)' --include="*.py" --include="*.js" .
# Python still parses:
python -c "import agent.conversation_loop, gateway.run, gateway.platforms.whatsapp, hermes_cli.hussh_one_header"
# JS still parses:
node --check scripts/whatsapp-bridge/bridge.js
```

`hermes update` knows how to fetch official Hermes through the `upstream` remote,
but prefer the explicit flow above whenever Hussh One has carried commits,
because it leaves merge conflicts visible and makes the guard mandatory before
restart.

## WhatsApp Delivery Contract

There is one presentation owner for every WhatsApp text or caption: the Python
delivery boundary (`ensure_single_whatsapp_header` in
`hermes_cli/hussh_one_header.py`). The Node Baileys bridge is transport only.

Normal delivery is exactly:

```text
🤫 Hussh One
<Display Model> · <Safe Route> · [A|S]
════════════════════
<body>
```

- `[A]` is the configured automatic route, including automatic escalation.
- `[S]` is retained only from an explicit per-session model selection; the
  finalizer preserves an already-composed selected header rather than replacing
  it with the current default.
- Gateway replies, direct `send_message` calls, cron delivery, and media
  captions all pass through the same idempotent finalizer. If a model echoes a
  second header, it is removed before send.
- `WHATSAPP_REPLY_PREFIX` / `whatsapp.reply_prefix` remain explicit emergency
  operator overrides. They are applied once in Python; they are never passed
  to the bridge. An empty override intentionally emits no header.

When a repeated header appears, inspect the exact payload at the Python
boundary and run the focused header/adapter tests before changing bridge
formatting. Adding another prefix in the Node process is a regression.

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
- the bootstrap, supervisor, doctor, and restart shell entry points remain syntactically valid

When credentials are available, also run the live smoke:

```bash
.venv/bin/hermes chat --provider=google-vertex-claude -m claude-opus-4-8 -q "reply with ok"
```

## Restart After Passing

Only restart the dashboard and gateway after the guard passes. For local Hussh One operation, use:

```bash
scripts/hussh-one-supervisor.sh restart
scripts/hussh-one-doctor.sh --require-services
```

`scripts/hussh-one-restart.sh` remains as a compatibility wrapper around the supervisor restart command. The supervisor starts the dashboard with `--tui`, so the browser Chat tab embeds the real Hermes TUI. If the guard fails, resolve the merge or plugin contract first; do not paper over it with local config.

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
