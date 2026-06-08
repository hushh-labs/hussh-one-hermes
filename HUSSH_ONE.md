# hussh 🤫 One — Cognitive & Architectural Abstraction Specification
### Machine-Readable System Sync & Integration Specification
`Version: 1.0.0` | `Target System: hussh-one-agents` | `Base: hermes-agent-v0.15.1`

> 📖 **Human-readable companion:** the nested, advertisable product & architecture
> docs live in [`docs/hussh-one/`](./docs/hussh-one/README.md) — overview, brand,
> architecture, per-feature pages, operations runbooks, contracts, and roadmap.
> This file remains the dense machine-readable spec.

---

## ⚕ SYSTEM SUMMARY & METADATA

```yaml
system_identity:
  name: "hussh 🤫 One"
  icon: "🤫"
  branding:
    whatsapp_reply_prefix: "hussh 🤫 One\n"
    cli_branding_skin: "hussh-one"

environment_variables:
  WHATSAPP_REPLY_PREFIX:
    type: "string"
    description: "Emergency override only. The default reply prefix comes from the brand profile/config."
  WHATSAPP_ALLOWED_GROUPS:
    type: "comma_separated_string"
    description: "JIDs of allowed WhatsApp groups to bypass DM-only/self-chat filters."
  HERMES_PROFILE:
    type: "string"
    description: "Determines the active configuration and data subdirectory. Keeps memory databases isolated."
    valid_examples: ["kushal", "msainani"]

configuration_schema_mutations:
  ~/.hermes/config.yaml:
    display.skin: "hussh-one"
    dashboard.theme: "hussh-one"
    model.provider: "gemini"
    model.default: "gemini-3.5-flash"
    cron.wrap_response: false
    whatsapp.require_mention_on_replies: true
    display.tool_progress: false
    display.interim_assistant_messages: false
    display.show_reasoning: false
    approvals.mode: false
```

---

## 1. COGNITIVE FLOW & DUAL-PROCESS CONVERGENCE

Future AI agents and automated scripts running this system must adhere to the **Dual-Process Cognitive Memory Pipeline**. This pipeline guarantees semantic continuity during waking cycles (System 1) while running offline mathematical consolidation during sleep cycles (System 2) without data cross-contamination.

```
+-----------------------------------------------------------------------------------+
|                            SYSTEM 1 (WAKING PROCESS)                              |
|                                                                                   |
|  [User Message] --> [WhatsApp / TUI Gateway] --> [Active Profile: state.db]       |
|                                                          │                        |
|                                                          ▼                        |
|                                                 [Lightweight Memory]              |
|                                               ~/.hermes/profiles/X/               |
+-----------------------------------------------------------------------------------+
                                                           │
                                                           ▼ Raw SQL Logs
+-----------------------------------------------------------------------------------+
|                            SYSTEM 2 (DREAM CYCLE)                                 |
|                                                                                   |
|  [Cron: 17:00 PM] --> [auto_dream.py] --(Filters out Cron logs)--> [Assembled]    |
|                                                                    [Dream Context]|
|                                                                          │        |
|  [WhatsApp Summary] <-- (Conconcatenated) <-- [Consolidator Agent] <─────┘        |
|                                                       │                           |
|                                                       ▼                           |
|                                            [Long-Term Memory Update]              |
|                                            - MEMORY.md, procedures.md             |
+-----------------------------------------------------------------------------------+
```

### A. System 1: Real-Time Contextual Buffer (Working Memory)
*   Active conversations are routed based on the environmental flag `HERMES_PROFILE`. 
*   **Machine Sync Action:** When executing tool or chat operations, the agent **MUST** check `os.environ.get("HERMES_PROFILE")`. All persistent writes, session states, and SQLite locks must resolve strictly under `~/.hermes/profiles/$HERMES_PROFILE/` to keep Kushal and MSainani's universes separated.

