#!/usr/bin/env bash
# Hussh One — VS Code Copilot BYOK (Vertex ADC) onboarding.
#
# Idempotent setup for native VS Code Copilot Custom Endpoints backed by Google
# Vertex AI through Application Default Credentials (ADC). Stands up two local,
# loopback-only services:
#
#   :8643  LiteLLM proxy  — transparent Vertex->OpenAI passthrough (native tool
#                           calling / edit / apply / agent mode). DB-less.
#   :8644  auth shim       — deterministic 401s + streaming passthrough in front
#                           of the proxy. Copilot points HERE.
#
# What this does (all idempotent / re-runnable):
#   1. Resolve the Vertex project (GOOGLE_CLOUD_PROJECT env, gcloud, or --project).
#   2. Verify gcloud ADC is present (warns with the fix command if not).
#   3. Create an isolated ~/.hermes/litellm-venv with litellm + deps if missing.
#   4. Generate a master key once (persisted; reused on re-runs).
#   5. Materialize launchers + proxy config + shim into ~/.hermes from repo assets.
#   6. Write VS Code Copilot chatLanguageModels.json (Insiders and/or stable),
#      pointing the "Hussh One Vertex ADC" endpoint at the shim (:8644).
#   7. Optionally start the services and smoke-test end to end (--start).
#
# Secrets: the master key lives only in ~/.hermes/scripts/start_litellm_proxy.sh
# (chmod 700) and is never printed in full. Bound to 127.0.0.1 only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ASSETS="$SCRIPT_DIR/copilot-byok"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PROJECT="${VERTEX_PROJECT:-}"
START_SERVICES=0
DRY_RUN="${HUSSH_ONE_DRY_RUN:-0}"
WRITE_VSCODE=1
USE_LAUNCHD=0
ALLOW_UNAUTHENTICATED_LOOPBACK=0
PROXY_PORT=8643
SHIM_PORT=8644

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-copilot-setup.sh [options]

Options:
  --project ID        Vertex/GCP project (default: $GOOGLE_CLOUD_PROJECT or gcloud)
  --start             Start/restart the proxy + shim after setup, then smoke test
  --launchd           (macOS) Install launchd KeepAlive agents for instant restart
                      of the proxy + shim on crash/OOM/sleep (recommended)
  --allow-unauthenticated-loopback
                      Compatibility mode for VS Code builds that fail to forward
                      custom-endpoint credentials. Accepts headerless requests
                      only at the loopback-bound shim; see security note below.
  --no-vscode         Do not write VS Code chatLanguageModels.json
  --dry-run           Print actions without mutating the machine
  -h, --help          Show this help

After setup, in VS Code: reload the window (Developer: Reload Window) and pick a
model from the "Hussh One Vertex ADC" endpoint. The setup writes the generated
loopback bearer key into the endpoint configuration automatically; do not paste
or leave a key blank. If VS Code prompts for one, it has retained a stale
configuration — reload the window (or restart VS Code) and select the Hussh One
endpoint again.

Compatibility mode is only for a VS Code custom-endpoint forwarding defect. It
keeps the shim bound to 127.0.0.1, but a local process (or a remote client using
an SSH port forward) could use the endpoint without the local bearer key. Prefer
the default authenticated mode whenever your VS Code build forwards credentials.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --start) START_SERVICES=1; shift ;;
    --launchd) USE_LAUNCHD=1; shift ;;
    --allow-unauthenticated-loopback) ALLOW_UNAUTHENTICATED_LOOPBACK=1; shift ;;
    --no-vscode) WRITE_VSCODE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
err()  { printf 'error: %s\n' "$*" >&2; }

run_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'dry-run:'; printf ' %q' "$@"; printf '\n'; return 0
  fi
  "$@"
}

