# hussh 🤫 One — Cognitive & Architectural Abstraction Specification
### Machine-Readable System Sync & Integration Specification
`Version: 1.0.0` | `Target System: hussh-one-agents` | `Base: hermes-agent-v0.15.1`

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
    cron.wrap_response: false
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
# Register upstream for official updates & shared repo for customizations
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git remote add hushh-labs https://github.com/hushh-labs/hushh-agents.git
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
Disable automated cron wrappers to ensure only clean metaphorical summaries are transmitted to mobile screens:
```bash
# Bypasses "Cronjob Response (job_id: xxx)" prepended text
.venv/bin/hermes config set cron.wrap_response false

# Set custom personality skin
.venv/bin/hermes config set display.skin hussh-one
```

### Step 4: Standalone Daemon Supervision (macOS launchd)
To ensure old background instances of the Node.js bridge don't cache stale prefix strings, always run a hard flush during a restart:
```bash
# Kill any orphaned node bridges hanging on port 3000
kill -9 $(lsof -t -i:3000)

# Restart the supervision daemon
.venv/bin/hermes gateway restart
```

The local browser dashboard must expose the real Hermes TUI chat surface:

```bash
scripts/hussh-one-restart.sh
```

This launches `hermes dashboard --tui` on port `9119` and the gateway/WhatsApp bridge on port `3000`.

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
*   **Verification:** Run `scripts/hussh-one-restart.sh`, then `scripts/hussh-one-guard.sh`. The guard fails if the dashboard is reachable without `__HERMES_DASHBOARD_EMBEDDED_CHAT__=true`.

### Contract E: Natural-Language Model Switching
*   **Invariant:** The TUI and WhatsApp channel may accept short, direct user text such as `switch to opus 4.8`, `switch to sonnet 4.6`, or `switch back to gemini 3.5 flash` as a session-only `/model` switch. The default config remains Gemini 3.5 Flash unless `/model ... --global` is used.
*   **Prompt-injection safeguard:** Detection must stay deterministic and reject slash commands, quoted text, URLs, code blocks, lists, long pasted text, help questions, negations, and injection-shaped phrases such as `ignore previous`, `system prompt`, `developer message`, or `webpage says`.
*   **Verification:** Run `tests/hermes_cli/test_natural_model_switch.py`, `tests/gateway/test_natural_model_switch.py`, and the TUI prompt-submit natural switch test.
