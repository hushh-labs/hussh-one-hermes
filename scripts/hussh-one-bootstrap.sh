#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Idempotent setup for a fresh Hussh One Hermes clone.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MANAGER="${HUSSH_ONE_SUPERVISOR:-auto}"
START_SERVICES=0
SKIP_INSTALL=0
SKIP_BUILD=0
LIVE_SMOKE=0
CLEAN_CONFLICTS=0
SETUP_COPILOT="${HUSSH_ONE_SETUP_COPILOT:-auto}"
SETUP_OPEN_WEBUI="${HUSSH_ONE_SETUP_OPEN_WEBUI:-auto}"
DRY_RUN="${HUSSH_ONE_DRY_RUN:-0}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-bootstrap.sh [options]

Options:
  --manager auto|launchd|systemd|s6|screen
  --start                   Start/restart Hussh One services after setup
  --skip-install            Do not create/update the Python environment
  --skip-build              Do not build TUI/dashboard frontend assets
  --live-smoke              Run optional live Vertex smoke checks from doctor
  --clean-conflicts         Let supervisor clean stale conflicting sessions
  --copilot                 Force VS Code Copilot BYOK setup when prerequisites exist
  --no-copilot              Skip VS Code Copilot BYOK setup
  --open-webui              Force Open WebUI companion setup
  --no-open-webui           Skip Open WebUI companion setup
  --dry-run                 Print actions without mutating the machine
  -h, --help                Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manager)
      MANAGER="${2:-}"
      shift 2
      ;;
    --start)
      START_SERVICES=1
      shift
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --live-smoke)
      LIVE_SMOKE=1
      shift
      ;;
    --clean-conflicts)
      CLEAN_CONFLICTS=1
      shift
      ;;
    --copilot)
      SETUP_COPILOT=1
      shift
      ;;
    --no-copilot)
      SETUP_COPILOT=0
      shift
      ;;
    --open-webui)
      SETUP_OPEN_WEBUI=1
      shift
      ;;
    --no-open-webui)
      SETUP_OPEN_WEBUI=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MANAGER" in
  auto|launchd|systemd|s6|screen) ;;
  *)
    echo "error: unsupported manager '$MANAGER'" >&2
    exit 2
    ;;
esac

export HERMES_HOME

log() {
  printf '%s\n' "$*"
}

run_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'dry-run:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

python_bin() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/python"
  else
    command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null || true
  fi
}

