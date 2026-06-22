# Crash Resilience — Dashboard OOM & Session-Model Persistence

How Hussh One survives long, heavy sessions without the dashboard dying or a
resumed session silently downgrading its model. This page documents the two
root causes we hit in production and the four layers of defense that now prevent
them.

> TL;DR — A long Claude session used to (1) silently revert to Gemini on resume,
> then (2) retry-storm an Anthropic-direct 400, growing context until the
> dashboard process was **OOM-killed (SIGKILL, `rc=-9`)** mid-write. The user
> only ever saw *"connection lost — reconnecting"*. Both causes are now fixed and
> the supervisor can no longer be hard-killed by a runaway session.

---

## Symptom

Browser dashboard Chat tab shows:

```
[connection lost — reconnecting…]
[connection lost — reconnecting…]
```

In the logs (`~/.hermes/logs/`):

```
[dashboard-watchdog] child exited rc=-9        # SIGKILL = kernel OOM kill
[dashboard-watchdog] dashboard port down; starting child
❌ HTTP 400: Third-party apps now draw from your extra usage…  # Anthropic-direct
⚠️ Iteration budget exhausted (90/90)
Context: 526 msgs, ~232,361 tokens             # runaway growth
```

---

## Root cause chain

1. **Session-model revert.** On dashboard refresh / `hermes --resume`, the agent
   was rebuilt from `config.yaml`'s `model.default`, so a session last running
   **Claude Opus** came back as **Gemini**. The per-session model (persisted in
   `state.db` → `sessions.model`) was not rehydrated because `_make_agent` was
   called **without** `session_id`, so the restore guard (`… and session_id`)
   never fired.
2. **Wrong Claude runtime.** When a Claude model *was* restored, `switch_model()`
   mapped the bare `claude-*` name to `provider="anthropic"` and grabbed a Claude
   Code OAuth token, which Anthropic rejects for API use → `HTTP 400: third-party
   apps now draw from your extra usage`. Non-retryable, but the loop kept trying.
3. **Context runaway → OOM.** The retry storm + late compaction let the session
   balloon to 500+ messages / 230k+ tokens. RSS blew past the ceiling and the
   kernel **SIGKILLed** the dashboard child mid-write — risking a corrupt
   `state.db` and surfacing only as "connection lost".

---

## The four-layer fix

| Layer | Where | What it does |
|-------|-------|--------------|
| **1. Resume restores the model** | `tui_gateway/server.py`, `cli.py` | Pass `session_id` into `_make_agent`; `cli.py` gains `_restore_session_model()` invoked from both `_init_agent` and `_preload_resumed_session` (dashboard preload). |
| **2. Claude always → Vertex (ADC)** | `api_server.py`, `server.py`, `cli.py` via `hussh_one_router._vertex_claude_runtime()` | Any restored `claude-*` model is re-pinned to `provider=google-vertex-claude` (`api_key=gcp-sdk`, `api_mode=anthropic_messages`), never Anthropic-direct OAuth. Kills the 400 retry storm at the source. |
| **3. Compact before the ceiling** | `config.yaml` (seeded by bootstrap) | `compression.threshold 0.50 → 0.35`, `hygiene_hard_message_limit 400 → 250` so sessions compact long before they reach the memory ceiling. |
| **4. Graceful restart, never SIGKILL** | `scripts/hussh-one-supervisor.sh` | The watchdog polls the dashboard **process-tree RSS** each interval and `SIGTERM`s at a soft cap (`HUSSH_ONE_DASHBOARD_MEM_CAP_MB`, default `6144`) for a clean, logged restart **before** the kernel can `SIGKILL` it mid-write. `ps`-based, no `psutil` dependency, macOS + Linux safe. |

Layers 1–2 remove the *cause*; layers 3–4 are belt-and-suspenders so even an
unrelated runaway can't hard-kill the dashboard.

---

## Config knobs

| Knob | Default | Purpose |
|------|---------|---------|
| `compression.threshold` | `0.35` | Compact when context usage exceeds this ratio of the model window. |
| `compression.hygiene_hard_message_limit` | `250` | Force-compress once a session exceeds this message count. |
| `HUSSH_ONE_DASHBOARD_MEM_CAP_MB` (env) | `6144` | Soft RSS cap for the dashboard process tree. `0` disables the monitor. Lower it on small-RAM boxes. |
| `HUSSH_ONE_DASHBOARD_WATCHDOG_INTERVAL` (env) | `5` | Seconds between liveness + memory polls. |

> **Important:** the compression values are seeded by `set_config_defaults()` in
> `scripts/hussh-one-bootstrap.sh`, **not** baked into the code `DEFAULT_CONFIG`
> (`hermes_cli/config.py` still ships upstream's `0.50` / `400`). Always run the
> bootstrap after a fresh clone so a new machine inherits the OOM-safe values —
> see [Machine transfer](#machine-transfer-pulling-on-a-new-box).

---

## Verification

```bash
# Resume-model + Vertex routing fix
python -m pytest tests/cli/test_resume_model_restore.py -q          # 23 tests

# Supervisor + bootstrap scripts still parse and behave
bash -n scripts/hussh-one-supervisor.sh
bash -n scripts/hussh-one-bootstrap.sh
python -m pytest tests/scripts/test_hussh_one_scripts.py -q

# Watch the soft cap fire (logs a SIGTERM, not an rc=-9)
grep "mem cap hit" ~/.hermes/logs/hussh-one-dashboard.error.log
```

A clean restart now logs `child exited rc=-15` (SIGTERM) instead of `rc=-9`
(SIGKILL). Seeing `rc=-15` after a `mem cap hit` line is the system working as
intended.

---

## Machine transfer (pulling on a new box)

Git carries the **code**; it does **not** carry your local state. After
`git pull`, run the bootstrap and supply the machine-local pieces:

```bash
git pull origin main
./scripts/hussh-one-bootstrap.sh        # seeds OOM-safe compression + brand/skin/model
```

| Item | Where | Action on the new machine |
|------|-------|---------------------------|
| API keys / secrets | `~/.hermes/.env` | Copy over (git-ignored, never committed) |
| GCP ADC (Vertex Claude) | `~/.config/gcloud/` | `gcloud auth application-default login` |
| Compression tuning | `~/.hermes/config.yaml` | Applied automatically by bootstrap |
| Memory cap override | env | Optional: `export HUSSH_ONE_DASHBOARD_MEM_CAP_MB=4096` on small-RAM boxes |
| Python venv | `.venv/` | `python -m venv .venv && pip install -e .` |
| Session history | `~/.hermes/state.db` | Machine-local; does not transfer (expected) |

---

## See also
- [Feature — Session-Model Persistence & Resume](../features/session-model-resume.md)
- [Operations Runbook](./README.md)
- [Upgrading from upstream](./upgrading.md)
- [Contracts](../contracts/README.md) — invariants **H** and **I**
