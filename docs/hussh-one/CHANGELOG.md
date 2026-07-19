# 🤫 Hussh One — Changelog

The **single canonical, dated, categorized history** of everything Hussh One adds on top of
upstream Hermes Agent. If a Hussh-One-specific commit lands and it isn't listed here, this
file is stale — see [Keeping this file current](#keeping-this-file-current) below.

This is the **narrative** companion to the [feature catalog](./features/README.md) (what's
shipped, by capability) and the [contracts index](./contracts/README.md) (what's invariant,
machine-checkable). Use this page when you need to answer *"when did we add X, and why"*.

> **Fork provenance:** `hushh-labs/hussh-one-hermes` (remote `origin`), tracking
> `NousResearch/hermes-agent` (remote `upstream`). Last reconciled with upstream at merge-base
> commit `6459b3d99` (2026-06-07). See [Upstream sync state](#upstream-sync-state).

---

## ⚖️ Distribution & Compliance

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-18 | `634e396f2` | **Apache-2.0 Hussh One distribution with MIT upstream preservation.** Replaced the root distribution license with Apache-2.0, preserved Nous Research's unmodified MIT grant, added NOTICE and nested-component attribution, applied SPDX headers to Hussh-added source, and added CI/guard enforcement plus package-artifact verification for the `Apache-2.0 AND MIT` expression. |

---

## 🛰️ WhatsApp Gateway & Bridge
The primary Hussh One surface — a self-chat WhatsApp number driving the agent via a Node.js
Baileys bridge, with owner-only triggering and sandboxed social-group capsules.

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-17 | `6346c137a` | Corrected the supervisor's WhatsApp health/status default from the obsolete `:3000` to the bridge's canonical `:8473`, so it now monitors the live connected service. |
| 2026-07-17 | `08d8eef31` | **Self-chat JID/LID bridge fix.** Pure, Node-tested incoming self-chat classification now accepts the owner's linked-device identifiers without weakening capsule triggers, isolated memory, or the gateway's single canonical outgoing header. |
| 2026-07-12 | `daaaa3070` | **Group Intelligence onboarding** — self-chat auto-all-users mode, debounced bridge watchdog, cron/scheduler + `send_message` tool support for the new flow. |
| 2026-07-08 | `2faade147` | **Per-capsule dedicated trigger handles.** Each capsule (e.g. "One Team") now scopes to its OWN `@`-handle via `whatsapp.capsules.<jid>.trigger_tokens`, forwarded to the bridge as `WHATSAPP_GROUP_TRIGGER_TOKENS`. Fixes cross-talk where any global `@One`/`@husshOne` tag — or a native @-mention of the owner's own number in self-chat mode — incorrectly woke every capsule group. |
| 2026-06-25 | `1904dd513` | Count-capped WhatsApp session prune (`MAX_PER_FAMILY`) — bounds Baileys session-dir bloat. |
| 2026-06-25 | `75a14bcf9` | Moved WhatsApp bridge to a dedicated port **8473** (off 3000) — avoids clashing with dev servers. |
| 2026-06-20 | `a9ceda337` | Stabilized the Baileys bridge reconnect loop; persist the real (not stale) session model across bridge restarts. |
| 2026-06-11 | `3dc7c05de` | Centralized the canonical 3-line stacked header (`brand / model+mode / divider`) and aligned all gateway tests to it. |
| 2026-06-10 | `e71c361bb` | Periodic health-check self-healing watchdog — detects and recovers a zombie bridge process automatically. |
| 2026-06-07 | `c8b3fefa3` | **Brand floor** for proactive sends (`send_message` tool, cron auto-delivery, restart/shutdown notices) — these bypass the inbound-reply header composer, so `_ensure_brand_floor()` guarantees they still carry the header. |
| 2026-06-07 | `0cac42ff1` | Bridge's own `WHATSAPP_REPLY_PREFIX` forced empty — the **Python gateway is the sole header composer**, never the Node bridge, eliminating a double-prefix race. |
| 2026-06-07 | `32faf3a75` | Recursive contaminated-header stripping — a model that echoes a stale header from its own context is caught and stripped before the real header is prepended (root cause of "double header" reports). |
| 2026-06-04 | `7265cb52b` | **Capsule sandbox v1** — sandboxed social-group containers: isolated memory, read-only toolset, no lateral send, non-owner `@One` triggering with anti-DOS rate limit. |
| 2026-06-04 | `ce0d9e905` | Unified group & DM triggering rules; strict `@One`/`@husshOne`/`@hussh-one` tagging enforced everywhere except capsules. |
| 2026-05-30 | `7c00df57c` | `WHATSAPP_REPLY_PREFIX` env now correctly takes priority over `config.yaml`'s `reply_prefix` (operator emergency override contract). |
| 2026-05-30 | `e03a3f37a` | Programmatic enforcement of the owner's WhatsApp-only-model + underline layout preference. |

📄 Feature pages: [whatsapp-branding-header.md](./features/whatsapp-branding-header.md) ·
[whatsapp-capsules.md](./features/whatsapp-capsules.md) ·
[triggering-and-security.md](./features/triggering-and-security.md) ·
[whatsapp-local-data.md](./features/whatsapp-local-data.md) ·
[multi-device-auth.md](./features/multi-device-auth.md)

---

## 🧠 Vertex Claude / GCP ADC Model Routing
Claude models MUST resolve through Google Vertex AI via Application Default Credentials
(ADC) — never Anthropic-direct — to avoid the claude.ai-billing 400 and keep billing on the
GCP project, not a personal subscription token.

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-14 | `d3f9f111f` | **Gemini 3.1 Pro Preview onboarded** end-to-end: native `gemini` provider, `google-gemini-cli`, Vertex ADC (Copilot BYOK), and adaptive thinking (`low`/`medium`/`high`) extended to the full Gemini 3/3.1/3.5 family (was Pro-only `low`/`high`, Flash-only `low`/`medium`/`high` — now unified). Natural-language switch ("switch to gemini 3.1", "gemini 3.1 pro") added to `natural_model_switch.py`. Context window: 2,097,152 in / 65,536 out (live-probed). |
| 2026-07-07 | `a717f82fb` | Vertex ADC context-window corrections; fixed a bare `SILENT` token leaking to user chat. |
| 2026-07-07 | `f32dce090` | Live-probed: `claude-sonnet-4-6` output cap is **128k**, not 64k. |
| 2026-06-26 | `3c6a06b5a` | Streaming fallbacks to kill a mid-stream Vertex `429` cleanly instead of hanging. |
| 2026-06-26 | `c17eac4a5` | Multi-region Gemini pool + retry/cooldown so parallel agents don't exhaust a single region's quota. |
| 2026-06-21 | `f0bc40723` | Stopped session-model revert + the OOM crash loop it triggered (see [crash-resilience.md](./operations/crash-resilience.md)). |
| 2026-06-16 | `d2fe7e9e3` | Replaced a crashing async LLM classifier in the workload router with a zero-latency, confidence-scored rule engine. |
| 2026-06-16 | `eee075067` | Default Vertex AI location fallback → `us-central1` (was `global`) to prevent 404s. |
| 2026-06-08 | `a6a2704b6` | **Session-model persistence** — chosen model survives gateway restarts and resumes (see dedicated feature below). |
| 2026-05-31 | `ac036d19a` / `bc25071f2` | Hardened Claude runtime adapter selection; live access-check verification before switching. |

📄 Feature pages: [session-model-resume.md](./features/session-model-resume.md) ·
[model-switching.md](./features/model-switching.md) ·
[crash-resilience.md](./operations/crash-resilience.md)

---

## 🧩 VS Code Copilot BYOK (Vertex ADC)
Native VS Code Copilot Custom Endpoints backed by the same Vertex ADC stack — chat, inline
edit, `@workspace`, and agent-mode tool calling, without a third-party extension or pasted
Google/Vertex API keys. The setup generates a separate local-only bearer key for VS Code to
authenticate to the loopback auth shim; Vertex requests still use ADC.

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-16 | `41c4ef2ca` | Accept blank or absent Copilot bearer headers only through the loopback-bound compatibility shim, restoring affected VS Code custom endpoints without exposing the proxy remotely. |
| 2026-07-16 | `100b7e4e6` | Completed the headerless VS Code BYOK fallback path through the local LiteLLM auth shim. |
| 2026-07-16 | `7ce5060b9` | Corrected VS Code custom-endpoint authentication to use provider-level credentials, preventing malformed/missing Authorization headers. |
| 2026-07-16 | `8ba7fb3cf` | Auto-configured Copilot's local loopback authentication during Hussh One BYOK onboarding. |
| 2026-07-15 | `bdb8cd3a8` | Tightened BYOK model steering and fallback behavior so Copilot recovers predictably instead of silently switching away from the configured Vertex route. |
| 2026-07-15 | `f32364846` | Documented the boundary between the local Copilot loopback key and Vertex ADC: the former authenticates only the local shim; model access remains ADC-based. |
| 2026-07-14 | `pending` | **3.1-pro 429 recovery + search-tool mapping verified.** `gemini-3.1-pro-preview` is GLOBAL-ONLY on Vertex (live region probe: all 18 non-global regions 404), so a mid-stream 429 falling back to itself hit the just-cooled-down deployment → "No deployments available" → Copilot rendered the dead stream as a broken tool call. Fixed by chaining its `fallbacks` to the multi-region `gemini-3.5-flash` pool. Separately **verified (not assumed)** that Copilot's real native search tools map correctly onto every Vertex ADC model: replayed the actual captured 86-tool Copilot manifest (incl. `file_search`, `grep_search`, `github_text_search`, `vscode_listCodeUsages`, MCP `*_search`) through the shim — all `type:object`, zero composition keywords, and every model (opus-4-8, sonnet-4-6, sonnet-5, gemini-3.5-flash, gemini-3.1-pro-preview) returned `finish=tool_calls` with well-formed search calls. Search was never broken — the symptom was the 3.1-pro 429 dead-end. |
| 2026-07-14 | `pending` | **Smart-tool-usage steering for BYOK agent mode.** BYOK models (Vertex Claude/Gemini) aren't tuned for Copilot's tool ecosystem, so in agent mode they over-spawned subagents for routine work and shelled out `cat`/`sed`/`grep` instead of using native read/edit/search tools. `hussh-one-copilot-setup.sh` now ships an always-on user-level custom instructions file (`~/.copilot/instructions/hussh-one-tooling.instructions.md`, `applyTo: '**'`, highest instruction priority) that steers every agent/model to native-tool-first behavior and against needless delegation. Added `hussh-one-doctor.sh` guard (`check_copilot_byok`) so the steering file can't silently regress. |
| 2026-07-14 | `b31cfdc9c` | Corrected the previous entry's unverified claim of a confirmed Gemini root-`anyOf` production bug: live traffic capture of Copilot's real 86-tool manifest (native built-ins + all MCP tools) shows zero root-level composition keywords and a clean 200 OK against both Gemini models — the theory did not hold up. Added a temporary env-gated debug capture tool to `litellm_auth_shim.py` (`HUSSH_SHIM_CAPTURE_TOOLS=1`, off by default) for future ground-truth investigations. The sanitizer/tests from the prior commit remain as a no-op safety net. |
| 2026-07-14 | `2b198a1c1` | Filled in the pending commit hash placeholder in the changelog entry below, after push. |
| 2026-07-14 | `d3f9f111f` | **Gemini 3.1 Pro Preview added to the BYOK model list**; graceful onboarding hardening — every future model/config add now goes through validate→backup→atomic-swap→smoke-test→auto-rollback (a bad model entry can no longer leave Copilot's proxy dead), plus a **native tool-calling gate** (two schema shapes: property-level and root-level `anyOf`, both run against every registered model on every setup). Added a defense-in-depth Gemini tool-schema sanitizer (`_scrub_tools_for_gemini` in `litellm_auth_shim.py`) for a theoretical Vertex incompatibility (root-level `anyOf`/`oneOf`/`allOf` in tool schemas) — **later verified via live traffic capture that Copilot's real 86-tool manifest (native built-ins + all MCP tools) contains no such shape and already works correctly against Gemini without it; see the correction in `scripts/copilot-byok/README.md`.** The sanitizer and its tests remain as a no-op safety net. Auth-shim debug logging redacted (no more raw bearer/header dumps to disk). |
| 2026-07-07 | `3ec880250` | Scrub LiteLLM's empty-content placeholder from Claude transcripts (Copilot compatibility fix). |
| 2026-07-04 | `67749df95` | Embedded the literal shim key directly in the VS Code BYOK config + added a doctor guard for drift. |
| 2026-06-26 | `962c9cfee` | Keep the PTY/WS session alive when the dashboard tab is backgrounded/minimized (adjacent reliability fix landed same wave). |
| 2026-06-25 | `82a7d3e69` | Shipped a custom VS Code Plan agent (`send:false`) via the Copilot setup script. |
| 2026-06-24 | `b814d3414` | Graceful Copilot BYOK recovery — an invisible proxy death (OOM/crash) is now a sub-second hiccup, not a lost turn. |
| 2026-06-24 | `5f51d848a` | **Native VS Code Copilot BYOK onboarding** — `scripts/hussh-one-copilot-setup.sh`, LiteLLM proxy (`:8643`) + auth shim (`:8644`). |

📄 Deep-dive: [`scripts/copilot-byok/README.md`](../../scripts/copilot-byok/README.md)

---

## 🌐 Open WebUI (Browser Chat Variant)
The third first-class Hussh One surface (alongside WhatsApp and TUI/dashboard) — a full
browser chat UI talking to Hermes' OpenAI-compatible API server.

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-06-24 | `462b804ec` | Render the Features catalog inline in the chat body; dropped the standalone pipe-file approach. |
| 2026-06-22 | `703b4d3f9` | In-app Features page via an upgrade-safe Open WebUI Pipe Function. |
| 2026-06-17 | `49e83fdf5` | Polished streaming UX — clean reasoning, ADK-style tool-activity status lines. |
| 2026-06-17 | `d4b318660` | Stream reasoning tokens + lifecycle status events to Open WebUI over SSE. |
| 2026-06-11 | `78372eea9` | Baked Hussh One performance + open-access defaults into the setup generator (title/tag generation OFF by default → 1 agent call/message). |

📄 Feature page: [open-webui.md](./features/open-webui.md)

---

## 🖥️ Dashboard / TUI Reliability
The web dashboard embeds the real TUI over a PTY bridge — these commits keep that surface
alive across network blips, backgrounded tabs, and gateway restarts.

| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-18 | `41eae5c88` | **Single-owner dashboard and quiet self-chat doctor.** macOS launchd now owns one versioned watchdog (which owns the `:9119` child) instead of racing a detached watchdog. The managed no-agent doctor persists alert state, reports only new/recovered failures (six-hour unresolved reminder), and silently prunes only regenerable WhatsApp session files. |
| 2026-07-10 | `be100c2f1` | Eliminated dashboard chat freezes caused by stale PTY/session state and added deterministic doctor diagnostics for the two failure modes that previously required a manual browser refresh. |
| 2026-06-24 | `7de58e588` | Full Ink cache evict on session reset — stops layout mismatch after `/new`. |
| 2026-06-24 | `936bb7768` | Restore all live sessions on gateway restart. |
| 2026-06-24 | `87649d682` | Made the Chat events-feed WebSocket resilient (reconnect + heartbeat). |
| 2026-06-22 | `2a6233ac4` | Added a Features page to the dashboard sidebar (Hussh One feature catalog, live). |
| 2026-06-22 | `613a0fbdf` | TUI model popover now syncs to the live session model instead of a stale snapshot. |
| 2026-06-19 | `3f6201330` | Self-healing session resume + resilient WebSocket auto-reconnect in `tui_gateway`. |
| 2026-06-07 | `e8a04cdb6` | Restored the tool-call panel; hardened model switch. |

📄 Feature pages: [theming.md](./features/theming.md) ·
[../operations/crash-resilience.md](./operations/crash-resilience.md)

---

## 🔀 Natural-Language Model Switching & Cron
| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-06-24 | `e374e89e5` | Per-job `max_iterations` + graceful iteration-cap delivery for cron. |
| 2026-05-30 | `fa2ffdd19` | `/model auto\|reset\|clear\|default` restores auto-routing from any messaging platform. |
| 2026-05-30 | `f1b639fd4` | Natural-language model switching ("switch to opus 4.8") without slash-command syntax. |

📄 Feature page: [model-switching.md](./features/model-switching.md)

---

## 🚀 Onboarding, Bootstrap, Doctor & Deployment
| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-17 | `040df65d3` | **Managed Open WebUI endpoint discovery.** Supervisor and doctor now read the persisted companion endpoint (or a legacy launcher) before health-checking, so a valid non-default loopback port is monitored and restarted instead of an unrelated process on `:8080`. |
| 2026-07-17 | `152b1d655` | **Companion services self-heal by default.** Bootstrap now provisions VS Code BYOK only when a supported editor plus Vertex ADC are available, starts the loopback blank-bearer compatibility shim, and installs branded Open WebUI against this checkout's Hermes binary and `HERMES_HOME`. Supervisor/doctor now health-check and restart Open WebUI; stale Google Ads/ADK injected branding was removed. |
| 2026-06-27 | `1810e8836` | Bootstrap now sets robust platform-specific config defaults. |
| 2026-06-27 | `4ee548e7c` | Updated onboarding evolution docs + wiki-link best practices. |
| 2026-06-21 | `a749903d5` | Seeded OOM-safe compression defaults (`compression.threshold=0.35`, hygiene message limit) in bootstrap. |
| 2026-06-15 | `107fba3d6` | Abstracted dual-platform (WhatsApp/desktop) onboarding guidelines. |
| 2026-06-17 | `1aea01fe3` | Clarified local-mode routing mapping for Gemma 4 and Qwen. |
| 2026-06-03 | `8f4bb599a` | Bootstrap/doctor configure reasoning effort; venv symlink ignored. |
| 2026-06-03 | `3e54b19e8` | Install scripts + defaults repointed at the `hushh-labs` fork and `main` branch. |
| 2026-05-31 | `4030d182d` | Hardened the clone deployment flow end-to-end. |
| 2026-05-30 | `98bae6edf` | Dashboard chat TUI diagnostics surfaced (debugging aid for embedded-TUI regressions). |
| 2026-05-30 | `0917cadd0` | `custom:` providers now accept model names containing slashes. |
| 2026-05-29 | `7380102ba` | Converged the project's pre-Hermes predecessor codebase into hermes-agent — added the original cognitive abstractions & machine-readable specification (`HUSSH_ONE.md`). |

📄 Operations runbook: [operations/README.md](./operations/README.md) ·
[operations/upgrading.md](./operations/upgrading.md)

---

## 🎨 Branding & Header Infrastructure
| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-17 | `de8b243f0` | **Canonical runtime identity.** WhatsApp, TUI, and dashboard now show `Model · Vertex ADC · [A/S]` from one safe identity module. `[A]` remains router-driven (including Claude escalation); `[S]` is persisted only after an explicit model switch. Also corrected the Claude Opus display regression and dashboard terminal foreground propagation. |
| 2026-06-15 | `71d50dfc1` | Repaired model header accuracy end-to-end (precise version/size naming). |
| 2026-06-07 | `4f5eb8d8e` | Default prefix made emoji-first: **"🤫 Hussh One"**. |
| 2026-06-03 | `77c62b0d5` | Extracted the WhatsApp header into the upgrade-safe `hermes_cli/hussh_one_header.py` module + MCP onboarding scanner. |
| 2026-05-30 | `824877edc` | Added branding + Vertex Claude guardrails (the overlay pattern's first hard boundary). |

📄 Architecture: [architecture/README.md](./architecture/README.md) (the overlay model) ·
Brand story: [overview/brand.md](./overview/brand.md)

---

## 📚 Documentation Infrastructure
| Date | Commit | What shipped |
|------|--------|---------------|
| 2026-07-18 | `e8fb123b9` | Changelog coverage now includes every `scripts/hussh-one-*` path through an explicit Git glob, so Hussh operational changes cannot silently evade the freshness check. |
| 2026-07-18 | `703c932ce` | Clean clones without an optional `upstream` remote now use the recorded fork base for changelog audit scope rather than falsely treating inherited Hermes history as undocumented Hussh work. |
| 2026-07-18 | `94acedb7a` | Documented the single-owner macOS dashboard restart path and the managed quiet-doctor alert, recovery, state-file, and safe session-cleanup operating procedures. |
| 2026-07-14 | `233ee0aae` | Backfilled correction-commit context in this changelog after the July Copilot verification work. |
| 2026-07-08 | `7da566c3c` | **This changelog + its self-checking freshness guard shipped** (`scripts/hussh-one-changelog-check.py`), wired into `hussh-one-health-index.py` and `hussh-one-doctor.sh`. |
| 2026-07-07 | `635c0e7e7` | Synced onboarding + docs with live-probed Vertex context windows and cron `[SILENT]` conventions. |
| 2026-06-22 | `d368c5db6` / `613a0fbdf` | Documented session-model resume, dashboard crash resilience, and the Open WebUI variant. |
| 2026-06-05 | `29aaf420b` | **Scaffolded this entire `docs/hussh-one/` nested documentation tree** (overview / architecture / features / operations / contracts / roadmap). |

---

## 🔗 Upstream Sync Points
Full merges of `upstream/main` into `main`, preserving the overlay:

| Date | Commit | Notes |
|------|--------|-------|
| 2026-07-15 | `12b402353` | Integrated upstream Hermes Agent **v0.18.2** into Hussh One. |
| 2026-06-07 | `42f39a52b` | **Trunk reconciliation** — merged the drifted `hussh-one-hermes` branch (9 unique features: capsules, upgrade-safe header module, bootstrap/doctor/supervisor scripts) into `main`; `main` became the sole canonical trunk; old branch deleted, preserved at tag `safety/hussh-one-hermes-20260607-232148`. |
| 2026-06-07 | `6a8f537e3` | Merged 71 upstream commits into `main`. |
| 2026-06-07 | `34b003a11` | Merge `upstream/main`. |
| 2026-05-30 | `a68251135` / `27b081d8f` | Merge `upstream/main` (x2, same day). |

---

## Upstream Sync State
Last full reconciliation: **2026-06-07** (merge-base `6459b3d99`).

```bash
# Check current drift (run anytime):
git fetch upstream --quiet
git rev-list --left-right --count HEAD...upstream/main
#   left  = our commits ahead of upstream (Hussh One work)
#   right = upstream commits we haven't merged yet
```

As of **2026-07-08**: upstream is **3,965 commits ahead** (last synced ~6 weeks ago).
Notably, upstream **relocated `gateway/platforms/whatsapp.py` → `plugins/platforms/whatsapp/adapter.py`**
as part of a plugin-migration refactor — the next merge must re-home our WhatsApp
customizations (capsule triggering, brand floor, header composition) into the new location.
See [`fork-upstream-merge-maintenance`](../../../docs/hussh-one-upstream-maintenance.md) for
the conflict-resolution playbook before attempting this merge.

---

## Keeping this file current

**Rule: every commit that touches a Hussh-One-only surface gets a row here, same day.**
Hussh-One-only surfaces: `hermes_cli/hussh_one_header.py`, `hermes_cli/brand.py`,
`gateway/whatsapp_capsule.py`, `scripts/whatsapp-bridge/bridge.js`,
`scripts/hussh-one-*.sh`, `scripts/copilot-byok/`, `scripts/open-webui/`,
`scripts/setup_open_webui.sh`, `plugins/model-providers/google-vertex-claude/`,
`hermes_cli/hussh_one_identity.py`, `hermes_cli/hussh_one_router.py`,
`hermes_cli/hussh_one_mcp_scan.py`, `docs/hussh-one/`, `HUSSH_ONE.md`.

Check for undocumented commits any time:
```bash
python3 scripts/hussh-one-changelog-check.py
```
This prints any commit since the last hash recorded in this file's git blame that touches a
Hussh-One-only path and has NOT been mentioned in this changelog. **Zero output = current.**

This check is also wired into `scripts/hussh-one-health-index.py` (`changelog` harness) and
should be run as part of onboarding a new machine/session — see
[operations/README.md](./operations/README.md#onboarding-checklist).

### How to add an entry
1. Pick (or add) the right theme section above — group by *surface*, not by date.
2. One row: `| Date | short-sha | one-line, specific, no-jargon description |`.
3. If the commit ships a brand-new capability (not a fix to an existing one), also:
   - add a row to the [feature catalog](./features/README.md),
   - add/extend the page under [`features/`](./features/),
   - add/extend the invariant in [contracts](./contracts/README.md).
4. Re-run `python3 scripts/hussh-one-changelog-check.py` — must report clean.