safe_suffix() {
  local value="$1"
  if [[ -z "$value" ]]; then printf '<unset>'
  elif [[ ${#value} -le 6 ]]; then printf '***'
  else printf '***%s' "${value: -6}"; fi
}

mkdir -p "$HERMES_HOME/scripts" "$HERMES_HOME/logs"

# ── 1. Resolve project ───────────────────────────────────────────────────────
if [[ -z "$PROJECT" ]]; then
  if command -v gcloud >/dev/null 2>&1; then
    PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
  fi
fi
if [[ -z "$PROJECT" ]]; then
  err "No Vertex project. Pass --project ID, set GOOGLE_CLOUD_PROJECT, or run: gcloud config set project <id>"
  exit 1
fi
log "Vertex project: $(safe_suffix "$PROJECT")"

# ── 2. Verify ADC ────────────────────────────────────────────────────────────
ADC_FILE="$HOME/.config/gcloud/application_default_credentials.json"
if command -v gcloud >/dev/null 2>&1; then
  if gcloud auth application-default print-access-token >/dev/null 2>&1; then
    log "Vertex ADC: application-default credentials available."
  else
    warn "Vertex ADC missing. Run: gcloud auth application-default login"
  fi
else
  warn "gcloud not installed; cannot verify ADC. Install Google Cloud SDK and run: gcloud auth application-default login"
fi

# ── 3. Master key (generate once, reuse) ─────────────────────────────────────
LAUNCHER_PROXY="$HERMES_HOME/scripts/start_litellm_proxy.sh"
KEY=""
if [[ -f "$LAUNCHER_PROXY" ]]; then
  KEY="$(grep -o 'LITELLM_MASTER_KEY="[^"]*"' "$LAUNCHER_PROXY" 2>/dev/null | cut -d'"' -f2 || true)"
fi
if [[ -z "$KEY" ]]; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  log "Generated new master key: $(safe_suffix "$KEY")"
else
  log "Reusing existing master key: $(safe_suffix "$KEY")"
fi

# ── 4. LiteLLM venv ──────────────────────────────────────────────────────────
LITELLM_VENV="$HERMES_HOME/litellm-venv"
if [[ ! -x "$LITELLM_VENV/bin/litellm" ]]; then
  log "Creating LiteLLM venv at $LITELLM_VENV ..."
  if [[ "$DRY_RUN" != "1" ]]; then
    python3 -m venv "$LITELLM_VENV"
    "$LITELLM_VENV/bin/python" -m pip install --upgrade pip >/dev/null
    # litellm[proxy] pulls in uvicorn/starlette/httpx the shim also needs.
    "$LITELLM_VENV/bin/python" -m pip install "litellm[proxy]" "google-cloud-aiplatform" >/dev/null
  fi
else
  log "LiteLLM venv present."
fi

# ── 5. Materialize assets into ~/.hermes (graceful: validate → backup → swap) ─
# Every model/config addition goes through this same safe path so a bad model
# entry (typo'd Vertex model id, malformed YAML, broken shim edit) can NEVER
# leave a running proxy replaced by one that won't boot or won't auth. We only
# ever touch the LIVE files after the CANDIDATE has proven itself:
#   1. render candidate into a *.new scratch file (never touch the live file)
#   2. validate candidate (YAML parses / Python compiles)
#   3. snapshot the current live file to *.bak (only if a live file exists)
#   4. atomically mv candidate -> live
#   5. (later, step 7) start/restart + smoke test; on smoke-test FAILURE,
#      automatically restore *.bak and restart again — see rollback path below.
PROXY_CONFIG="$HERMES_HOME/litellm-proxy-config.yaml"
SHIM_DST="$HERMES_HOME/scripts/litellm_auth_shim.py"
LAUNCHER_SHIM="$HERMES_HOME/scripts/start_litellm_shim.sh"
PROXY_CONFIG_BAK="$PROXY_CONFIG.bak"
SHIM_DST_BAK="$SHIM_DST.bak"

validate_yaml() {  # $1 = path
  python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "$1" 2>&1
}

validate_py() {  # $1 = path
  python3 -m py_compile "$1" 2>&1
}

# proxy config (substitute project) — render to scratch, validate, then swap.
if [[ "$DRY_RUN" != "1" ]]; then
  PROXY_CONFIG_NEW="$PROXY_CONFIG.new"
  sed "s/__VERTEX_PROJECT__/$PROJECT/g" "$ASSETS/litellm-proxy-config.template.yaml" > "$PROXY_CONFIG_NEW"
  if ! yaml_err="$(validate_yaml "$PROXY_CONFIG_NEW")"; then
    err "Generated litellm-proxy-config.yaml is not valid YAML — refusing to touch the live config."
    err "$yaml_err"
    rm -f "$PROXY_CONFIG_NEW"
    exit 1
  fi
  if [[ -f "$PROXY_CONFIG" ]]; then
    cp "$PROXY_CONFIG" "$PROXY_CONFIG_BAK"
  fi
  mv "$PROXY_CONFIG_NEW" "$PROXY_CONFIG"
  chmod 600 "$PROXY_CONFIG"

  SHIM_DST_NEW="$SHIM_DST.new"
  cp "$ASSETS/litellm_auth_shim.py" "$SHIM_DST_NEW"
  if ! py_err="$(validate_py "$SHIM_DST_NEW")"; then
    err "Generated litellm_auth_shim.py failed to compile — refusing to touch the live shim."
    err "$py_err"
    rm -f "$SHIM_DST_NEW"
    exit 1
  fi
  if [[ -f "$SHIM_DST" ]]; then
    cp "$SHIM_DST" "$SHIM_DST_BAK"
  fi
  mv "$SHIM_DST_NEW" "$SHIM_DST"
  chmod 755 "$SHIM_DST"
else
  log "dry-run: would write $PROXY_CONFIG and $SHIM_DST"
fi

# proxy launcher (carries the key; chmod 700)
if [[ "$DRY_RUN" != "1" ]]; then
  cat > "$LAUNCHER_PROXY" <<EOF
#!/bin/bash
# Hussh One — LiteLLM Vertex ADC proxy launcher (generated by hussh-one-copilot-setup.sh)
# Transparent Vertex AI -> OpenAI proxy for VS Code Copilot BYOK. 127.0.0.1:$PROXY_PORT only.
set -euo pipefail
export GOOGLE_APPLICATION_CREDENTIALS="\$HOME/.config/gcloud/application_default_credentials.json"
export LITELLM_MASTER_KEY="$KEY"
export GOOGLE_CLOUD_PROJECT="$PROJECT"
exec "\$HOME/.hermes/litellm-venv/bin/litellm" \\
  --config "\$HOME/.hermes/litellm-proxy-config.yaml" \\
  --port $PROXY_PORT \\
  --host 127.0.0.1
EOF
  chmod 700 "$LAUNCHER_PROXY"

  # shim launcher (reads key from proxy launcher; chmod 700)
  cat > "$LAUNCHER_SHIM" <<EOF
#!/bin/bash
# Hussh One — LiteLLM auth shim launcher (generated by hussh-one-copilot-setup.sh)
# Deterministic 401s + streaming passthrough in front of the proxy. 127.0.0.1:$SHIM_PORT only.
# VS Code Copilot points at :$SHIM_PORT, which forwards to :$PROXY_PORT.
set -euo pipefail
export LITELLM_MASTER_KEY="\$(grep -o 'LITELLM_MASTER_KEY="[^"]*"' "\$HOME/.hermes/scripts/start_litellm_proxy.sh" | cut -d'"' -f2)"
export SHIM_UPSTREAM="http://127.0.0.1:$PROXY_PORT"
export SHIM_HOST="127.0.0.1"
export SHIM_PORT="$SHIM_PORT"
export HUSSH_SHIM_ALLOW_LOOPBACK_ANONYMOUS="$ALLOW_UNAUTHENTICATED_LOOPBACK"
exec "\$HOME/.hermes/litellm-venv/bin/python" "\$HOME/.hermes/scripts/litellm_auth_shim.py"
EOF
  chmod 700 "$LAUNCHER_SHIM"
  log "Wrote launchers, proxy config, and shim into $HERMES_HOME."
else
  log "dry-run: would write $LAUNCHER_PROXY and $LAUNCHER_SHIM"
fi

# ── 6. VS Code Copilot config ────────────────────────────────────────────────
write_vscode_config() {
  local user_dir="$1" edition="$2"
  [[ -d "$user_dir" ]] || return 0
  local target="$user_dir/chatLanguageModels.json"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: would write $target ($edition)"
    return 0
  fi
  VSCODE_TARGET="$target" SHIM_PORT="$SHIM_PORT" MASTER_KEY="$KEY" python3 - <<'PY'
import json, os, sys

target = os.environ["VSCODE_TARGET"]
shim_port = os.environ["SHIM_PORT"]
master_key = os.environ.get("MASTER_KEY", "")
url = f"http://127.0.0.1:{shim_port}/v1"

# Context/output limits below are LIVE-PROBED against Vertex ADC (Jul 2026):
# all four Claudes natively accept 1M-token prompts on Vertex (no beta header;
# rejected only above 1,000,000: "prompt is too long: N > 1000000 maximum")
# and cap output at exactly 128,000 ("max_tokens: N > 128000"). Gemini 3.5
# Flash: 1,048,576 in / 65,536 out (65537 exclusive). Keeping these accurate
# matters: Copilot uses maxInputTokens to drive its rolling-window /
# summarization heuristics — understating it makes the agent truncate context
# 5x too early; overstating it causes hard API 400s mid-conversation.
vertex_models = [
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash (Vertex ADC)",
     "maxInputTokens": 1048576, "maxOutputTokens": 65536},
    {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview (Vertex ADC)",
     "maxInputTokens": 2097152, "maxOutputTokens": 65536},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Vertex ADC)",
     "maxInputTokens": 1000000, "maxOutputTokens": 128000},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8 (Vertex ADC)",
     "maxInputTokens": 1000000, "maxOutputTokens": 128000},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5 (Vertex ADC)",
     "maxInputTokens": 1000000, "maxOutputTokens": 128000},
    {"id": "claude-fable-5", "name": "Claude Fable 5 (Vertex ADC)",
     "maxInputTokens": 1000000, "maxOutputTokens": 128000},
]
for m in vertex_models:
    m.update({"url": url, "toolCalling": True, "vision": True,
              "thinking": True, "streaming": True})

vertex_block = {
    "name": "Hussh One Vertex ADC",
    "vendor": "customendpoint",
    # `customendpoint` reads credentials at the provider level.  Model-level
    # apiKey/headers fields are ignored by current VS Code Insiders builds,
    # which results in a request with no Authorization header at all.
    # Keep the literal loopback-only key here instead of a VS Code secret-store
    # reference: the latter can disappear after a keychain or editor update.
    "apiKey": master_key,
    "apiType": "chat-completions",
    "models": vertex_models,
}

# Preserve any existing non-Vertex endpoints (e.g. LM Studio) verbatim.
existing = []
if os.path.exists(target):
    try:
        with open(target) as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []

merged = [b for b in existing
          if isinstance(b, dict) and b.get("name") != "Hussh One Vertex ADC"]
merged.append(vertex_block)

os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w") as f:
    json.dump(merged, f, indent=2)
print(f"  wrote {target}")
PY
}

