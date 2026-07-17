# VS Code Copilot BYOK — Vertex ADC stack

Native VS Code Copilot Custom Endpoints (BYOK) backed by Google Vertex AI
through Application Default Credentials (ADC). Gives Copilot's chat, inline
edit, apply, `@workspace`, and **agent-mode tool calling** the same Vertex
models Hussh One uses — no third-party extension and no Google/Vertex API key.
The installer generates a separate local bearer key solely for VS Code to
authenticate to the loopback-only auth shim; it is not sent to Vertex.

## TL;DR

```bash
# One command, idempotent, re-runnable:
scripts/hussh-one-copilot-setup.sh --start

# Then in VS Code: Developer: Reload Window → pick a "Hussh One Vertex ADC" model.
```

## Architecture

```
VS Code Copilot
      │  http://127.0.0.1:8644/v1   (generated local bearer key)
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
  gemini-3.5-flash · gemini-3.1-pro-preview · claude-sonnet-4-6 · claude-opus-4-8 · claude-sonnet-5 · claude-fable-5
```

Both services bind to `127.0.0.1` only.

### Authentication boundary

There are deliberately two credentials in this local chain:

| Hop | Credential | Purpose |
| --- | --- | --- |
| VS Code → `127.0.0.1:8644` | Installer-generated LiteLLM master key | Set as the `customendpoint` provider's `apiKey`; prevents another local process from using the proxy. It remains local. |
| Auth shim/proxy → Vertex AI | Google ADC access token | Authorizes the actual Gemini/Claude request and is minted/refreshed automatically. |

The first is the provider-level `apiKey` in VS Code's custom-endpoint configuration.
The second is the Vertex authentication mechanism. Do not substitute a Google
AI Studio key for ADC: that key only works against the separate direct Gemini
API, not the Vertex endpoint.

The installer writes the local key into both VS Code and VS Code Insiders
configurations automatically. After installation, reload the VS Code window and
select a **Hussh One Vertex ADC** model; a manual key entry is neither needed
nor valid. If VS Code asks for one, it has retained stale endpoint metadata —
reload the window (or restart VS Code) rather than leaving the field blank.

### VS Code Insiders compatibility mode

Some Insiders builds have registered a valid `customendpoint` configuration but
still dropped its provider `apiKey` from requests. For that specific upstream
defect, run setup with `--allow-unauthenticated-loopback`. The shim then accepts
only **headerless** requests received directly on its `127.0.0.1` listener and
adds the real key only for its private LiteLLM hop. Wrong or malformed supplied
keys still fail. This is an opt-in compatibility mode, not the default: any
local process, or a client using an SSH port forward, can call the endpoint
without the local bearer key while it is enabled.

## Context windows & output caps (live-probed, Jul 2026)

`hussh-one-copilot-setup.sh` writes these into `chatLanguageModels.json`. They
were **empirically probed against Vertex** (oversized requests, reading the
rejection boundaries), not copied from docs — keep them accurate: Copilot uses
`maxInputTokens` to drive its rolling-window/summarization heuristics, so
understating it truncates agent context early, while overstating it causes
hard 400s mid-conversation.

| Model | maxInputTokens | maxOutputTokens | Notes |
|-------|---------------|-----------------|-------|
| gemini-3.5-flash | 1,048,576 | 65,536 | output limit is `65537 (exclusive)` |
| gemini-3.1-pro-preview | 2,097,152 | 65,536 | adaptive thinking (low/medium/high) |
| claude-sonnet-4-6 | 1,000,000 | 128,000 | 1M native on Vertex — **no beta header needed** |
| claude-opus-4-8 | 1,000,000 | 128,000 | same |
| claude-sonnet-5 | 1,000,000 | 128,000 | same |
| claude-fable-5 | 1,000,000 | 128,000 | GLOBAL region only |

Probe method (repeatable): send `max_tokens: 2000000` → error message states the
real output cap; send a >1M-token prompt → error states the real input cap.

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

## Scaling: parallel coding agents (multi-region Gemini, 429-proof)

Vertex quota (`RESOURCE_EXHAUSTED` / HTTP 429) is **per-region-per-project**, so
several concurrent agents all hammering a single region exhaust it fast — the
classic `MidStreamFallbackError ... RateLimitError ... Available Model Group
Fallbacks=None`. The proxy config fixes this without raising any quota:

