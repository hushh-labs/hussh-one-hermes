# Feature — Session-Model Persistence & Resume

## What it does
Keeps each session on the model the owner actually chose for it — across browser
refreshes, `hermes --resume`, and cold gateway restarts. A session running Claude
Opus comes back as Claude Opus, not the Gemini config default.

## Why it matters
Without it, every dashboard refresh silently downgraded a deliberate Claude
session to the cheap default model — and the downgraded Claude path then hit a
non-retryable Anthropic-direct 400 that retry-stormed context into an OOM crash.
Persisting and correctly rehydrating the per-session model removes both the
quality regression and the crash trigger. See
[Crash Resilience](../operations/crash-resilience.md) for the full chain.

## How it works (modules)
- **Persisted in `state.db`** — the chosen model lives in `sessions.model`
  (written on every `/model` switch and first turn).
- **Restored on resume** — `_make_agent` receives `session_id`, so the restore
  guard fires and reads `sessions.model`. Three call sites cover every surface:
  - `tui_gateway/server.py` — TUI / dashboard session open.
  - `gateway/platforms/api_server.py` — OpenWebUI / programmatic API.
  - `cli.py` — `_restore_session_model()`, invoked from both `_init_agent` (no
    preload) and `_preload_resumed_session` (dashboard preload path).
- **Claude → Vertex always** — restored `claude-*` models resolve through
  `hermes_cli.hussh_one_router._vertex_claude_runtime()`
  (`provider=google-vertex-claude`, `api_key=gcp-sdk`,
  `api_mode=anthropic_messages`), never Anthropic-direct OAuth.

## Config knobs
- `model.default` / `model.provider` — global default used **only** for genuinely
  new sessions (no stored model yet).
- An explicit `--model` on launch **wins** over restore (honour the operator).
- No new config required — restore is automatic and fail-safe.

## Triggering / Behavior
- New session → uses `model.default` (correct; nothing to restore).
- Resumed session with a stored model → restores that model + runtime.
- Resumed session, operator passed `--model` → honours the explicit flag.
- Restore failure → falls back to the stored model **name** and never raises out
  of the resume path.

## Privacy / Security
- Claude is pinned to GCP Vertex via ADC, so a stale Claude Code OAuth token is
  never used for API calls (avoids both the 400 and any cross-account billing).
- Any inherited Gemini credential pool is cleared on a Vertex restore so the
  agent rebuilds cleanly.

## Tests
- `tests/cli/test_resume_model_restore.py` (23 tests): Claude→Vertex restore,
  explicit-`--model` precedence, no-stored-model no-op, same-model no-op,
  non-Claude `switch_model` path, fail-safe on resolver error.

## Status
✅ Shipped.

## Future
- Surface the restored model in the dashboard session list UI.
- Per-capsule model pinning (cheap model for social capsules).
