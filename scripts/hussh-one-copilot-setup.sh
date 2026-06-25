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
PROXY_PORT=8643
SHIM_PORT=8644

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-copilot-setup.sh [options]

Options:
  --project ID        Vertex/GCP project (default: $GOOGLE_CLOUD_PROJECT or gcloud)
  --start             Start/restart the proxy + shim after setup, then smoke test
  --no-vscode         Do not write VS Code chatLanguageModels.json
  --dry-run           Print actions without mutating the machine
  -h, --help          Show this help

After setup, in VS Code: reload the window (Developer: Reload Window) and pick a
model from the "Hussh One Vertex ADC" endpoint. When prompted for an API key,
paste the key printed at the end (also stored in the launcher).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --start) START_SERVICES=1; shift ;;
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

# ── 5. Materialize assets into ~/.hermes ─────────────────────────────────────
PROXY_CONFIG="$HERMES_HOME/litellm-proxy-config.yaml"
SHIM_DST="$HERMES_HOME/scripts/litellm_auth_shim.py"
LAUNCHER_SHIM="$HERMES_HOME/scripts/start_litellm_shim.sh"

# proxy config (substitute project)
if [[ "$DRY_RUN" != "1" ]]; then
  sed "s/__VERTEX_PROJECT__/$PROJECT/g" "$ASSETS/litellm-proxy-config.template.yaml" > "$PROXY_CONFIG"
  chmod 600 "$PROXY_CONFIG"
  cp "$ASSETS/litellm_auth_shim.py" "$SHIM_DST"
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
  VSCODE_TARGET="$target" SHIM_PORT="$SHIM_PORT" python3 - <<'PY'
import json, os, sys

target = os.environ["VSCODE_TARGET"]
shim_port = os.environ["SHIM_PORT"]
url = f"http://127.0.0.1:{shim_port}/v1"

vertex_models = [
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash (Vertex ADC)",
     "maxInputTokens": 1048576, "maxOutputTokens": 16000},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Vertex ADC)",
     "maxInputTokens": 200000, "maxOutputTokens": 16000},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8 (Vertex ADC)",
     "maxInputTokens": 200000, "maxOutputTokens": 16000},
]
for m in vertex_models:
    m.update({"url": url, "toolCalling": True, "vision": True,
              "thinking": True, "streaming": True})

vertex_block = {
    "name": "Hussh One Vertex ADC",
    "vendor": "customendpoint",
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

if [[ "$WRITE_VSCODE" == "1" ]]; then
  case "$(uname -s)" in
    Darwin)
      write_vscode_config "$HOME/Library/Application Support/Code - Insiders/User" "Insiders"
      write_vscode_config "$HOME/Library/Application Support/Code/User" "Stable"
      ;;
    Linux)
      write_vscode_config "$HOME/.config/Code - Insiders/User" "Insiders"
      write_vscode_config "$HOME/.config/Code/User" "Stable"
      ;;
    *) warn "Unknown OS; skipping VS Code config. Point Copilot at $url manually." ;;
  esac
fi

# ── 7. Optional start + smoke test ───────────────────────────────────────────
listening() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1 || (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

if [[ "$START_SERVICES" == "1" && "$DRY_RUN" != "1" ]]; then
  log "Starting proxy + shim ..."
  nohup_start() {
    local launcher="$1" logf="$2"
    ( setsid bash "$launcher" >>"$logf" 2>&1 & ) 2>/dev/null || \
      ( bash "$launcher" >>"$logf" 2>&1 & )
  }
  if ! listening "$PROXY_PORT"; then
    nohup_start "$LAUNCHER_PROXY" "$HERMES_HOME/logs/litellm-proxy.log"
  fi
  if ! listening "$SHIM_PORT"; then
    nohup_start "$LAUNCHER_SHIM" "$HERMES_HOME/logs/litellm-shim.log"
  fi
  # wait up to ~30s for both
  for _ in $(seq 1 30); do
    if listening "$PROXY_PORT" && listening "$SHIM_PORT"; then break; fi
    sleep 1
  done
  log "Smoke test (auth + chat through shim) ..."
  SMOKE_KEY="$KEY" SMOKE_PORT="$SHIM_PORT" python3 - <<'PY'
import json, os, urllib.request, urllib.error
key = os.environ["SMOKE_KEY"]; port = os.environ["SMOKE_PORT"]
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
ok = (noauth == 401 and chat == 200)
print(f"  no-auth->{noauth} (want 401), chat->{chat} (want 200): {'PASS' if ok else 'FAIL'}")
raise SystemExit(0 if ok else 1)
PY
fi

log ""
log "VS Code Copilot BYOK (Vertex ADC) setup complete."
log "  Endpoint URL : http://127.0.0.1:$SHIM_PORT/v1   (the auth shim)"
log "  API key      : $KEY"
log "  Models       : gemini-3.5-flash, claude-sonnet-4-6, claude-opus-4-8"
log ""
log "In VS Code: Developer: Reload Window, choose a 'Hussh One Vertex ADC' model,"
log "and paste the API key above when prompted. The reaper watchdog keeps both"
log "services alive (ensure_litellm_proxy)."
