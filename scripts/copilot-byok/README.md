# VS Code Copilot BYOK — Vertex ADC stack

Native VS Code Copilot Custom Endpoints (BYOK) backed by Google Vertex AI
through Application Default Credentials (ADC). Gives Copilot's chat, inline
edit, apply, `@workspace`, and **agent-mode tool calling** the same Vertex
models Hussh One uses — no third-party extension, no API keys pasted into a
cloud service.

## TL;DR

```bash
# One command, idempotent, re-runnable:
scripts/hussh-one-copilot-setup.sh --start

# Then in VS Code: Developer: Reload Window → pick a "Hussh One Vertex ADC" model.
```

## Architecture

```
VS Code Copilot
      │  http://127.0.0.1:8644/v1   (Bearer <master key>)
      ▼
┌──────────────────────────┐   :8644
│  auth shim               │   litellm_auth_shim.py (Starlette + httpx)
│  - deterministic 401s    │   - missing/wrong key → 401 (not 500/400)
│  - streaming passthrough │   - request & response streamed, no buffering
└──────────────────────────┘   - no body cap, no read timeout (1M-token safe)
      │  http://127.0.0.1:8643
      ▼
┌──────────────────────────┐   :8643
│  LiteLLM proxy           │   transparent Vertex→OpenAI passthrough
│  - forwards tools/stream │   - DB-less (master key via env, no Postgres)
└──────────────────────────┘
      │  Vertex AI (global), ADC
      ▼
  gemini-3.5-flash · claude-sonnet-4-6 · claude-opus-4-8
```

Both services bind to `127.0.0.1` only.

## Why two services (and why Copilot points at 8644, not 8643)

- **8643 LiteLLM proxy** is a *pure passthrough*. It forwards Copilot's
  `tools`, `tool_choice`, streaming, and system prompt straight to Vertex and
  returns the raw model response — so Copilot drives its own tool loop. (The
  Hermes API server on `8642` is a different thing: it runs *Hermes'* agent and
  drops the client's `tools` array, so it can't power Copilot agent mode.)
- **8644 auth shim** fixes a real defect: DB-less LiteLLM mislabels auth
  failures — a missing `Authorization` header returns `500` and a wrong key
  returns `400`, when both should be `401`. A wrong status code makes clients
  retry a permanent auth error as if it were transient, or conclude the proxy
  is down. The shim returns a correct `401` and otherwise streams through
  untouched.

## Scaling: large + refilling context windows

Copilot resends the entire (growing) transcript on every turn, and Gemini 3.5
Flash accepts ~1M input tokens. The shim is built so this never breaks:

- **Request body streamed upstream** (`request.stream()`) — a multi-MB chat
  payload is forwarded chunk-by-chunk at constant memory; no `413`, no size cap.
- **Response body streamed back** (`httpx` stream → `StreamingResponse`) — SSE
  token deltas reach Copilot immediately; a long completion never has to fit in
  RAM.
- **No read/total timeout** (`httpx.Timeout(read=None)`) — a large-context
  first-token can take tens of seconds; we never cut it off.
- **Pooled upstream client** (keep-alive) so concurrent requests are cheap.

Verified end-to-end: 60K-token (270 KB) and 160K-token (1.18 MB) prompts, a
5-turn refilling conversation (prompt_tokens climbing 4K→20K, all `200`), and
streaming-with-tools (Copilot agent-mode path).

## Files

Repo-canonical assets (source of truth):

| Path | Purpose |
|------|---------|
| `scripts/copilot-byok/litellm_auth_shim.py` | The shim (copied to `~/.hermes/scripts/`) |
| `scripts/copilot-byok/litellm-proxy-config.template.yaml` | Proxy config; `__VERTEX_PROJECT__` substituted at install |
| `scripts/hussh-one-copilot-setup.sh` | Idempotent installer |

Materialized into `~/.hermes/` at install time:

| Path | Notes |
|------|-------|
| `~/.hermes/scripts/litellm_auth_shim.py` | copy of the repo shim |
| `~/.hermes/scripts/start_litellm_proxy.sh` | launcher, carries the master key, `chmod 700` |
| `~/.hermes/scripts/start_litellm_shim.sh` | launcher, reads key from the proxy launcher, `chmod 700` |
| `~/.hermes/litellm-proxy-config.yaml` | proxy config with your project, `chmod 600` |
| `~/.hermes/litellm-venv/` | isolated venv with `litellm[proxy]` + `google-cloud-aiplatform` |
| VS Code `chatLanguageModels.json` | "Hussh One Vertex ADC" endpoint → `:8644` |

## Setup options

```
scripts/hussh-one-copilot-setup.sh [options]
  --project ID    Vertex/GCP project (default: $GOOGLE_CLOUD_PROJECT or gcloud)
  --start         Start/restart proxy + shim, then smoke test
  --no-vscode     Do not write chatLanguageModels.json
  --dry-run       Print actions without mutating the machine
```

The installer is idempotent: the master key is generated once and reused on
re-runs; existing non-Vertex Copilot endpoints (e.g. LM Studio) in
`chatLanguageModels.json` are preserved.

## Prerequisites

1. **gcloud ADC**: `gcloud auth application-default login`
2. **A Vertex-enabled GCP project** with Gemini + Claude access. Location is
   `global` (Claude is not servable in `us-central1`).
3. **VS Code** (Insiders or Stable) with Copilot Chat.

## Lifecycle / resilience

Both services are kept alive by the reaper watchdog —
`ensure_litellm_proxy()` in `~/.hermes/scripts/reap_stale_processes.py` (cron
every 30 min) probes `8643` and `8644` and respawns whichever is down (proxy
first, since the shim depends on it). Both are protected from being killed or
reniced by the reaper's process-signature guard.

## Health check

`scripts/hussh-one-doctor.sh` runs `check_copilot_byok`:

- asset presence (both launchers + shim), and
- a live probe: shim returns `401` on a no-auth `/v1/models` and `200` on
  `/healthz`.

It is a **warning, not a failure**, when BYOK isn't installed — the stack is
optional per machine.

## Manual verification

```bash
KEY=$(grep -o 'LITELLM_MASTER_KEY="[^"]*"' ~/.hermes/scripts/start_litellm_proxy.sh | cut -d'"' -f2)

# no auth → 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8644/v1/models

# correct key → 200 + models
curl -s http://127.0.0.1:8644/v1/models -H "Authorization: Bearer $KEY"

# chat with native tool call
curl -s http://127.0.0.1:8644/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"hi"}]}'
```

## Troubleshooting

- **Copilot shows "model unavailable" / errors**: confirm the shim is up
  (`curl http://127.0.0.1:8644/healthz`). If down, `scripts/hussh-one-copilot-setup.sh --start`
  (or wait for the reaper). Then Developer: Reload Window.
- **`No connected db` from `:8643`**: that's the raw proxy's DB-less auth path.
  Point Copilot at `:8644` (the shim), not `:8643` — the setup does this for you.
- **403 PERMISSION_DENIED from Vertex**: ADC project lacks model access, or you
  used a non-`global` location. Re-run with `--project <vertex-enabled-project>`.
- **Key mismatch**: the shim reads its key from the proxy launcher; re-running
  the setup keeps them in sync. Don't hand-edit one launcher's key only.