hermes_bin() {
  if [[ -x "$REPO_ROOT/.venv/bin/hermes" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/hermes"
  elif command -v hermes >/dev/null 2>&1; then
    command -v hermes
  else
    printf '%s\n' "$REPO_ROOT/.venv/bin/hermes"
  fi
}

ensure_venv() {
  if [[ "$SKIP_INSTALL" == "1" ]]; then
    log "Python install skipped."
    return 0
  fi

  if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    if command -v uv >/dev/null 2>&1; then
      run_cmd uv venv --python 3.11 .venv
    elif command -v python3.11 >/dev/null 2>&1; then
      run_cmd python3.11 -m venv .venv
    elif command -v python3 >/dev/null 2>&1; then
      run_cmd python3 -m venv .venv
    else
      echo "error: Python 3 is required to create .venv" >&2
      exit 1
    fi
  fi

  if command -v uv >/dev/null 2>&1; then
    run_cmd uv pip install -e ".[all,dev]"
  else
    run_cmd "$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
    run_cmd "$REPO_ROOT/.venv/bin/python" -m pip install -e ".[all,dev]"
  fi
}

build_node_project() {
  local dir="$1"
  local script="$2"
  if [[ ! -f "$dir/package.json" ]]; then
    return 0
  fi
  if ! command -v npm >/dev/null 2>&1; then
    warn "npm is not installed; skipping $dir build"
    return 0
  fi
  if [[ ! -d "$dir/node_modules" ]]; then
    run_cmd npm --prefix "$dir" install
  fi
  run_cmd npm --prefix "$dir" run "$script"
}

build_assets() {
  if [[ "$SKIP_BUILD" == "1" ]]; then
    log "Frontend build skipped."
    return 0
  fi
  build_node_project ui-tui build
  build_node_project web build
}

configure_web_search() {
  local hermes
  hermes="$(hermes_bin)"
  if [[ "$DRY_RUN" != "1" && ! -x "$hermes" ]]; then
    warn "Hermes binary unavailable; web search was not configured"
    return 0
  fi

  # Hermes deliberately hides web_search until a real provider is available.
  # Use its bundled, no-key DDGS plugin for a working fresh-install baseline.
  # Every step is non-fatal: a restricted network must not block Hermes setup.
  if ! run_cmd "$hermes" plugins enable web-ddgs; then
    warn "Could not enable the bundled DuckDuckGo search plugin; configure web search with 'hermes tools'."
    return 0
  fi
  if ! run_cmd "$hermes" tools post-setup ddgs; then
    warn "Could not install the DuckDuckGo search dependency; configure web search with 'hermes tools'."
    return 0
  fi
  if ! run_cmd "$hermes" config set web.search_backend ddgs; then
    warn "Could not select the DuckDuckGo search backend; configure web search with 'hermes tools'."
  fi
}

set_config_defaults() {
  local hermes
  hermes="$(hermes_bin)"
  if [[ "$DRY_RUN" != "1" && ! -x "$hermes" ]]; then
    warn "Hermes binary unavailable; config defaults were not written"
    return 0
  fi
  run_cmd "$hermes" config set display.skin hussh-one
  run_cmd "$hermes" config set dashboard.theme hussh-one
  run_cmd "$hermes" config set model.provider gemini
  run_cmd "$hermes" config set model.default gemini-3.6-flash
  run_cmd "$hermes" config set agent.reasoning_effort high
  run_cmd "$hermes" config set display.show_reasoning true
  # Compact sessions well before the dashboard memory ceiling so a long Hussh
  # One session can't balloon to 200k+ tokens / 500+ msgs and get OOM-killed
  # (SIGKILL rc=-9) mid-write — which surfaces only as "connection lost". Pairs
  # with the supervisor RSS soft-cap (HUSSH_ONE_DASHBOARD_MEM_CAP_MB).
  run_cmd "$hermes" config set compression.threshold 0.35
  run_cmd "$hermes" config set compression.hygiene_hard_message_limit 250

  # 🤫 Hussh One specific robust defaults
  run_cmd "$hermes" config set approvals.mode false
  run_cmd "$hermes" config set whatsapp.require_mention_on_replies true
  run_cmd "$hermes" config set display.platforms.whatsapp.tool_progress off
  run_cmd "$hermes" config set display.platforms.whatsapp.show_reasoning false
  run_cmd "$hermes" config set display.interim_assistant_messages false
  # The Source Library remains a local Desktop/dashboard-only capability. This
  # explicit default gives operators a durable off switch without ever adding
  # the toolset to messaging-platform configuration.
  run_cmd "$hermes" config set hussh_one.source_library.enabled true

  configure_web_search
}

configure_hussh_persona() {
  local source="$REPO_ROOT/docs/hussh-one/persona/SOUL.md"
  local destination="$HERMES_HOME/SOUL.md"
  local stock_persona
  stock_persona="You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools. You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over being verbose unless otherwise directed below. Be targeted and efficient in your exploration and investigations."

  if [[ ! -f "$source" ]]; then
    warn "Canonical Hussh One persona is missing; preserving existing SOUL.md"
    return 0
  fi

  if [[ ! -f "$destination" ]] \
    || grep -q '<!-- hussh-one-persona:v1 -->' "$destination" \
    || [[ "$(tr -d '\r\n' < "$destination")" == "$stock_persona" ]]; then
    run_cmd mkdir -p "$HERMES_HOME"
    run_cmd cp "$source" "$destination"
    log "Configured the canonical Hussh One persona"
    return 0
  fi

  warn "Preserving a customized SOUL.md; merge the canonical Hussh One persona manually if desired"
}

persist_env_secret() {
  local key="$1" value="$2" python file
  python="$(python_bin)"
  file="$HERMES_HOME/.env"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: persist $key in the mode-0600 Hermes secret file"
    return 0
  fi
  if [[ -z "$python" || ! -x "$python" ]]; then
    warn "Cannot persist $key: repository Python is unavailable"
    return 1
  fi
  run_cmd mkdir -p "$HERMES_HOME"
  "$python" - "$file" "$key" 3<<<"$value" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

destination = Path(sys.argv[1])
key = sys.argv[2]
with os.fdopen(3, encoding="utf-8") as secret_input:
    value = secret_input.read().strip()
if not value or "\n" in value or "\r" in value:
    raise SystemExit(f"refusing to persist malformed {key}")

lines = destination.read_text(encoding="utf-8").splitlines() if destination.exists() else []
replacement = f"{key}={value}"
updated: list[str] = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            updated.append(replacement)
            replaced = True
        continue
    updated.append(line)
if not replaced:
    if updated and updated[-1]:
        updated.append("")
    updated.append(replacement)

destination.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(updated) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, destination)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
}

bootstrap_hussh_consent_token() {
  local python token
  if [[ -n "$(env_value HUSHH_CONSENT_MCP_TOKEN)" ]]; then
    log "Hussh Consent MCP credential: configured in the active Hermes profile."
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: retrieve the one-time Hussh Technologies MCP credential from GCP Secret Manager"
    persist_env_secret HUSHH_CONSENT_MCP_TOKEN '<gcp-secret>'
    return 0
  fi
  python="$(python_bin)"
  if [[ -z "$python" || ! -x "$python" ]]; then
    warn "Hussh Consent MCP credential is missing and repository Python is unavailable"
    return 0
  fi

  # This is intentionally a one-time machine bootstrap. The dedicated partner
  # credential remains in Secret Manager and is copied only into the active
  # profile's mode-0600 .env. It is never written into Git or MCP config.
  if ! token="$("$python" - <<'PY'
import base64
import os

try:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
except ImportError as exc:
    raise SystemExit(f"google-auth unavailable: {exc}") from exc

project = (
    os.getenv("GOOGLE_CLOUD_PROJECT")
    or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    or "hushh-pda-uat"
).strip()
try:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    url = (
        "https://secretmanager.googleapis.com/v1/projects/"
        f"{project}/secrets/HUSHH_TECHNOLOGIES_PARTNER_MCP_TOKEN/versions/latest:access"
    )
    response = session.get(url, timeout=20)
    response.raise_for_status()
    token = base64.b64decode(response.json()["payload"]["data"]).decode().strip()
except Exception as exc:
    raise SystemExit(f"credential bootstrap unavailable: {exc}") from exc
print(token)
PY
  )"; then
    warn "Hussh Consent MCP credential is missing; existing GCP ADC could not access the dedicated Hussh Technologies secret"
    return 0
  fi
  if [[ -z "$token" ]]; then
    warn "Hussh Consent MCP credential is missing; GCP Secret Manager returned an empty value"
    return 0
  fi
  if ! persist_env_secret HUSHH_CONSENT_MCP_TOKEN "$token"; then
    return 0
  fi
  log "Hussh Consent MCP credential: securely bootstrapped from GCP Secret Manager."
}