# ── 6b. VS Code Plan-agent fix (disable auto "Start Implementation" on switch) ─
# The built-in Copilot "Plan" agent ships a handoff with send:true, so flipping
# Agent→Plan can auto-fire "Start implementation" to the coding agent without a
# click. We ship a custom Plan agent (same name → overrides the built-in) whose
# handoffs use send:false, so implementation only starts when the user clicks.
write_plan_agent() {
local user_dir="$1" edition="$2"
[[ -d "$user_dir" ]] || return 0
local target="$user_dir/prompts/Plan.agent.md"
if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run: would write $target ($edition)"
  return 0
fi
mkdir -p "$user_dir/prompts"
cat > "$target" <<'AGENT'
---
name: Plan
description: 'Research and write an implementation plan with read-only tools. Hands off to Agent mode ONLY when you click a button — never auto-sends on mode switch.'
tools: ['codebase', 'search', 'usages', 'fetch', 'githubRepo', 'findTestFiles', 'searchResults', 'read', 'problems', 'changes']
handoffs:
- label: Start Implementation
  agent: agent
  prompt: 'Implement the plan above.'
  send: false
- label: Continue with Agent Mode
  agent: agent
  prompt: 'You are now switching to Agent Mode, where you can read and edit any file in the codebase. Continue with the task without losing the plan context above.'
  send: false
---
You are a PLAN AGENT — a senior engineer who researches the codebase and writes a precise, actionable implementation plan. You operate with read-only tools and DO NOT edit code in this mode.

Your job each turn:
1. Understand the goal. Ask one clarifying question only if the goal is genuinely ambiguous; otherwise proceed.
2. Research the actual repository before planning — read the real files, routes, services, and tests involved. Ground every step in paths that exist. Never invent file names or APIs.
3. Produce a forward-looking plan:
 - Lead with any Decisions to lock (blocking choices + your recommended default).
 - Then phased tasks, each with exact target file paths and the concrete change/shape to build.
 - Include validation steps and risks/guardrails.
4. Keep the plan tight and concrete — reproduce the shape of existing code, not the whole code.

DO NOT begin editing or implementing. When the plan is ready, stop and let the user review. Implementation happens only after the user explicitly clicks Start Implementation (which hands off to Agent mode). Switching into Plan mode must never itself trigger implementation.
AGENT
echo "  wrote $target"
}