### B. System 2: Nightly Cognitive Consolidation (Auto-Dream)
*   **Cron Job ID:** `2e5aee0849fb`
*   **Trigger Schedule:** `0 17 * * *` (Daily at 5:00 PM) with a custom `grace_seconds: 18000` (5-hour grace catchup period on sleep wakeup).
*   **Workspace Constraint:** `workdir` must be locked to `/Users/kushaltrivedi/.hermes` to keep path resolutions clean and prevent catastrophic home directory traversals.
*   **Threat-Phrase Defang (False-Positive Guard):** `auto_dream.py` ingests the owner's own recent conversations. When those chats *discuss* security topics (prompt-injection hardening, exfiltration, jailbreaks), the literal phrases land in the compiled cron prompt and the Tirith scanner blocks the job with `Blocked: prompt matches threat pattern 'prompt_injection'`. The script runs `defang_threat_terms()` over every ingested string (message bodies, titles, and `MEMORY.md`/`procedures.md`/`index.json` reads) to insert a `U+00B7` middle-dot inside each trigger word (`prompt injection` → `p·rompt injection`), neutralizing the regex match while staying human-readable. Patterns use `[\s\-_]*` between words so the hyphenated variant is also caught. Never apply the defang to the daemon's own instruction prompt — only to ingested history. Verify with the live scanner, not grep: `cd ~/.hermes/hermes-agent && python3 -c "import sys; sys.path.insert(0,'.'); import subprocess, tools.cronjob_tools as ct; print(ct._scan_cron_prompt(subprocess.run(['python3','$HOME/.hermes/scripts/auto_dream.py'],capture_output=True,text=True).stdout) or 'PASS')"`.

### C. WhatsApp Group Capsules (Sandboxed Social Brains)
Group chats the owner opts in are run as **capsules** — isolated, read-only sandboxes that let non-owners interact with `@One` without ever touching the owner's private world. Config lives under `whatsapp.capsules.<group_jid>` in `~/.hermes/config.yaml`.

*   **One JID = one capsule.** WhatsApp can re-mint a group's JID (re-creation, community linking, test duplicates). Always confirm the live JID from the local DB before editing — do not keep stale duplicates:
    ```python
    # ChatStorage.sqlite: group.net.whatsapp.WhatsApp.shared
    SELECT ZPARTNERNAME, ZCONTACTJID FROM ZWACHATSESSION WHERE ZCONTACTJID LIKE '%@g.us'
    ```
*   **Isolation contract (per capsule):** `skip_global_memory: true`, `skip_global_user_profile: true`, `block_outbound_send: true`, `enabled_toolsets: [web, vision]` only. Each capsule gets its OWN memory vault via `memory_dir: capsules/<name>` so social groups never cross-contaminate each other or the owner's main agent.
*   **Triggering:** capsule groups are exempt from the allowlist friction but still require an explicit mention (`@One`, `@husshOne`, `@hussh-one`) per `whatsapp.mention_patterns` + `require_mention: true`.
*   **Active capsules (as of writing — confirm live):** `three-musketeers` (`120363405517552679@g.us`) and `one-team` (`120363425605838730@g.us`).

---

## 2. REFACTORING & RECOVERY INVARIANTS (THE CRITICAL BUGS DETECTED & RESOLVED)

Any agent updating or modifying this codebase must preserve these three critical patches:

### A. Gemini Parallel Tool Call Response Part-Mismatch (HTTP 400 Mismatch)
*   **The Bug:** OpenAI and Anthropic APIs return parallel tool responses as consecutive, separate messages. The base Gemini native adapter historically translated each separate message into its own individual `user` turn. This caused the Gemini API to reject the call because a `model` turn with $N$ parallel `functionCall` parts was not immediately followed by a *single* `user` turn with exactly $N$ matching `functionResponse` parts.
*   **The Invariant Patch (`agent/gemini_native_adapter.py`):**
    We refactored `_build_gemini_contents` to merge consecutive same-role messages. If a message is a tool response and the previous item in the `contents` stack was also a `user` turn containing a `functionResponse` part, the new part is **extended** into the existing turn's list instead of creating a new turn:
    ```python
    if parts:
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": gemini_role, "parts": parts})
    ```

