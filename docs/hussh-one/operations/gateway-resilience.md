# Gateway Resilience — Why Hermes Doesn't Stay Offline

How the `ai.hermes.gateway` process recovers from crashes and restarts, what
already guarantees it comes back up, and what two separate investigations
found: a 2026-08-21 restart initially reported as "the gateway completely
stopped," and a 2026-08-23 incident where the gateway really was unable to
come back up.

> TL;DR — The gateway has two independent, working self-healing layers:
> launchd's `KeepAlive` (OS-level, restarts on *any* exit, typically within
> seconds) and a 15-minute doctor cron (catches the case launchd alone can't
> fix). Across 216 recorded stop events over ~3 months, median recovery is
> **2 seconds**; worst-case crash recovery is **31 seconds**, bounded by
> design. The 2026-08-21 22:14 stop was a deliberate `hermes gateway restart`
> and recovered in 31 seconds as designed — see
> [Incident 1](#incident-1-2026-08-21--a-deliberate-restart-misread-as-an-outage).
> **But on 2026-08-23 the gateway genuinely could not have come back up if it
> had crashed**: the production checkout was left mid-merge with an
> unresolved conflict marker, which broke `hermes_cli.main` for any fresh
> process — the exact scenario both self-healing layers depend on working.
> See [Incident 2](#incident-2-2026-08-23--a-broken-working-tree-disabled-both-recovery-layers),
> which is the one that actually matches "the gateway is still not up." Both
> incidents are resolved and a regression guard now runs in
> `scripts/hussh-one-doctor.sh` — see
> [Findings & prevention](#findings--prevention).

---

## Architecture: two independent recovery layers

| Layer | Where | What it does |
|-------|-------|---------------|
| **1. launchd `KeepAlive`** | `~/Library/LaunchAgents/ai.hermes.gateway.plist` | `KeepAlive` is the unconditional boolean form (`<true/>`), not the `{SuccessfulExit: false}` dict form — so launchd restarts the gateway on **any** exit, clean or crashed. `ThrottleInterval=30` stops a crash-looping process from hammering respawns faster than once per 30s. `ExitTimeOut=25` gives a graceful `SIGTERM`→drain window before launchd escalates to `SIGKILL`. `RunAtLoad=true` also brings it up on login/reboot. This is the primary, OS-level defense and requires no agent or script to be running. |
| **2. "Hussh One Self-Healing Doctor" cron** | `~/.hermes/scripts/hussh_one_doctor_heal.py`, cron job id `594d722646a1`, every 15 minutes, `no_agent: true` | A deterministic Python script (installed by `hussh-one-bootstrap.sh`, **not** hand-edited) that checks each label in `CORE_SERVICES` (including `ai.hermes.gateway`) via `launchctl print`, and for anything not `running`, issues `launchctl kickstart -k`. This is the backstop for the case layer 1 can't fix on its own: a launchd job that has gone **`not-loaded`** (unloaded, e.g. after a `launchctl bootout` or a plist edit) rather than merely crashed — `kickstart` can't revive a job that isn't loaded, so the doctor reports that as an unresolved finding via self-chat instead of silently failing. It also runs `hussh-one-health-index.py`, heals bloated WhatsApp sessions on a 24h cooldown, and rate-limits its own alerts to at most one reminder per 6 hours per unresolved issue (`~/.hermes/health/hussh-one-doctor-alert-state.json`). |

Layer 1 handles the overwhelming majority of exits without anyone or anything
noticing. Layer 2 exists for the failure mode layer 1 structurally cannot
cover — the job itself being unloaded — and to give the owner a self-chat
alert if a service still won't come back after a kickstart attempt.

Both were confirmed live during this audit: `launchctl print
gui/$(id -u)/ai.hermes.gateway` shows `state = running`, `pid` matching the
actual gateway process, and `runs = 41` (no drift between launchd's view and
reality); the doctor cron shows `last_status: ok` with **4,235** completed
runs.

---

## Evidence: three months of stop/restart events

`~/.hermes/logs/gateway-exit-diag.log` records every gateway start and stop as
structured JSON. Pairing each of the 216 recorded stops with the next start
gives:

- **Median downtime: 2 seconds.**
- **Crash recovery (`exit_nonzero`) is 0–31 seconds in every case**, which
  lines up exactly with `ThrottleInterval=30` — launchd is rate-limiting the
  respawn, not failing to respawn.
- **24 gaps exceed 5 minutes**, some as long as tens of hours — but every one
  of these is preceded by `exit_clean`, not a crash. These are the machine
  sleeping or being deliberately shut down overnight (a laptop, not a
  server), not a resilience failure — nothing was "stuck down" waiting to be
  healed.

## Incident 1: 2026-08-21 — a deliberate restart misread as an outage

```
22:14:26.704Z  asyncio.run.returned  pid=94506  success=false
22:14:26.705Z  gateway.exit_nonzero  pid=94506
22:14:57.208Z  gateway.start         pid=40492  replace=true
```

This is a **31-second, launchd-recovered restart**, and it is directly
attributable to a `hermes_cli.main gateway restart` invocation run earlier in
that session to apply a `GOOGLE_CLOUD_PROJECT` config change (unblocking
Hermes from the Vertex billing deny). It is not an unexplained crash:

- No corresponding kill event in `~/.hermes/logs/tui_gateway_crash.log` near
  that timestamp, which rules out a TUI-triggered `SIGHUP`.
- The timing matches the manual restart command exactly.
- Recovery time (31s) matches the `ThrottleInterval` ceiling, confirming
  layer 1 handled it normally.

**Conclusion: the system worked as designed.** The "gateway completely
stopped" the owner observed mid-session was the ~31-second window between the
old process exiting and launchd bringing the new one up — visible from inside
an active WhatsApp/Feishu session as a momentary disconnect, not an outage
that needed manual intervention.

---

## Incident 2: 2026-08-23 — a broken working tree disabled both recovery layers

This is the real one. The gateway process (pid 40492, running since the
2026-08-21 restart above) was still technically alive and answering
`/health` — but two things were silently wrong underneath it, and either one
alone was enough to make the system feel offline even though the process
never exited:

1. **Chat completions were 403ing.** The default text model
   (`gemini-3.7-flash`, via `GEMINI_API_KEY`/`GOOGLE_API_KEY` in
   `~/.hermes/.env`) was still using a Gemini Developer API key bound to
   `hushh-pda-uat` (project `745506018753`) — the project under the Lightning
   billing dunning deny. Every real message got
   `PERMISSION_DENIED: Lightning dunning decision is deny` (18 occurrences in
   `gateway.log`). The process was up; it just couldn't answer anything. Only
   the live-voice key (`hussh-one-live-31-bridge`, minted earlier on the
   personal-billing bridge project for the Gemini 3.1 Flash Live migration)
   had been moved off the blocked project — the general-purpose key never
   was. That bridge key is restricted at the *service* level
   (`generativelanguage.googleapis.com`), not to a specific model, so it
   works for `gemini-3.7-flash` too — confirmed with a direct
   `generateContent` call before rewiring `.env`.
2. **A fresh restart could not have succeeded.** The production checkout
   (`~/Documents/GitHub/hussh-one-hermes-agent`, the exact `WorkingDirectory`
   the launchd plist runs from) was sitting mid-`git merge` — `upstream/main`
   into a throwaway `sync/upstream-20260823` branch, started ~21:24 that day
   and abandoned with an unresolved `<<<<<<< HEAD` conflict marker in
   `hermes_cli/providers.py`. That's a `SyntaxError` at import time. Running
   `hermes gateway restart` reproduced it immediately:
   `SyntaxError: invalid syntax` on `<<<<<<< HEAD`. The already-running old
   process didn't care (it had the module in memory from before the merge
   started), but **if it had crashed for any reason, neither launchd's
   `KeepAlive` nor the 15-minute doctor cron could have brought it back** —
   both just re-run the same broken entrypoint. The self-healing
   architecture in this document was, at that moment, completely disabled.

**Fix, applied end to end:**

- Minted-key reuse verified live (`curl` a `generateContent` call, HTTP 200,
  "PONG" back) before touching config.
- `~/.hermes/.env`'s `GOOGLE_API_KEY` and `GEMINI_API_KEY` repointed to the
  bridge-project key (old value backed up first).
- The merge was confirmed safe to abandon — `sync/upstream-20260823`'s
  pre-merge tip was byte-identical to `main`'s tip (branched moments before
  the merge attempt, zero Hussh commits ahead), so `git merge --abort` lost
  no committed work, only the ~3-hour-old, never-committed conflict
  resolution in progress. Checked out back to `main` (the only canonical
  runtime branch per this doc's [Operations Runbook](./README.md)).
- `hermes gateway restart` now succeeds; verified `hermes_cli.main` imports
  cleanly, launchd shows a fresh `running` pid, and a real
  `/v1/chat/completions` call through the gateway (not the raw Gemini API)
  returned a genuine model response.
- A regression guard was added to `scripts/hussh-one-doctor.sh` (see below)
  so this class of failure surfaces on the next health check instead of the
  next crash.

**Root cause of the merge being there at all:** something — most likely an
automated or manual upstream-sync attempt — ran `git merge upstream/main`
directly inside the live production checkout instead of the isolated
worktree this repo's own [canonical branch and clone
contract](./README.md#canonical-branch-and-clone-contract) prescribes, hit
conflicts, and left the tree in that state. The contract already says sync
branches are temporary and upstream comparisons belong in a detached
worktree (`git worktree add --detach ../hermes-stock upstream/main`) — this
incident is exactly the failure mode that rule exists to prevent, and it was
not followed.

---

## Findings & prevention

Four issues surfaced during this audit — the working-tree/API-key pair from
[Incident 2](#incident-2-2026-08-23--a-broken-working-tree-disabled-both-recovery-layers)
that actually caused the outage, plus three smaller ones from the initial
2026-08-21 audit:

0. **Both root causes of Incident 2 are now guarded against automatically.**
   `scripts/hussh-one-doctor.sh` gained two checks, run first, before
   anything else: `check_working_tree_mergeable` fails loudly if
   `.git/MERGE_HEAD` exists or any tracked `.py` file has an unresolved
   `<<<<<<<` marker, and `check_cli_importable` actually runs
   `python -c "import hermes_cli.main"` in the production checkout and fails
   if it doesn't — i.e. it answers "could the gateway restart right now if it
   had to?" instead of assuming yes because the old process is still up. Both
   were verified against a reproduction of the exact incident (re-injecting
   the conflict marker) before being reverted. There is no equivalent
   automated check yet for a Developer API key silently pointing at a billing
   account under a dunning deny — that class of failure only surfaces as
   repeated 403s in `gateway.log`; consider extending `check_vertex_profile`
   in the same script to also probe the `GEMINI_API_KEY` currently in
   `~/.hermes/.env` with a cheap `generateContent` call.
1. **`gateway-restart.log` doesn't capture this class of restart.** It only
   has one entry (2026-08-04) despite 216 stop events in the same window —
   it logs explicit `hermes gateway restart` CLI invocations, not
   launchd-driven crash respawns. `gateway-exit-diag.log` is the complete
   record; treat it, not `gateway-restart.log`, as authoritative when
   investigating a future incident.
2. **A stale second copy of the doctor script exists** at
   `~/.hermes/skills/software-development/whatsapp-gateway-customization/scripts/hussh_one_doctor_heal.py`
   (last touched 2026-06-25, 186 lines, hardcodes a repo path that doesn't
   match this checkout). It is not the one the cron runs — per
   `docs/hussh-one/operations/README.md`, bootstrap installs and updates only
   `~/.hermes/scripts/hussh_one_doctor_heal.py` (299 lines, env-aware repo
   resolution, alert-state persistence). The stale copy risks someone
   "fixing" the wrong file and believing the fix is live. Recommend deleting
   it or replacing it with a pointer comment to the real one.
3. **Live import drift observed in `~/.hermes/logs/errors.log`**:
   `Gateway approval notify failed: cannot import name
   'compression_made_progress' from 'agent.turn_context'` at 2026-08-23
   21:34:02, even though that function exists on disk
   (`agent/turn_context.py:293`). The gateway process running at the time
   (pid 40492) started 2026-08-21T22:14:57 — before that function was added
   to the checked-out code — so its already-imported module object simply
   never picked up the change; this is expected Python behavior, not a bug in
   the function itself. It only broke a background approval-notification
   path (non-fatal, single occurrence), but the same staleness could hit a
   request-critical path after a future edit under `agent/`. **Prevention:
   always follow a `git pull`/code change touching `agent/`, `gateway/`, or
   `hermes_cli/` with `hermes gateway restart`** — a `git pull` alone does
   not reload the running process. Consider adding this as an explicit step
   to `scripts/hussh-one-guard.sh` or the onboarding checklist in
   `docs/hussh-one/operations/README.md`.

---

## If the gateway ever seems offline

1. Check real state first, don't assume: `launchctl print
   gui/$(id -u)/ai.hermes.gateway | grep state`. If it says `running`, the
   process is up — but "up" is not the same as "working"; see step 2 and
   Incident 2 above. A "disconnected" chat is more likely a client-side
   reconnect than a gateway outage.
2. **Run the doctor before touching anything:**
   `scripts/hussh-one-doctor.sh --require-services`. As of this audit it
   checks, in order: no unfinished merge / conflict markers in the working
   tree, `hermes_cli.main` actually imports, branch/remote sanity, required
   files, config, supervisor status, and whether recent messages are actually
   getting model responses (not just whether the process is alive).
3. If it says anything other than `running`, force a kickstart:
   `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`.
4. If kickstart doesn't bring it to `running` within a few seconds (the
   `not-loaded` case doctor can't self-heal), reload the job explicitly:
   `launchctl bootout gui/$(id -u)/ai.hermes.gateway 2>/dev/null; launchctl
   bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway.plist`.
5. Prefer `hermes gateway restart` over killing the process by hand when
   applying a config change — it's the same 31-second recovery path this
   report validated, and it's what gets logged to `gateway-restart.log`. If
   this command itself fails with a `SyntaxError` or import error, that *is*
   the incident — see Incident 2: fix the working tree
   (`git status`, resolve or `git merge --abort`, get back to `main`) before
   anything else, because until it imports cleanly the OS-level and cron
   self-healing layers cannot recover the gateway either.
6. For a full incident timeline, read `~/.hermes/logs/gateway-exit-diag.log`
   (structured JSON, one line per start/stop) rather than
   `gateway-restart.log` (see [finding 1](#findings--prevention)), and check
   `~/.hermes/logs/gateway.log` for repeated provider errors (403/401) that
   would explain "up but not answering."

## Verification

```bash
# Confirm both layers are wired to the real process
launchctl print gui/$(id -u)/ai.hermes.gateway | grep -E "state|pid|runs"
python3 -c "import json; d=json.load(open('$HOME/.hermes/cron/jobs.json'));
print(next(j for j in (d if isinstance(d, list) else d.values())
           if isinstance(j, dict) and 'doctor' in str(j.get('script','')).lower()))"

# Dry-run the doctor without sending a message
HERMES_HOME="$HOME/.hermes" .venv/bin/python ~/.hermes/scripts/hussh_one_doctor_heal.py

# Re-run the downtime analysis over gateway-exit-diag.log for a fresh window
# (pair each exit_nonzero/exit_clean with the next gateway.start; see this
# audit's method above)
```

---

## See also
- [Operations Runbook](./README.md) — doctor cron install/update details
- [Crash resilience — dashboard OOM & session-model persistence](./crash-resilience.md) — the dashboard's equivalent hardening
- [Upgrading from upstream](./upgrading.md)