# ── 6c. Smart-tool-usage custom instructions (native tools > shell/subagents) ─
# BYOK models (Vertex Claude / Gemini) are NOT Copilot's default GPT models and
# aren't tuned for Copilot's tool ecosystem, so in agent mode they tend to
# (a) over-spawn subagents for routine work and (b) shell out `cat`/`sed`/`grep`
# instead of using the native read/edit/search tools. We ship an ALWAYS-ON
# user-level custom instructions file (highest priority per VS Code's instruction
# precedence) that steers every agent/model toward smart native-tool usage.
#
# Location: ~/.copilot/instructions/ is a documented VS Code user-profile
# instructions location (scanned by default; also read by the Copilot CLI). One
# home-level file covers BOTH editions and ALL workspaces — no per-edition dupes.
# applyTo: '**' makes it always-on. This is advisory steering, not a hard tool
# restriction, so it never breaks a legitimate workflow.
write_copilot_instructions() {
  local dst_dir="$HOME/.copilot/instructions"
  local target="$dst_dir/hussh-one-tooling.instructions.md"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: would write $target"
    return 0
  fi
  mkdir -p "$dst_dir"
  cat > "$target" <<'INSTR'
---
name: Hussh One — Smart Tool Usage
description: Native-tool-first behavior for all agents and models (esp. BYOK Vertex Claude/Gemini via Copilot).
applyTo: '**'
---
# Smart tool usage (always on)

You are in agent mode, often on a BYOK model (Vertex Claude / Gemini) that is
NOT Copilot's default model and is NOT tuned for this tool ecosystem. Use the
RIGHT built-in tool for each job. Do not fall back to shell commands or
subagents for work a native tool does better.

## Read/search with native tools, never the terminal
- View file contents with the built-in read/file tool.
- NEVER run `cat`, `head`, `tail`, `less`, `more`, or `type` to read a file.
- Find files with the file-search / glob tool, not `ls` or `find`.
- Search code with the codebase / text-search tool, not `grep` or `rg`.

## Edit with the edit / apply-patch tools, never the shell
- Apply changes with the built-in edit / apply-patch / insert-edit tools.
- NEVER edit files via `sed`, `awk`, `echo >`, `cat <<EOF`, or `tee`.
- Reserve the terminal for what genuinely needs a shell: builds, installs,
  running tests, git, package managers, starting/stopping processes.

## Do not over-delegate to subagents
- Handle routine work (reading a file, making one edit, running one command)
  YOURSELF in the main thread. Do NOT spawn a subagent/delegate for it.
- Delegate ONLY when work is large, independent, and parallelizable — several
  unrelated investigations that each need their own context.
- One well-scoped tool call beats a subagent round-trip. Prefer the direct path.

## General
- Pick the most specific tool available before reaching for a general one.
- Batch independent tool calls; don't serialize what can run in parallel.
- Be deterministic and verify results; never claim success without checking output.
INSTR
  echo "  wrote $target"
}