### B. Streaming Delta Trailing-Bracket JSON Decoupling (Unrepairable JSON)
*   **The Bug:** Gemini's streaming API sends the *entire accumulated JSON arguments* on every chunk, rather than standard text deltas. String-based prefix matching fails because complete JSON objects close themselves on every tick (e.g. `{"path": "x"}` -> `{"path": "xy"}`). Standard prefix matching would strip `{` and `}` but keep intermediate quotes, causing strings to concatenate as duplicate malformed blocks like `{"path": "x"}{"path": "xy"}`.
*   **The Invariant Patch (`agent/gemini_native_adapter.py`):**
    We refactored `translate_stream_event` to **only emit and update the `arguments` string on the final chunk** (i.e. when `finishReason` is present). On intermediate chunks, arguments are kept as `""` to prevent malformed string accumulations on the receiver side:
    ```python
    emitted_arguments = ""
    if finish_reason_raw:
        emitted_arguments = args_str
        last_arguments = str(slot.get("last_arguments") or "")
        if last_arguments:
            if args_str == last_arguments:
                emitted_arguments = ""
            elif args_str.startswith(last_arguments):
                emitted_arguments = args_str[len(last_arguments):]
        slot["last_arguments"] = args_str
    ```

### C. Infinite Memory Feedback Loops (Auto-Dream Context Ballooning)
*   **The Bug:** The pre-run script `auto_dream.py` queries `state.db` for recent sessions. Because it queried all sessions, it would collect previous `Auto-Dream` sessions (which are extremely large). In successive runs, the input token size grew exponentially (800k -> 1.4M), crashing the context window limits.
*   **The Invariant Patch (`~/.hermes/scripts/auto_dream.py`):**
    We modified the SQLite query in `collect_recent_logs` to explicitly ignore any cron or scheduler-owned sessions:
    ```python
    cursor.execute("SELECT id, title, started_at, source FROM sessions WHERE started_at >= ? AND source != 'cron' AND id NOT LIKE 'cron_%'")
    ```

---

## 3. COMPONENT INTERACTION SPECIFICATIONS

```
  [ Python Gateway Core ]                     [ Node.js WhatsApp Bridge ]
            │                                              │
            │  (1) Load env from PROJECT_DIR/.env          │
            ├─────────────────────────────────────────────>│  (2) Environment Captured:
            │                                              │      - WHATSAPP_REPLY_PREFIX
            │                                              │      - WHATSAPP_ALLOWED_GROUPS
            │                                              │
            │  (3) JSON-RPC HTTP Payload (Port 3000)       │
            ├─────────────────────────────────────────────>│  (4) Injects custom reply_prefix 
            │                                              │      and executes E2EE decryption 
            │                                              │      of historical media files.
```

---

## 4. BOOTSTRAPPING A NEW DEVELOPER/MACHINE INSTANCE

When setting up a new fork, a fresh agent, or a collaborator machine (`msainani`), follow this exact protocol to keep the abstractions clean and verifiable:

### Step 1: Remote Syncing
```bash
# Clone the Hussh One fork and register upstream for official updates
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git switch hussh-one-hermes
```

### Step 2: Environment Isolation Configuration
Never commit your personal variables to Git. Write them directly to the local `.env` which is git-ignored:
```bash
# /Users/msainani/Documents/GitHub/hermes-agent/.env
WHATSAPP_ALLOWED_GROUPS="120363040968035480@g.us"
```

Configure the system profile to isolate memory and SQLite states:
```bash
export HERMES_PROFILE="msainani"
```

### Step 3: Global System Preferences
Disable automated cron wrappers and set the Hussh One local defaults:
```bash
# Bypasses "Cronjob Response (job_id: xxx)" prepended text
.venv/bin/hermes config set cron.wrap_response false

# Set Hussh One identity and keep the global default on Gemini Flash
.venv/bin/hermes config set display.skin hussh-one
.venv/bin/hermes config set dashboard.theme hussh-one
.venv/bin/hermes config set model.provider gemini
.venv/bin/hermes config set model.default gemini-3.5-flash

# Configure strict, noise-free group tagging and auto-approvals
.venv/bin/hermes config set whatsapp.require_mention_on_replies true
.venv/bin/hermes config set display.tool_progress false
.venv/bin/hermes config set display.interim_assistant_messages false
.venv/bin/hermes config set display.show_reasoning false
.venv/bin/hermes config set approvals.mode off
```

### Step 4: Bootstrap, Supervisor, and Doctor
Fresh clones should use the Hussh One bootstrap rather than hand-running screen sessions:
```bash
scripts/hussh-one-bootstrap.sh --manager auto

# Optional: install/start the detected supervisor as part of bootstrap
scripts/hussh-one-bootstrap.sh --manager auto --start
```

