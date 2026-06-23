# Feature — Open WebUI (Browser Chat Variant)

## What it does
Runs **🤫 Hussh One in a full browser chat UI** — [Open WebUI](https://github.com/open-webui/open-webui) talking to Hermes' OpenAI-compatible API server. It is the third first-class Hussh One surface alongside the **TUI/dashboard** and **WhatsApp**: same agent, same models, same router, rendered as a polished web chat with streaming reasoning and tool-activity status.

## Why it matters
Not everyone lives in a terminal or WhatsApp. Open WebUI gives Hussh One a shareable, multi-user, mobile-friendly web client without forking a frontend — Hermes simply advertises itself as an OpenAI model and Open WebUI is the renderer. We actively optimize this path for cost and responsiveness.

## How it works (modules)
- **`gateway/platforms/api_server.py`** — OpenAI-compatible endpoints:
  - `GET /v1/models` — advertises the Hermes agent (one entry per profile).
  - `POST /v1/chat/completions` — streamed agent turns (SSE).
  - `GET /v1/runs/{id}` + `/v1/runs/{id}/events` — structured lifecycle events.
  - `GET /health` · `/health/detailed` — cross-container probes.
- **`scripts/setup_open_webui.sh`** — idempotent bootstrap: seeds `~/.hermes/.env`, installs Open WebUI into a venv, writes a launcher, and optionally installs a user service (launchd / systemd `--user`).
- **Streaming polish** — `_sanitize_reasoning_chunk()` cleans `<thinking>`/`<reasoning>` wrappers for GUI rendering; `_tool_status_description()` produces ADK-style human status lines ("Searching files…") instead of raw tool names; SSE token batching + error handling tuned for Open WebUI throughput.

## Config knobs (env, via setup script)
| Var | Default | Purpose |
|-----|---------|---------|
| `OPEN_WEBUI_PORT` / `OPEN_WEBUI_HOST` | `8080` / `127.0.0.1` | Where the web UI listens |
| `OPEN_WEBUI_NAME` | `Hermes Agent WebUI` | Branding shown in the UI |
| `OPEN_WEBUI_AUTH` | `True` | `False` = open access, no login (fresh DB only) |
| `OPEN_WEBUI_ENABLE_TITLE_GENERATION` | `False` | `True` costs a full extra agent run per chat to auto-title |
| `OPEN_WEBUI_ENABLE_TAGS_GENERATION` | `False` | `True` costs a full extra agent run per chat to auto-tag |
| `HERMES_API_PORT` / `HERMES_API_HOST` | `8642` / `127.0.0.1` | Hermes OpenAI-compatible API server |
| `HERMES_API_MODEL_NAME` | `Hermes Agent` | Model name advertised to `/v1/models` |

## Hussh One performance defaults
Title/tag generation are **off by default** so Open WebUI stays at **one Hermes agent call per message** — each auto-title/tag would otherwise fire a full extra server-side agent run on the heavy engine. This is the cost-optimization "open-access + performance" baseline baked into the setup generator.

## Reliability
Open WebUI runs the **same agent** as the dashboard, so it inherits the
[session-model resume](./session-model-resume.md) + Vertex-Claude pinning fixes:
a Claude session restored via the API server re-pins to GCP Vertex (ADC), never
Anthropic-direct. See [Crash resilience](../operations/crash-resilience.md).

## Triggering / Behavior
- Browser → Open WebUI (`:8080`) → Hermes API server (`:8642/v1`) → agent turn streamed back token-by-token with reasoning + tool status.
- Per-session model is persisted and restored exactly like the TUI/dashboard.

## Privacy / Security
- Defaults to loopback (`127.0.0.1`); expose deliberately.
- `OPEN_WEBUI_AUTH=False` only engages on a fresh DB with zero users — once a user exists, the login form stays.

## Tests / Verification
```bash
bash -n scripts/setup_open_webui.sh
curl -s http://127.0.0.1:8642/v1/models | jq .          # API server advertises the agent
curl -s http://127.0.0.1:8642/health/detailed | jq .    # rich status probe
```

## In-app Features page (Pipe Function)
Open WebUI is a separate Svelte app with a prebuilt frontend, so a Features page
is shipped as an **upgrade-safe Pipe Function** (lives in OWU's function DB, not
the bundle):
- **`scripts/open-webui/hussh_one_features_pipe.py`** — the Pipe. Registers one
  selectable entry, **"🤫 Hussh One — Features"**, in the model dropdown.
  Selecting it and sending any message renders the feature catalog as markdown
  in the main chat body.
- **`scripts/open-webui/install_features_pipe.py`** — idempotent installer
  (upserts the function row, owns it with the first admin user, marks it
  active + global).
- Auto-installed by `setup_open_webui.sh` (`install_features_pipe`). On a fresh
  box the OWU DB only exists after first launch — re-run the installer once after
  the first start:
  ```bash
  ~/.local/open-webui-venv/bin/python scripts/open-webui/install_features_pipe.py
  ```
- Source of truth for the catalog content stays `docs/hussh-one/features`; keep
  the pipe markdown in sync when features change.

## Status
✅ Shipped and actively optimized.

## Future
- First-class per-profile model picker inside Open WebUI.
- Surface the live model + reasoning-effort badge in the web header (parity with the TUI status bar).