if [[ "$WRITE_VSCODE" == "1" ]]; then
  # Home-level, edition- and OS-independent — write once.
  write_copilot_instructions
  case "$(uname -s)" in
    Darwin)
      write_vscode_config "$HOME/Library/Application Support/Code - Insiders/User" "Insiders"
      write_vscode_config "$HOME/Library/Application Support/Code/User" "Stable"
      write_plan_agent "$HOME/Library/Application Support/Code - Insiders/User" "Insiders"
      write_plan_agent "$HOME/Library/Application Support/Code/User" "Stable"
      ;;
    Linux)
      write_vscode_config "$HOME/.config/Code - Insiders/User" "Insiders"
      write_vscode_config "$HOME/.config/Code/User" "Stable"
      write_plan_agent "$HOME/.config/Code - Insiders/User" "Insiders"
      write_plan_agent "$HOME/.config/Code/User" "Stable"
      ;;
    *) warn "Unknown OS; skipping VS Code config. Point Copilot at $url manually." ;;
  esac
fi

# ── 7. Optional start + smoke test ───────────────────────────────────────────
listening() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1 || (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# launchd KeepAlive agents → instant (<1s) restart on crash/OOM/sleep. This is
# the graceful-UX backbone: the shim retries across the restart window, so a
# proxy OOM becomes a sub-second hiccup the client never sees as an error.
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PROXY_LABEL="ai.hushh.one.litellm-proxy"
SHIM_LABEL="ai.hushh.one.litellm-shim"

write_launchd_plist() {
  local label="$1" launcher="$2" logf="$3"
  local plist="$LAUNCHD_DIR/$label.plist"
  mkdir -p "$LAUNCHD_DIR"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$launcher</string>
  </array>
  <!-- Instant restart on death (OOM/crash) and on any non-zero exit. -->
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>RunAtLoad</key><true/>
  <!-- Throttle restart storms but stay fast (1s). -->
  <key>ThrottleInterval</key><integer>1</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$logf</string>
  <key>StandardErrorPath</key><string>$logf</string>
</dict>
</plist>
EOF
  echo "$plist"
}

bootout_label() {  # tolerant unload (ignore "not loaded")
  local label="$1"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || \
    launchctl unload "$LAUNCHD_DIR/$label.plist" >/dev/null 2>&1 || true
}

bootstrap_label() {
  local label="$1" plist="$2"
  launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || \
    launchctl load "$plist" >/dev/null 2>&1 || true
}

install_launchd() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    warn "--launchd is macOS-only; falling back to background start."
    return 1
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: would install launchd agents $PROXY_LABEL and $SHIM_LABEL"
    return 0
  fi
  log "Installing launchd KeepAlive agents (instant restart) ..."
  # Stop any hand-started copies so launchd owns the ports cleanly.
  pkill -f "litellm.*--port $PROXY_PORT" >/dev/null 2>&1 || true
  pkill -f "litellm_auth_shim.py" >/dev/null 2>&1 || true
  sleep 1
  local pp sp
  pp="$(write_launchd_plist "$PROXY_LABEL" "$LAUNCHER_PROXY" "$HERMES_HOME/logs/litellm-proxy.log")"
  sp="$(write_launchd_plist "$SHIM_LABEL" "$LAUNCHER_SHIM" "$HERMES_HOME/logs/litellm-shim.log")"
  bootout_label "$PROXY_LABEL"; bootstrap_label "$PROXY_LABEL" "$pp"
  # Give the proxy a head start so the shim's first health probe sees it.
  sleep 2
  bootout_label "$SHIM_LABEL"; bootstrap_label "$SHIM_LABEL" "$sp"
  return 0
}

