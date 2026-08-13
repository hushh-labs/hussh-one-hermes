# Feature — Open WebUI (Browser Chat Variant)

## What it does
Runs **🤫 Hussh One in a full browser chat UI** — [Open WebUI](https://github.com/open-webui/open-webui) talking to Hermes' OpenAI-compatible API server. Hussh One is the agent identity, never a selectable model. It is the third first-class Hussh One surface alongside the **TUI/dashboard** and **WhatsApp**: same agent, same models, same router, rendered as a polished web chat with streaming reasoning and tool-activity status.

## Why it matters
Not everyone lives in a terminal or WhatsApp. Open WebUI gives Hussh One a mobile-friendly web client without forking a frontend. Hermes advertises concrete provider models while Open WebUI renders the One agent experience. We actively optimize this path for cost and responsiveness.

## How it works (modules)
- **`gateway/platforms/api_server.py`** — OpenAI-compatible endpoints:
  - `GET /v1/models` — advertises concrete configured model routes, with the current default first; the agent name is retained only as a legacy fallback when no routes exist.
  - `POST /v1/chat/completions` — streamed agent turns (SSE).
  - `GET /v1/runs/{id}` + `/v1/runs/{id}/events` — structured lifecycle events.
  - `GET /health` · `/health/detailed` — cross-container probes.
- **`scripts/setup_open_webui.sh`** — idempotent bootstrap: seeds `~/.hermes/.env`, installs Open WebUI into a venv, writes a launcher, and optionally installs a user service (launchd / systemd `--user`).
- **Optional document conversion** — on macOS the bootstrap attempts to install
  `pandoc` when Homebrew is available. A missing or non-writable Homebrew
  installation now produces a warning and continues, so the core browser chat
  and service setup remain available on a fresh device.
- **Streaming polish** — `_sanitize_reasoning_chunk()` cleans `<thinking>`/`<reasoning>` wrappers for GUI rendering; `_tool_status_description()` produces ADK-style human status lines ("Searching files…") instead of raw tool names; SSE token batching + error handling tuned for Open WebUI throughput.

## Config knobs (env, via setup script)
| Var | Default | Purpose |
|-----|---------|---------|
| `OPEN_WEBUI_PORT` / `OPEN_WEBUI_HOST` | `8080` / `127.0.0.1` | Where the web UI listens |
| `OPEN_WEBUI_NAME` | `🤫 Hussh One` | Branding shown in the UI |
| `OPEN_WEBUI_AUTH` | `False` | Passwordless personal access; permitted only on loopback |
| `OPEN_WEBUI_VERSION` | `0.10.2` | Repository-tested companion version; upgrades happen as a controlled Hussh contract change |
| `OPEN_WEBUI_MODELS_CACHE_TTL` | `300` | Avoid repeated model-list calls while allowing bounded refresh |
| `OPEN_WEBUI_STREAM_DELTA_CHUNK_SIZE` | `5` | Batches tiny stream deltas to reduce browser/render overhead without making output feel delayed |
| `OPEN_WEBUI_ENABLE_TITLE_GENERATION` | `False` | `True` costs a full extra agent run per chat to auto-title |
| `OPEN_WEBUI_ENABLE_TAGS_GENERATION` | `False` | `True` costs a full extra agent run per chat to auto-tag |
| `HERMES_API_PORT` / `HERMES_API_HOST` | `8642` / `127.0.0.1` | Hermes OpenAI-compatible API server |
| `HERMES_API_MODEL_NAME` | `🤫 Hussh One` | Legacy fallback name advertised only when no concrete provider route is available |

## Hussh One performance defaults
Title/tag generation are **off by default** so Open WebUI stays at **one Hermes agent call per message** — each auto-title/tag would otherwise fire a full extra server-side agent run on the heavy engine. The managed launcher also caches the stable Hermes model catalog for five minutes, bounds OpenAI-compatible connection waits, batches very small stream deltas, uses the faster JSON serializer, disables per-token database writes, scopes CORS to the local UI origins, and pins Open WebUI to one worker because its default local SQLite/Chroma persistence is not multi-worker safe.

Open WebUI is the browser front door, not a second agent runtime. Its native
subagents are disabled in the managed launcher: Hermes owns orchestration,
MCP/consent tools, model routing, memory, and cancellation. This prevents two
agent loops from duplicating work, tool calls, context, and cost.

The streaming path follows the proven Hussh Search Console shape without
duplicating its search planner: one warm agent loop emits reasoning, answer
deltas, and tool lifecycle events as they happen; no second synthesis call is
introduced. SSE disables proxy buffering, sends keepalives during long tools,
flushes final tails, reports usage, and interrupts agent work when the browser
disconnects.

Open WebUI's top bar is the single model picker. The composer adds only a static
**Thinking** label and a compact level selector (`Off` through `Max`) beside
voice input. Reasoning is a per-request override and does not mutate the global
default. Hussh branding remains visible when the sidebar control is hovered; a
separate, persistent expand button sits on the following row. Changelog is
icon-only in the compact sidebar and labeled only when expanded, so it never
forces the collapsed rail open.

### Google ADK alignment

This integration deliberately does **not** embed Google ADK into Open WebUI or
pretend that the prebuilt frontend is an ADK app. Open WebUI speaks the standard
OpenAI-compatible Chat Completions protocol to Hermes. Hermes follows the same
runtime principles that matter here: SSE partial events are yielded promptly,
reasoning and tool/source events remain distinct, final responses close the
stream deterministically, and browser disconnects cancel server-side work.
Independent subagent branches may run concurrently inside Hermes; dependent
work remains sequential. That keeps one owner for state, consent, tools, and
session history while preserving ADK-quality streaming semantics.

## Reliability
Open WebUI runs the **same agent** as the dashboard, so it inherits the
[session-model resume](./session-model-resume.md) + Vertex-Claude pinning fixes:
a Claude session restored via the API server re-pins to GCP Vertex (ADC), never
Anthropic-direct. See [Crash resilience](../operations/crash-resilience.md).

The launcher records the setup-script revision under `$HERMES_HOME` and
reconciles the companion before startup when a later Hussh One update changes
that contract. Reconciliation installs the repository-tested Open WebUI
version, reapplies Hussh assets and runtime defaults, verifies Hermes'
authenticated `/v1/models` contract, and keeps the last known-good runtime
available if reconciliation fails. launchd/systemd then keeps the resulting
process alive, while the Hussh supervisor checks its local health endpoint.
Setup does not restart an already-healthy Hermes gateway when API configuration
is unchanged, avoiding a preventable connection-error window. Open WebUI startup
also waits for the authenticated Hermes model endpoint.

Setup also persists a stable `WEBUI_SECRET_KEY` in `~/.hermes/.env`. An
existing Open WebUI key is migrated on upgrade, preserving encrypted MCP/OAuth
credentials across restarts and new companion reconciliations instead of
regenerating a checkout-local `.webui_secret_key`.

## Triggering / Behavior
- Browser → Open WebUI (`:8080`) → Hermes API server (`:8642/v1`) → agent turn streamed back token-by-token with reasoning + tool status.
- Per-session model is persisted and restored exactly like the TUI/dashboard.

## Privacy / Security
- Defaults to passwordless loopback access (`127.0.0.1`). Setup refuses to
  combine passwordless mode with a non-loopback bind.
- A pre-existing single-user database is backed up and migrated in place while
  preserving its user ID, chats, and settings. Multi-user databases fail closed
  and require `OPEN_WEBUI_AUTH=True`.
- Migration backups are stored under the Open WebUI data directory's
  `backups/` folder.

## Tests / Verification
```bash
bash -n scripts/setup_open_webui.sh
curl -s http://127.0.0.1:8642/v1/models | jq .          # API server advertises real model routes
curl -s http://127.0.0.1:8642/health/detailed | jq .    # rich status probe
```

## In-app Features page
Open WebUI is a separate Svelte app with a prebuilt frontend. The setup script
generates the Hussh Features experience directly in its managed static assets,
avoiding function-database injection that changes across Open WebUI releases.
The feature documentation in this directory remains the source of truth.

## Status
✅ Shipped and actively optimized.

## Future
- First-class per-profile model picker inside Open WebUI.
- Surface the live reasoning-effort badge in the web header (parity with the TUI status bar).