- **Multi-region Gemini pool** — `gemini-3.5-flash` is registered as one
  `model_name` across every region that actually serves it (verified by live
  probe: `global, asia-southeast1, asia-northeast1, asia-south1, europe-west2`).
  The LiteLLM router load-balances across them, so effective QPM ≈ sum of all
  regions. (Claude opus/sonnet are `global`-only on Vertex, so they stay single
  region but still get retries + cooldown.)
- **Retry + cooldown** (`router_settings`) — a region that returns 429 is cooled
  down for `cooldown_time` seconds and the request is retried (`num_retries`) on
  another region. Transient spikes never surface to Copilot.

Verified end-to-end: 24 and 60 fully-parallel Gemini calls through the shim →
**0× 429, 100% `200`** (3.5 s wall for 60). To re-probe regions after a model or
quota change, fire a tiny completion per candidate region and keep the ones that
return `200` (not `NotFound`).

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
  --launchd       (macOS) Install launchd KeepAlive agents — instant restart on
                  crash/OOM/sleep. Recommended; implies --start.
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

## Graceful resilience — why a proxy death is invisible

The `:8643` LiteLLM proxy buffers each full request/response, so a large Opus
agent turn can spike its RSS and get OOM/jetsam-killed by macOS mid-response.
Without protection that surfaces in VS Code as **"Server error: 502"** and the
turn is lost. Two coordinated layers make this a sub-second, invisible hiccup:

**1. Instant restart (launchd KeepAlive).** `--launchd` installs two user
LaunchAgents (`ai.hushh.one.litellm-proxy`, `ai.hushh.one.litellm-shim`) with
`KeepAlive{SuccessfulExit=false}` + `ThrottleInterval=1`. macOS respawns a dead
service in ~1s — no waiting on the 30-min reaper. This is the recommended
backbone; the reaper remains as a slower belt-and-suspenders fallback.

**2. Transparent retry in the shim.** The shim buffers the request body (capped
at `SHIM_MAX_BUFFER_MB`, default 64 — a 1M-token context is only a few MB) so it
can re-send safely. On a connect/transient failure **before the first response
byte**, it retries with bounded backoff for up to `SHIM_RETRY_BUDGET_S`
(default 20s, schedule 0.25→0.5→1→1.5→2→3s). So a request that lands during the
restart window simply **waits and succeeds** instead of erroring. The shim still
never buffers the *response* — completions stay streamed and constant-memory.

**Mid-stream death** (proxy dies after headers, when the status code is already
committed) can't be turned into a clean retry, so the shim emits a graceful tail:
for an SSE stream, a final OpenAI-shaped error event + `data: [DONE]` so Copilot
renders a "retry — service is back up" message instead of hanging on a truncated
read. The hard-down case (upstream never returns within the budget) returns a
correctly-typed `503` with `Retry-After`, not a `502`.

Verified end-to-end by hard-killing (`kill -9`) the proxy mid-flight: a
non-streaming request recovered in 3.7s (200, real answer), a streaming request
recovered with a clean `[DONE]`, a 45K-token context completed, and a 3×
rapid-kill restart storm stayed up throughout.

Tunables (env, read at shim launch): `SHIM_MAX_BUFFER_MB`, `SHIM_RETRY_BUDGET_S`.

## Onboarding a new model — graceful by construction

Adding a model to this stack (new `model_name` in the proxy config, a shim
edit, or both) goes through the **same safe path every time** — a bad entry
can never leave the live proxy broken or Copilot silently stuck on a stale
model:

1. **Render, don't overwrite.** The generated proxy config and shim are
   written to a `*.new` scratch file first — the live `litellm-proxy-config.yaml`
   / `litellm_auth_shim.py` are never touched by an unvalidated candidate.
2. **Validate before swap.** The candidate config must parse as YAML
   (`yaml.safe_load`); a candidate shim must compile (`python3 -m py_compile`).
   A validation failure aborts the run with the real parser error — nothing is
   swapped in.
3. **Snapshot before swap.** If a live file already exists, it's copied to
   `*.bak` immediately before the validated candidate replaces it — so there's
   always a last-known-good fallback on disk.
4. **Restart + smoke test.** `--start` restarts both services and runs the
   auth/chat smoke test (`401` no-auth, `200` authed chat) against the new
   config.