started_via_launchd=0
if [[ "$USE_LAUNCHD" == "1" ]]; then
  if install_launchd; then
    started_via_launchd=1
    START_SERVICES=1   # imply start so the smoke test runs
  fi
fi

if [[ "$START_SERVICES" == "1" && "$DRY_RUN" != "1" ]]; then
  restart_services() {
    if [[ "$started_via_launchd" == "1" ]]; then
      bootout_label "$PROXY_LABEL"; bootstrap_label "$PROXY_LABEL" "$LAUNCHD_DIR/$PROXY_LABEL.plist"
      sleep 2
      bootout_label "$SHIM_LABEL"; bootstrap_label "$SHIM_LABEL" "$LAUNCHD_DIR/$SHIM_LABEL.plist"
    else
      pkill -f "litellm.*--port $PROXY_PORT" >/dev/null 2>&1 || true
      pkill -f "litellm_auth_shim.py" >/dev/null 2>&1 || true
      sleep 1
      nohup_start "$LAUNCHER_PROXY" "$HERMES_HOME/logs/litellm-proxy.log"
      nohup_start "$LAUNCHER_SHIM" "$HERMES_HOME/logs/litellm-shim.log"
    fi
    for _ in $(seq 1 30); do
      if listening "$PROXY_PORT" && listening "$SHIM_PORT"; then break; fi
      sleep 1
    done
  }
  nohup_start() {
    local launcher="$1" logf="$2"
    ( setsid bash "$launcher" >>"$logf" 2>&1 & ) 2>/dev/null || \
      ( bash "$launcher" >>"$logf" 2>&1 & )
  }
  if [[ "$started_via_launchd" != "1" ]]; then
    log "Starting proxy + shim ..."
    if ! listening "$PROXY_PORT"; then
      nohup_start "$LAUNCHER_PROXY" "$HERMES_HOME/logs/litellm-proxy.log"
    fi
    if ! listening "$SHIM_PORT"; then
      nohup_start "$LAUNCHER_SHIM" "$HERMES_HOME/logs/litellm-shim.log"
    fi
  fi
  # wait up to ~30s for both
  for _ in $(seq 1 30); do
    if listening "$PROXY_PORT" && listening "$SHIM_PORT"; then break; fi
    sleep 1
  done
  log "Smoke test (auth + chat through shim) ..."
  smoke_test() {
    SMOKE_KEY="$KEY" SMOKE_PORT="$SHIM_PORT" ALLOW_ANONYMOUS="$ALLOW_UNAUTHENTICATED_LOOPBACK" python3 - <<'PY'
import json, os, urllib.request, urllib.error
key = os.environ["SMOKE_KEY"]; port = os.environ["SMOKE_PORT"]
allow_anonymous = os.environ.get("ALLOW_ANONYMOUS") == "1"
base = f"http://127.0.0.1:{port}"
def code(path, auth, method="GET", data=None):
    req = urllib.request.Request(base+path, data=data, method=method)
    if data: req.add_header("Content-Type", "application/json")
    if auth is not None: req.add_header("Authorization", "Bearer "+auth)
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status
    except urllib.error.HTTPError as e: return e.code
    except Exception as e: return f"ERR {type(e).__name__}"
body = json.dumps({"model":"gemini-3.5-flash","messages":[{"role":"user","content":"reply OK"}]}).encode()
noauth = code("/v1/models", None)
chat = code("/v1/chat/completions", key, "POST", body)
expected_noauth = 200 if allow_anonymous else 401
ok = (noauth == expected_noauth and chat == 200)
print(f"  no-auth->{noauth} (want {expected_noauth}), chat->{chat} (want 200): {'PASS' if ok else 'FAIL'}")
raise SystemExit(0 if ok else 1)
PY
  }
  # ── Tool-calling smoke test — every model registered on the endpoint must
  # natively round-trip an OpenAI-format `tools` request the way VS Code
  # Copilot's MCP tool-calling path sends them (function name + JSON-schema
  # parameters in, `tool_calls[].function` back). This is what "onboarding a
  # new model" must prove before it ships, so a model that silently drops
  # tool support (or mangles the schema) never reaches Copilot unannounced.
  tool_call_smoke_test() {
    SMOKE_KEY="$KEY" SMOKE_PORT="$SHIM_PORT" python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
key = os.environ["SMOKE_KEY"]; port = os.environ["SMOKE_PORT"]
base = f"http://127.0.0.1:{port}"

def call(model, tool, prompt):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [tool],
        "tool_choice": "auto",
        "max_tokens": 300,
    }).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read().decode())
        msg = data.get("choices", [{}])[0].get("message", {})
        calls = msg.get("tool_calls") or []
        return [c.get("function", {}).get("name") for c in calls]