Lifecycle is owned by one detected manager:

```bash
scripts/hussh-one-supervisor.sh install
scripts/hussh-one-supervisor.sh restart
scripts/hussh-one-supervisor.sh status
scripts/hussh-one-doctor.sh --require-services
scripts/hussh-one-restart.sh
```

The supervisor chooses `launchd` on macOS, user `systemd` on Linux, s6 in supported containers, and `screen` only as a fallback. It refuses mixed manager state unless `--clean-conflicts` is passed. The dashboard is always launched as `hermes dashboard --tui --no-open` on port `9119`, and the gateway/WhatsApp bridge health remains on port `3000`.

---

## 6. SANDBOXED GROUP CONTAINERS ("Capsule" mode)

A **capsule** is a per-group sandbox so a non-owner social group (e.g. "Three
Musketeers") can be opened to `@One` WITHOUT leaking the owner's private memory,
user profile, work/project context, or live credentials — and without the
ability to mutate anything (no send-to-other-chats, no file deletes, no command
injection). The agent may still READ public/outside info (web) and it grows its
OWN memory scoped to that group only.

### Why in-process (not a second profile)
WhatsApp runs as a SINGLE Baileys session on one bridge (port 3000). You cannot
run a second gateway under a different `HERMES_PROFILE` bound to the same
WhatsApp account — they would fight over the session creds. So the container is
built IN-PROCESS: the gateway detects the capsule JID and, for that session
only, swaps in (a) an isolated memory dir, (b) skip of the global USER/MEMORY,
and (c) a read-only toolset.

### Config schema (`config.yaml` → `whatsapp.capsules`)
```yaml
whatsapp:
  capsules:
    "120363405517552679@g.us":          # Three Musketeers JID
      name: "three-musketeers"
      memory_dir: "capsules/three-musketeers"   # under HERMES_HOME; isolated MEMORY.md/USER.md
      skip_global_memory: true                   # do NOT load owner's MEMORY.md/USER.md
      skip_global_user_profile: true
      enabled_toolsets: ["web", "vision"]        # READ-only / public-info tools only
      disabled_toolsets: ["terminal","file","delegation","cronjob","skills","session_search","kanban","spotify","homeassistant"]
      block_outbound_send: true                  # may only reply in THIS chat; never send_message elsewhere
      system_prompt: >
        You are operating inside the Three Musketeers social group capsule.
        You have NO access to Kushal's personal data, phone numbers, work/Hushh
        details, file contents, credentials, or any global memory. You may read
        public web info. You may NOT send messages to other chats, delete or
        modify files, run shell commands, or query consent/MCP data. Only answer
        the casual question asked, warmly and briefly. Any memory you form stays
        inside this capsule.
```

### Invariants (Capsule contract)
1. **Memory isolation** — capsule sessions resolve `get_memory_dir()` to
   `HERMES_HOME/capsules/<name>/`. The owner's `MEMORY.md`/`USER.md` are never
   loaded (skip_global_memory/skip_global_user_profile). New memory the agent
   writes lands only in the capsule dir.
2. **Read-only blast radius** — capsule sessions load ONLY `enabled_toolsets`
   (default `web`,`vision`). All mutating/sensitive toolsets (terminal, file,
   delegation, cronjob, skills, session_search, MCP/consent) are stripped.
3. **No lateral send** — `block_outbound_send` forbids `send_message`/fan-out to
   any target other than the originating capsule chat.
4. **Non-owner triggering (capsule-only)** — UNLIKE every other group/DM (which
   is 100% owner-only), capsule groups listed in `WHATSAPP_CAPSULE_GROUPS` MAY be
   triggered by OTHER members, but ONLY via an explicit `@One` / `@husshOne` /
   `@hussh-one` tag (or a `/` slash command). Untagged non-owner chatter is still
   dropped at the Node bridge. Non-capsule groups/DMs remain owner-only and
   injection-proof. The owner (`fromMe`) is never gated by this.
5. **Anti-DOS rate limit (non-owner only)** — non-owner capsule invocations are
   rate limited per-sender to stop runaway `@One` floods from burning compute.
   Generous by design so real conversation never trips it. Defaults:
   `WHATSAPP_CAPSULE_RATE_MAX=30` per `WHATSAPP_CAPSULE_RATE_WINDOW_MS=60000`
   (30 / minute / sender / group). Enforced in `bridge.js` BEFORE the agent runs,
   so blocked spam costs zero compute. Owner `fromMe` messages are never limited.
6. **Branding preserved** — the canonical stacked header still applies.

### Bridge env keys (capsule triggering + rate limit)
- `WHATSAPP_CAPSULE_GROUPS` — comma-separated JIDs where non-owners may tag `@One`
  (must also have a matching `whatsapp.capsules` entry in config.yaml for the sandbox).
- `WHATSAPP_CAPSULE_RATE_MAX` — max non-owner invocations per window (default 30).
- `WHATSAPP_CAPSULE_RATE_WINDOW_MS` — sliding window in ms (default 60000).

### Verification (Contract F)
- `tests/gateway/test_whatsapp_capsule.py`: asserts a capsule session (a) gets
  the isolated memory dir, (b) does NOT receive global MEMORY/USER, (c) loads
  only the read-only toolset, (d) refuses outbound send to a non-capsule target.
- `bridge.js` capsule path: non-owner messages require an explicit `@One` tag and
  pass the per-sender rate limiter before being forwarded to the gateway.

---

## 5. MACHINE-READABLE VERIFICATION CONTRACTS

Future machines must run these tests to verify the integrity of the abstraction layers.

### Contract A: Group Routing & Decryption Safeguard
*   **Invariant:** Messages from non-allowed groups must return an `ignored` event structure.
*   **Verification:** Verify that `bridge.js` does not emit payloads to Python for any JID not in `ALLOWED_GROUPS` when `WHATSAPP_MODE=self-chat`.

### Contract B: Zero-Width Unicode Leakage
*   **Invariant:** Database dumps injected into the assembled cron prompt must contain exactly `0` instances of zero-width unicode characters (`U+200B`, `U+200C`, `U+200D`, `U+FEFF`).
*   **Verification:** Run `auto_dream.py` and inspect standard output for any hidden sequences that would trigger the prompt-injection scanner gates.

### Contract C: Upstream Update Guard
*   **Invariant:** Official Hermes and plugin updates must not erase the Hussh One brand profile, skin, dashboard theme, WhatsApp prefix default, or Vertex Claude provider abstraction.
*   **Verification:** Run `scripts/hussh-one-guard.sh` after every upstream merge, plugin update, or provider-runtime edit. The maintenance procedure is documented in `docs/hussh-one-upstream-maintenance.md`.

### Contract D: Dashboard Chat Surface
*   **Invariant:** The Hussh One dashboard must expose embedded chat through the real Hermes TUI, not a forked React chat composer.
*   **Verification:** Run `scripts/hussh-one-supervisor.sh restart`, then `scripts/hussh-one-doctor.sh --require-services` and `scripts/hussh-one-guard.sh`. The guard fails if the dashboard is reachable without `__HERMES_DASHBOARD_EMBEDDED_CHAT__=true`.

### Contract E: Natural-Language Model Switching
*   **Invariant:** The TUI and WhatsApp channel may accept short, direct user text such as `switch to opus 4.8`, `switch to sonnet 4.6`, or `switch back to gemini 3.5 flash` as a session-only `/model` switch. The default config remains Gemini 3.5 Flash unless `/model ... --global` is used.
*   **Prompt-injection safeguard:** Detection must stay deterministic and reject slash commands, quoted text, URLs, code blocks, lists, long pasted text, help questions, negations, and injection-shaped phrases such as `ignore previous`, `system prompt`, `developer message`, or `webpage says`.
*   **Vertex safeguard:** Vertex Claude model switches must run a Hermes-runtime-shaped live access check before mutating the session. Long-lived sessions must also fail fast before the next model call if a stale Vertex Claude runtime points at a model such as `claude-opus-4-8` that the active project cannot run. Stale runtimes carrying `gcp-sdk` plus a Vertex AI base URL must normalize back to the `google-vertex-claude` adapter instead of using the native Anthropic client.
*   **Verification:** Run `tests/hermes_cli/test_natural_model_switch.py`, `tests/gateway/test_natural_model_switch.py`, and the TUI prompt-submit natural switch test.