configure_hussh_consent_connector() {
  local hermes token
  hermes="$(hermes_bin)"

  bootstrap_hussh_consent_token

  # Keep the hosted streamable MCP as the lifecycle source of truth. Hermes'
  # transport boundary owns the local X25519 identity and decrypts approved
  # envelopes into one-time leases; config contains neither token nor key.
  run_cmd "$hermes" mcp remove hushh_consent >/dev/null 2>&1 || true
  run_cmd "$hermes" config set --force \
    mcp_servers.hushh_consent.url "https://api.uat.hushh.ai/mcp/"
  run_cmd "$hermes" config set --force \
    mcp_servers.hushh_consent.headers.Authorization \
    'Bearer ${HUSHH_CONSENT_MCP_TOKEN}'

  if command -v codex >/dev/null 2>&1; then
    run_cmd codex mcp remove hushh_consent >/dev/null 2>&1 || true
    run_cmd codex mcp add hushh_consent \
      --url "https://api.uat.hushh.ai/mcp/" \
      --bearer-token-env-var HUSHH_CONSENT_MCP_TOKEN
    token="$(env_value HUSHH_CONSENT_MCP_TOKEN)"
    if [[ -n "$token" && "$(uname -s)" == "Darwin" ]]; then
      run_cmd launchctl setenv HUSHH_CONSENT_MCP_TOKEN "$token"
    fi
  fi
}