# Test 1: property-level anyOf (a value can be one of several types) — the
# shape Vertex has always handled fine on both Gemini and Claude.
property_anyof_tool = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                    "description": "City name or postal code",
                }
            },
            "required": ["location"],
        },
    },
}

# Test 2: ROOT-LEVEL anyOf (conditional "provide field A OR field B")  — the
# real shape used by production MCP tools (e.g. hushh-consent's
# check_consent_status: "must provide scope OR request_id"). Vertex's Gemini
# function-calling validator hard-400s on this UNLESS the shim's
# _scrub_tools_for_gemini() strips it first (see litellm_auth_shim.py). This
# is the regression that broke real Copilot sessions in production — this
# test exists specifically so it can never reach Copilot silently again.
root_anyof_tool = {
    "type": "function",
    "function": {
        "name": "check_consent_status",
        "description": "Check consent status for a user/scope pair or a specific request id.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "The user id"},
                "scope": {"type": "string", "description": "The scope requested"},
                "request_id": {"type": "string", "description": "The request id"},
            },
            "required": ["user_id"],
            "anyOf": [{"required": ["scope"]}, {"required": ["request_id"]}],
        },
    },
}

models = os.environ.get("SMOKE_MODELS", "").split(",")
models = [m for m in models if m.strip()]
failures = []
for model in models:
    try:
        names = call(model, property_anyof_tool, "What's the weather in Tokyo right now? Use the tool.")
        ok = "get_weather" in names
        print(f"  {model:<28} property-anyOf  tool_call={'PASS' if ok else 'FAIL'} names={names}")
        if not ok:
            failures.append(f"{model}:property-anyOf")
    except Exception as e:
        print(f"  {model:<28} property-anyOf  tool_call=FAIL exception={type(e).__name__}: {e}")
        failures.append(f"{model}:property-anyOf")

    try:
        names = call(model, root_anyof_tool, "Check consent status for user 12345, scope=basic_profile. Use the tool.")
        ok = "check_consent_status" in names
        print(f"  {model:<28} root-anyOf      tool_call={'PASS' if ok else 'FAIL'} names={names}")
        if not ok:
            failures.append(f"{model}:root-anyOf")
    except Exception as e:
        print(f"  {model:<28} root-anyOf      tool_call=FAIL exception={type(e).__name__}: {e}")
        failures.append(f"{model}:root-anyOf")