5. **Automatic rollback on failure.** If the smoke test fails, the script
   restores `*.bak` for both the config and the shim, restarts again, and
   re-verifies. Only if the rollback restart *also* fails does the script
   exit non-zero with a manual-attention pointer — a single bad model add can
   never take down the other five working models.
6. **Native tool-calling gate (two schema shapes, every model).** Once the
   base smoke test passes, every registered model is sent TWO real
   OpenAI-format `tools` requests: one with a **property-level** `anyOf`
   (a value can be one of several types) and one with a **root-level**
   `anyOf` (a conditional "provide field A OR field B" — the actual shape
   real MCP tools use, see the section below). Each model must return a
   valid `tool_calls[].function.name` for both, or the run prints a loud
   `FAIL` per model+shape (non-blocking — one model failing doesn't take
   the others down with it, but it can never ship silently).

Live-verified (2026-07-14): injected deliberately broken YAML into the
template, re-ran the installer — validation caught it, the live config stayed
byte-identical (`md5` before/after), and both services stayed up throughout
with zero manual intervention. Separately, all 6 registered models pass both
tool-calling gate shapes (12/12 PASS: `gemini-3.5-flash`, `gemini-3.1-pro-preview`,
`claude-sonnet-4-6`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-fable-5` ×
property-anyOf and root-anyOf).

**When you add a new model, this is the checklist:**
1. Add a `model_list` entry to `scripts/copilot-byok/litellm-proxy-config.template.yaml`.
2. Add its id + context/output caps to the `vertex_models` list in
   `hussh-one-copilot-setup.sh` (`write_vscode_config()`).
3. Add it to `SMOKE_MODELS` near the bottom of `hussh-one-copilot-setup.sh` so
   the tool-calling gate covers it automatically.
4. If it's also reachable via the native `gemini` / `google-vertex-claude`
   Hermes providers, add it to `hermes_cli/models.py` (`_PROVIDER_MODELS`) and
   `hermes_cli/natural_model_switch.py` (`_canonical_model()`) so natural-
   language switching ("switch to \<model\>") works without slash syntax —
   match on the **bare version number**, not just the full name, so "switch to
   gemini 3.1" resolves the same as "switch to gemini 3.1 pro".
5. Run `scripts/hussh-one-copilot-setup.sh --start` and confirm both smoke
   tests print `PASS` before considering the model shipped.
6. If the new model is Gemini-family, confirm `_is_gemini_model()` in
   `litellm_auth_shim.py` matches its id (it matches on `"gemini" in id`, so
   this should be automatic — verify with the root-anyOf gate anyway).

## Real MCP / native-tool compatibility — investigating the Gemini `anyOf` theory

**Correction (2026-07-14, same day as the original write-up below):** the
original version of this section claimed a confirmed production bug — Vertex
Gemini hard-400ing on root-level `anyOf` in MCP tool schemas — based on a
schema shape *inferred* from documentation, not from a captured real request.
That theory did not hold up under direct verification and is preserved below
only as a record of the investigation, followed by what verification actually
showed.

**What we verified directly**, with a live debug capture on the shim
(`HUSSH_SHIM_CAPTURE_TOOLS=1`, temporary — do not leave this on, it writes
full raw request bodies including conversation content to `/tmp`):

1. Captured ONE real, live VS Code Copilot agent-mode request against
   `gemini-3.1-pro-preview`, carrying Copilot's actual **86-tool manifest** —
   every native built-in tool (`run_in_terminal`, `read_file`, `create_file`,
   `replace_string_in_file`, `list_dir`, `grep_search`, `manage_todo_list`,
   `insert_edit_into_file`, etc.) plus every live MCP tool
   (`mcp_hushh-consent_*`, `mcp_hussh-wiki_*`, `mcp_next-devtools_*`,
   `mcp_plaid_*`, `mcp_shadcn_*`).
2. Scanned all 86 tool schemas for root-level `anyOf`/`oneOf`/`allOf`:
   **zero found.** The real, live `mcp_hushh-consent_check_consent_status`
   schema Copilot actually sends has **no `anyOf` at all** — VS Code's own
   tool-schema translation layer had already flattened the conditional
   requirement away before the request ever reached the model API. (One
   tool, `edit_notebook_file`, has a **property-level** `anyOf` — a value
   that can be `string` or `array` — which is the Vertex-safe shape, not the
   root-level one.)
3. Replayed that exact real captured 86-tool request against both
   `gemini-3.5-flash` and `gemini-3.1-pro-preview` through the live shim:
   **both returned `200 OK` with a correct `tool_calls: ["run_in_terminal"]`.**
   No 400, no schema rejection, no error of any kind.
4. Searched every VS Code chat session transcript on this machine for the
   actual Vertex error string (`functionDeclaration ... schema should be of
   type OBJECT`): **zero real occurrences.** The user-reported error
   (`route=/ria/;...ui_step_type=;ui_error=;error=`) that prompted this
   investigation turned out to be unrelated — it's a diagnostic string format
   from `hushh-research`'s own app-level e2e/smoke-test harness
   (`bootstrap_state`/`ui_flow`/`ui_step_type` fields), surfaced via
   `run_in_terminal` output while running native route tests. It has nothing
   to do with tool-calling schemas, Gemini, or Vertex.

**Conclusion:** as of 2026-07-14, Copilot's native built-in tools AND all
live MCP tools work correctly against Gemini through this BYOK stack, with
no schema-compatibility issue found. The `_scrub_tools_for_gemini()`
sanitizer in `litellm_auth_shim.py` and its test coverage remain in place as
a defense-in-depth safety net — they are harmless no-ops on schemas that
don't need them, and they protect against a real, documented category of
Vertex incompatibility that could reappear if a future MCP server or Copilot
version ever does emit a root-level composition keyword. But do not treat
the write-up below as a confirmed incident — it was a hypothesis that did
not survive contact with real captured traffic.

**If you hit a genuine tool-calling failure on Gemini in the future**, the
fastest path to ground truth is the same debug capture used here:
1. Temporarily add `export HUSSH_SHIM_CAPTURE_TOOLS="1"` to
   `~/.hermes/scripts/start_litellm_shim.sh`, restart the shim
   (`launchctl bootout` + `bootstrap` the `ai.hushh.one.litellm-shim`
   launchd job — a plain restart via the setup script won't pick up an env
   var change).
2. Trigger the failing Copilot turn once in VS Code.
3. Check `/tmp/hussh_shim_capture_*.json` for the raw request that failed —
   inspect its exact `tools` array against the real Vertex error message in
   `~/.hermes/logs/litellm-proxy.log`.
4. **Remove the `HUSSH_SHIM_CAPTURE_TOOLS` line and restart again once done**
   — it writes full raw request bodies (including conversation content) to
   `/tmp`, which is not something to leave running.

---

## Historical write-up (superseded by the correction above)

The section below is preserved for context but its central claim (a
confirmed production 400 from real MCP tool schemas) was not substantiated
by direct verification — see the correction above.

## Real MCP tool compatibility — the Gemini root-`anyOf` schema bug

**This is the class of bug that broke real Copilot sessions in production.**
VS Code Copilot's actual configured MCP servers (see `.mcp.json` /
`.vscode/mcp.json` in this repo and sibling repos — `shadcn`, `next-devtools`,
`plaid`, `hushh-consent`) expose tools with real, non-trivial JSON schemas.
Two of the `hushh-consent` MCP server's tools — `check_consent_status` and
`request_consent` — use a **root-level `anyOf`** to express a conditional
requirement ("must provide `scope` OR `request_id`"):

```json
{
  "type": "object",
  "properties": { "user_id": {"type": "string"}, "scope": {"type": "string"}, "request_id": {"type": "string"} },
  "required": ["user_id"],
  "anyOf": [{"required": ["scope"]}, {"required": ["request_id"]}]
}
```

**Vertex's Gemini function-calling validator hard-rejects this** with a
non-retryable `400`:
```
"Unable to submit request because `check_consent_status` functionDeclaration
parameters schema should be of type OBJECT."
```
even though `type: "object"` IS present — Vertex's Gemini backend simply
can't parse a root-level composition keyword (`anyOf`/`oneOf`/`allOf`)
alongside it. **Claude on the exact same Vertex proxy accepts this shape
natively — this is Gemini-specific.** And because Vertex validates the
*entire* `tools` manifest up front (not per-call), this 400 fires on **every**
Gemini turn where Copilot includes the tool in its manifest — even if the
model never calls it. From Copilot's side this surfaces as an opaque agent
error with no obvious cause.

**Fix — `_scrub_tools_for_gemini()` in `litellm_auth_shim.py`:** for
Gemini-bound requests only (detected via `_is_gemini_model()` — never touches
Claude), every tool's `parameters` schema is checked for a root-level
`anyOf`/`oneOf`/`allOf`. If found, the composition keyword is stripped and
its meaning is folded into the schema's `description` as a plain-English
note (e.g. `"Must also provide `scope` or `request_id`."`) so Gemini still
understands the constraint even though the JSON Schema conditional is gone.
**Property-level `anyOf`** (e.g. a `location` field that can be a `string` or
`integer`) is a *different*, Vertex-safe shape and is left completely
untouched — only root-level composition on the tool's top-level parameters
object is rewritten.

Verified live end-to-end against the exact real `hushh-consent` tool schemas:
`check_consent_status` and `request_consent`, both models
(`gemini-3.5-flash`, `gemini-3.1-pro-preview`), both returned a correct
`200` with a valid `tool_calls[].function` after the fix (previously a hard
`400` on every attempt). Unit-tested in
`tests/scripts/copilot_byok/test_litellm_auth_shim_gemini_schema.py` (17
tests) and covered by the `hussh-one-copilot-setup.sh` tool-calling gate's
`root-anyOf` case on every setup run — see the checklist above.

**If you add a new MCP server / tool with a conditional-requirement schema**
(anything using `anyOf`/`oneOf`/`allOf` at the schema root to say "one of
these fields is required"), it's automatically covered by this fix — no
action needed on the MCP-server side. If Gemini still fails on a new tool
shape, check the shim log for `"stripped root-level anyOf/oneOf/allOf"` to
confirm the sanitizer fired, then inspect the raw Vertex error for a
different validation issue (the sanitizer only handles this one specific
composition-keyword rejection).

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

# no auth -> 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8644/v1/models

# correct key -> 200 + models
curl -s http://127.0.0.1:8644/v1/models -H "Authorization: Bearer ${KEY}"

# chat with native tool call
curl -s http://127.0.0.1:8644/v1/chat/completions \
  -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"hi"}]}'
```

## Troubleshooting

- **Copilot shows "model unavailable" / errors**: confirm the shim is up
  (`curl http://127.0.0.1:8644/healthz`). If down, `scripts/hussh-one-copilot-setup.sh --start`
  (or wait for the reaper). Then Developer: Reload Window.
- **`No connected db` from `:8643`**: that's the raw proxy's DB-less auth path.
  Point Copilot at `:8644` (the shim), not `:8643` — the setup does this for you.
- **403 PERMISSION_DENIED from Vertex**: ADC project lacks model access, or you
  used a non-`global` location. Re-run with `--project <vertex-enabled-project>`.
- **Gemini agent turn fails/errors with an opaque agent-side message
  (route/step-type garbage, blank tool result, or the agent claims a tool
  "doesn't exist") while Claude works fine on the same tool**: almost
  certainly the root-`anyOf` schema bug (see "Real MCP tool compatibility"
  above) — check the raw upstream response for `functionDeclaration
  parameters schema should be of type OBJECT`. Confirm the shim log shows
  `"stripped root-level anyOf/oneOf/allOf"` for that tool/model; if it
  doesn't appear, the sanitizer isn't running (stale shim process — bootout +
  bootstrap the `ai.hushh.one.litellm-shim` launchd agent, or re-run
  `hussh-one-copilot-setup.sh --start`, and verify the PID actually changed).
- **Key mismatch**: the shim reads its key from the proxy launcher; re-running
  the setup keeps them in sync. Don't hand-edit one launcher's key only.
- **New model shows `tool_call=FAIL` in the setup output**: the model doesn't
  support (or mishandles) native OpenAI-format tool calling on Vertex. Don't
  wire it into Copilot's agent-mode workflows until this passes — check the
  raw response body for a schema-translation error vs. a genuine
  no-tool-support model.
- **A model add broke the whole stack**: it shouldn't — `hussh-one-copilot-setup.sh`
  auto-rolls-back to the last-known-good config/shim on smoke-test failure. If
  you see `"New config was broken and has been rolled back"`, fix the model
  entry in the template and re-run; the live stack is already safe again.