env_value() {
  local key="$1"
  local file="$HERMES_HOME/.env"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$key" '
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      gsub(/^["'\'']|["'\'']$/, "", value)
      print value
      exit
    }
  ' "$file"
}

safe_suffix() {
  local value="$1"
  if [[ -z "$value" ]]; then
    printf '<unset>'
  elif [[ ${#value} -le 6 ]]; then
    printf '***'
  else
    printf '***%s' "${value: -6}"
  fi
}

check_gcp_adc() {
  local env_project gcloud_project token_ok
  env_project="$(env_value GOOGLE_CLOUD_PROJECT)"
  if [[ -z "$env_project" ]]; then
    env_project="$(env_value GCP_PROJECT)"
  fi

  if ! command -v gcloud >/dev/null 2>&1; then
    warn "gcloud is not installed; Vertex Claude ADC readiness could not be checked"
    return 0
  fi

  gcloud_project="$(gcloud config get-value project 2>/dev/null || true)"
  if [[ -n "$gcloud_project" ]]; then
    log "GCP active project: $(safe_suffix "$gcloud_project")"
  else
    warn "gcloud has no active project; run: gcloud config set project <project-id>"
  fi

  if [[ -n "$env_project" && -n "$gcloud_project" && "$env_project" != "$gcloud_project" ]]; then
    warn "GCP project selector in $HERMES_HOME/.env differs from gcloud active project ($(safe_suffix "$env_project") vs $(safe_suffix "$gcloud_project"))"
  fi

  if gcloud auth application-default print-access-token >/dev/null 2>&1; then
    token_ok=1
  else
    token_ok=0
  fi
  if [[ "$token_ok" == "1" ]]; then
    log "Vertex ADC: application-default credentials are available."
  else
    warn "Vertex ADC missing; run: gcloud auth application-default login"
  fi
}

check_whatsapp_pairing() {
  local session_dir="$HERMES_HOME/whatsapp/session"
  if [[ -f "$session_dir/creds.json" ]]; then
    log "WhatsApp pairing: session credentials found."
  else
    warn "WhatsApp pairing is per-machine and not complete here; start the bridge and scan the QR code before expecting connected health."
  fi
}

setup_copilot_byok() {
  if [[ "$SETUP_COPILOT" == "0" ]]; then
    return 0
  fi
  local editor_dir=""
  for candidate in \
    "$HOME/Library/Application Support/Code - Insiders/User" \
    "$HOME/Library/Application Support/Code/User" \
    "$HOME/.config/Code - Insiders/User" \
    "$HOME/.config/Code/User"; do
    if [[ -d "$candidate" ]]; then
      editor_dir="$candidate"
      break
    fi
  done
  if [[ -z "$editor_dir" ]]; then
    if [[ "$SETUP_COPILOT" == "1" ]]; then
      warn "No supported VS Code user profile found; Copilot BYOK was skipped"
    else
      log "Copilot BYOK skipped: no supported VS Code installation found."
    fi
    return 0
  fi
  if ! command -v gcloud >/dev/null 2>&1 \
    || ! gcloud auth application-default print-access-token >/dev/null 2>&1 \
    || [[ -z "$(gcloud config get-value project 2>/dev/null || true)" ]]; then
    warn "Copilot BYOK skipped: Vertex ADC and an active GCP project are required"
    return 0
  fi

  local args=("$SCRIPT_DIR/hussh-one-copilot-setup.sh" --start --allow-unauthenticated-loopback)
  if [[ "$(uname -s)" == "Darwin" ]]; then
    args+=(--launchd)
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    args+=(--dry-run)
  fi
  log "Setting up VS Code Copilot BYOK (Vertex ADC) for $(basename "$(dirname "$editor_dir")") ..."
  if ! run_cmd "${args[@]}"; then
    warn "Copilot BYOK setup failed; Hermes setup will continue. Re-run scripts/hussh-one-copilot-setup.sh after fixing the prerequisite."
  fi
}

setup_open_webui() {
  if [[ "$SETUP_OPEN_WEBUI" == "0" ]]; then
    return 0
  fi
  local hermes
  hermes="$(hermes_bin)"
  if [[ "$DRY_RUN" != "1" && ! -x "$hermes" ]]; then
    warn "Open WebUI skipped: repository Hermes binary is unavailable"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    warn "Open WebUI skipped: python3 is unavailable"
    return 0
  fi
  log "Setting up Open WebUI companion service ..."
  if ! run_cmd env \
    "HERMES_HOME=$HERMES_HOME" \
    "HERMES_BIN=$hermes" \
    OPEN_WEBUI_ENABLE_SERVICE=auto \
    "$SCRIPT_DIR/setup_open_webui.sh"; then
    warn "Open WebUI setup failed; Hermes setup will continue. Re-run scripts/setup_open_webui.sh after fixing the prerequisite."
  fi
}

install_managed_doctor() {
  local python
  python="$(python_bin)"
  if [[ -z "$python" || ( "$DRY_RUN" != "1" && ! -x "$python" ) ]]; then
    warn "Managed Hussh doctor skipped: repository Python is unavailable"
    return 0
  fi
  local args=(
    "$SCRIPT_DIR/hussh_one_doctor_install.py"
    --repo-root "$REPO_ROOT"
    --hermes-home "$HERMES_HOME"
    --python-bin "$python"
  )
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: install managed Hussh doctor into $HERMES_HOME/scripts"
    return 0
  fi
  if ! run_cmd "$python" "${args[@]}"; then
    warn "Managed Hussh doctor installation failed; Hermes setup will continue. Re-run bootstrap after fixing Python."
  fi
}

start_services() {
  local args=("$SCRIPT_DIR/hussh-one-supervisor.sh" restart --manager "$MANAGER")
  if [[ "$CLEAN_CONFLICTS" == "1" ]]; then
    args+=(--clean-conflicts)
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    args+=(--dry-run)
  fi
  run_cmd "${args[@]}"
}

run_doctor() {
  local args=("$SCRIPT_DIR/hussh-one-doctor.sh" --manager "$MANAGER")
  if [[ "$START_SERVICES" == "1" ]]; then
    args+=(--require-services)
  fi
  if [[ "$LIVE_SMOKE" == "1" ]]; then
    args+=(--live-vertex)
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    args+=(--dry-run)
  fi
  run_cmd "${args[@]}"
}

log "Bootstrapping Hussh One Hermes in $REPO_ROOT"
mkdir -p "$HERMES_HOME"
ensure_venv
install_managed_doctor
build_assets
set_config_defaults
configure_hussh_persona
configure_hussh_consent_connector
check_gcp_adc
check_whatsapp_pairing
setup_copilot_byok
setup_open_webui

if [[ "$START_SERVICES" == "1" ]]; then
  start_services
fi

run_doctor