if failures:
    print(f"  Tool-calling FAILED for: {', '.join(failures)}")
    sys.exit(1)
PY
  }
  if ! smoke_test; then
    # ── Graceful rollback: the new model/config broke the stack. Restore the
    # last-known-good proxy config + shim (if a backup exists) and retry once
    # before giving up, so a single bad model entry never leaves Copilot dead.
    rolled_back=0
    if [[ -f "$PROXY_CONFIG_BAK" ]]; then
      warn "Smoke test failed — rolling back litellm-proxy-config.yaml to last known good."
      cp "$PROXY_CONFIG_BAK" "$PROXY_CONFIG"
      rolled_back=1
    fi
    if [[ -f "$SHIM_DST_BAK" ]]; then
      warn "Smoke test failed — rolling back litellm_auth_shim.py to last known good."
      cp "$SHIM_DST_BAK" "$SHIM_DST"
      rolled_back=1
    fi
    if [[ "$rolled_back" == "1" ]]; then
      warn "Restarting proxy + shim on the rolled-back config ..."
      restart_services
      if smoke_test; then
        err "New config was broken and has been rolled back. Fix the model entry (check $ASSETS/litellm-proxy-config.template.yaml) and re-run."
        exit 1
      else
        err "Rollback restart ALSO failed the smoke test — services may need manual attention (check $HERMES_HOME/logs/litellm-proxy.log and litellm-shim.log)."
        exit 1
      fi
    else
      err "Smoke test failed and no backup was available to roll back to. Check $HERMES_HOME/logs/litellm-proxy.log and litellm-shim.log."
      exit 1
    fi
  fi
  # ── Tool-calling gate (runs only once the base smoke test has passed) ──────
  # Every model we ship to Copilot must prove native OpenAI-format tool
  # calling round-trips correctly through shim -> LiteLLM -> Vertex, using
  # the same anyOf-bearing schema shape Copilot's real MCP tools send. This
  # is the "graceful onboarding" gate for tool support specifically — it
  # never blocks setup (a single flaky/no-tool-support model shouldn't take
  # down every other model), but any FAIL is surfaced loudly so a model
  # that can't do native tool calling is never silently handed to Copilot.
  SMOKE_MODELS="gemini-3.5-flash,gemini-3.1-pro-preview,claude-sonnet-4-6,claude-opus-4-8,claude-sonnet-5,claude-fable-5"
  log "Tool-calling smoke test (native OpenAI tools -> tool_calls, anyOf schema) ..."
  if ! SMOKE_MODELS="$SMOKE_MODELS" tool_call_smoke_test; then
    warn "One or more models FAILED native tool calling — see above. Setup continues,"
    warn "but do NOT rely on that model for MCP tool use in Copilot until fixed."
  fi
fi

log ""
log "VS Code Copilot BYOK (Vertex ADC) setup complete."
log "  Endpoint URL : http://127.0.0.1:$SHIM_PORT/v1   (the auth shim)"
log "  Local auth   : configured automatically in the VS Code endpoint"
log "  Models       : gemini-3.5-flash, gemini-3.1-pro-preview, claude-sonnet-4-6, claude-opus-4-8"
if [[ "$started_via_launchd" == "1" ]]; then
  log "  Resilience   : launchd KeepAlive ($PROXY_LABEL, $SHIM_LABEL) — instant restart"
fi
log ""
log "In VS Code: Developer: Reload Window, choose a 'Hussh One Vertex ADC' model,"
log "and use its preconfigured local authentication. If VS Code prompts for a key,"
log "reload/restart VS Code instead of entering a blank value."
if [[ "$started_via_launchd" != "1" ]]; then
  log "Tip: re-run with --launchd for instant crash/OOM restart (recommended). The"
  log "reaper watchdog (ensure_litellm_proxy) also self-heals both services."
fi
