#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Fast Hussh One clone/deployment health checks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MANAGER="${HUSSH_ONE_SUPERVISOR:-auto}"
REQUIRE_SERVICES=0
LIVE_VERTEX=0
DRY_RUN="${HUSSH_ONE_DRY_RUN:-0}"
DASHBOARD_URL="${HUSSH_ONE_DASHBOARD_URL:-http://127.0.0.1:9119}"
WHATSAPP_HEALTH_URL="${HUSSH_ONE_WHATSAPP_HEALTH_URL:-http://127.0.0.1:8473/health}"
OPEN_WEBUI_URL="${HUSSH_ONE_OPEN_WEBUI_URL:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
OPEN_WEBUI_LAUNCHER="${HUSSH_ONE_OPEN_WEBUI_LAUNCHER:-$HOME/.local/bin/start-open-webui-hermes.sh}"

FAILURES=0
WARNINGS=0

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-doctor.sh [options]

Options:
  --manager auto|launchd|systemd|s6|screen
  --require-services       Fail if dashboard, WhatsApp, or Open WebUI health is not reachable
  --live-vertex            Run optional live Vertex Claude smoke checks
  --dry-run                Print external service checks without mutating state
  -h, --help               Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manager)
      MANAGER="${2:-}"
      shift 2
      ;;
    --require-services)
      REQUIRE_SERVICES=1
      shift
      ;;
    --live-vertex)
      LIVE_VERTEX=1
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

read_open_webui_setting() {
  local key="$1" config_value launcher_value
  if [[ -r "$HERMES_HOME/open-webui.env" ]]; then
    config_value="$(sed -nE "s/^${key}=([^[:space:]]+).*/\\1/p" "$HERMES_HOME/open-webui.env" | head -n 1)"
    [[ -n "$config_value" ]] && printf '%s\n' "$config_value" && return 0
  fi
  if [[ -r "$OPEN_WEBUI_LAUNCHER" ]]; then
    launcher_value="$(sed -nE "s/^export ${key#OPEN_WEBUI_}=([^[:space:]]+).*/\\1/p" "$OPEN_WEBUI_LAUNCHER" | head -n 1)"
    [[ -n "$launcher_value" ]] && printf '%s\n' "$launcher_value" && return 0
  fi
  return 1
}

if [[ -z "$OPEN_WEBUI_URL" ]]; then
  open_webui_host="$(read_open_webui_setting OPEN_WEBUI_HOST || true)"
  open_webui_port="$(read_open_webui_setting OPEN_WEBUI_PORT || true)"
  open_webui_host="${open_webui_host:-127.0.0.1}"
  [[ "$open_webui_port" =~ ^[0-9]+$ ]] || open_webui_port=8080
  OPEN_WEBUI_URL="http://${open_webui_host}:${open_webui_port}"
fi

case "$MANAGER" in
  auto|launchd|systemd|s6|screen) ;;
  *)
    echo "error: unsupported manager '$MANAGER'" >&2
    exit 2
    ;;
esac

export HERMES_HOME

pass() {
  printf 'ok: %s\n' "$*"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf 'warn: %s\n' "$*" >&2
}

fail() {
  FAILURES=$((FAILURES + 1))
  printf 'fail: %s\n' "$*" >&2
}

python_bin() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/python"
  else
    command -v python3 2>/dev/null || command -v python 2>/dev/null || true
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

check_branch_and_remote() {
  local branch origin default_branch
  branch="$(git branch --show-current 2>/dev/null || true)"
  origin="$(git remote get-url origin 2>/dev/null || true)"
  # The Hussh One fork lives at hushh-labs/hussh-one-hermes but its DEFAULT
  # (deployment) branch is "main" — there is no separate "hussh-one-hermes"
  # branch. Accept either so a normal main checkout is not flagged.
  if [[ "$branch" == "main" || "$branch" == "hussh-one-hermes" ]]; then
    pass "branch is '$branch' (Hussh One deployment branch)"
  else
    warn "current branch is '${branch:-unknown}', expected main for Hussh One deployments"
  fi
  if [[ "$origin" == *"hushh-labs/hussh-one-hermes"* ]]; then
    pass "origin points at hushh-labs/hussh-one-hermes"
  else
    warn "origin is '${origin:-unset}', expected hushh-labs/hussh-one-hermes"
  fi
  if command -v gh >/dev/null 2>&1; then
    default_branch="$(gh repo view hushh-labs/hussh-one-hermes --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || true)"
    if [[ -n "$default_branch" ]]; then
      if [[ "$default_branch" == "main" || "$default_branch" == "hussh-one-hermes" ]]; then
        pass "GitHub default branch is $default_branch"
      else
        warn "GitHub default branch is $default_branch, expected main"
      fi
    fi
  fi
}

check_required_files() {
  local files=(
    HUSSH_ONE.md
    docs/hussh-one-upstream-maintenance.md
    docs/hussh-one/CHANGELOG.md
    hermes_cli/brand.py
    hermes_cli/skins/hussh-one.yaml
    hermes_cli/dashboard_themes/hussh-one.yaml
    plugins/model-providers/google-vertex-claude/__init__.py
    scripts/hussh-one-bootstrap.sh
    scripts/hussh-one-supervisor.sh
    scripts/hussh-one-doctor.sh
    scripts/hussh-one-guard.sh
    scripts/hussh-one-changelog-check.py
  )
  local missing=0
  for file in "${files[@]}"; do
    if [[ -f "$file" ]]; then
      pass "required file exists: $file"
    else
      fail "required file missing: $file"
      missing=1
    fi
  done
  return "$missing"
}

check_legacy_branding() {
  local legacy_pattern
  legacy_pattern='hushh''-puppy|hussh ''puppy|HUSSH''_PUPPY'
  if rg -n "$legacy_pattern" --glob '!tests/hermes_cli/test_hussh_one_branding.py' >/tmp/hussh-one-branding.$$ 2>/dev/null; then
    cat /tmp/hussh-one-branding.$$
    rm -f /tmp/hussh-one-branding.$$
    fail "legacy ""Hussh ""puppy branding text remains in tracked files"
  else
    rm -f /tmp/hussh-one-branding.$$
    pass "legacy puppy branding strings are absent"
  fi
}

check_config() {
  local py
  local output
  local status
  py="$(python_bin)"
  if [[ -z "$py" || ! -x "$py" ]]; then
    fail "Python runtime not found"
    return 0
  fi
  set +e
  output="$("$py" - <<'PY'
from hermes_cli.config import load_config

cfg = load_config()
errors = []
warnings = []

brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
# Match the canonical brand from hermes_cli.brand (single source of truth)
# rather than a hardcoded string, so the doctor never drifts from the real
# brand. Current canonical form is the emoji-first "🤫 Hussh One".
try:
    from hermes_cli.brand import BRAND_DISPLAY_NAME as _CANON_BRAND
except Exception:
    _CANON_BRAND = "🤫 Hussh One"
if brand.get("display_name") != _CANON_BRAND:
    errors.append(f"brand.display_name is not {_CANON_BRAND}")
if cfg.get("display", {}).get("skin") != "hussh-one":
    errors.append("display.skin is not hussh-one")
if cfg.get("dashboard", {}).get("theme") != "hussh-one":
    errors.append("dashboard.theme is not hussh-one")

model_cfg = cfg.get("model")
if isinstance(model_cfg, dict):
    provider = str(model_cfg.get("provider") or "").strip()
    default_model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
else:
    provider = ""
    default_model = str(model_cfg or "").strip()

if provider and provider != "gemini":
    warnings.append(f"model.provider is {provider}, expected gemini for global default")
if default_model and default_model != "gemini-3.6-flash":
    warnings.append(f"model.default is {default_model}, expected gemini-3.6-flash")
if not provider:
    warnings.append("model.provider is unset; bootstrap will set it to gemini")
if not default_model:
    warnings.append("model.default is unset; bootstrap will set it to gemini-3.6-flash")

reasoning_effort = cfg.get("agent", {}).get("reasoning_effort")
if reasoning_effort != "high":
    warnings.append(f"agent.reasoning_effort is {reasoning_effort}, expected high")
if not cfg.get("display", {}).get("show_reasoning"):
    warnings.append("display.show_reasoning is False, expected True")

for line in errors:
    print(f"ERROR:{line}")
for line in warnings:
    print(f"WARN:{line}")
raise SystemExit(1 if errors else 0)
PY
  )"
  status=$?
  set -e
  while IFS= read -r line; do
    case "$line" in
      ERROR:*) fail "${line#ERROR:}" ;;
      WARN:*) warn "${line#WARN:}" ;;
      *) [[ -n "$line" ]] && printf '%s\n' "$line" ;;
    esac
  done <<< "$output"
  if [[ "$status" -eq 0 ]]; then
    pass "Hussh One config identity loaded"
  else
    fail "Hussh One config identity check failed"
  fi
}

check_supervisor_status() {
  local args=("$SCRIPT_DIR/hussh-one-supervisor.sh" status --manager "$MANAGER")
  if [[ "$DRY_RUN" == "1" ]]; then
    args+=(--dry-run)
  fi
  if "${args[@]}" >/tmp/hussh-one-supervisor.$$ 2>/tmp/hussh-one-supervisor-err.$$; then
    pass "supervisor status command completed"
    sed 's/^/  /' /tmp/hussh-one-supervisor.$$
  else
    warn "supervisor status command reported a problem"
    sed 's/^/  /' /tmp/hussh-one-supervisor-err.$$ >&2
  fi
  rm -f /tmp/hussh-one-supervisor.$$ /tmp/hussh-one-supervisor-err.$$
}

check_boot_persistence() {
  # "Is it running now" (check_supervisor_status) is not the same claim as
  # "will it come back after a restart". A plist that lost RunAtLoad (bad
  # edit, accidental re-write, upstream merge) still shows a running
  # process today and only reveals the gap on the NEXT reboot — exactly
  # the blind spot this check closes.
  if [[ "$MANAGER" != "launchd" && "$MANAGER" != "auto" ]]; then
    warn "boot-persistence check only implemented for launchd; manager=$MANAGER skipped"
    return 0
  fi
  if [[ "$(uname -s)" != "Darwin" ]]; then
    warn "boot-persistence check is macOS/launchd-specific; skipped on $(uname -s)"
    return 0
  fi
  if ! command -v /usr/libexec/PlistBuddy >/dev/null 2>&1; then
    warn "PlistBuddy unavailable; boot-persistence check skipped"
    return 0
  fi

  local agents_dir="$HOME/Library/LaunchAgents"
  # label:plist pairs as a flat list (macOS ships bash 3.2 — no `declare -A`
  # associative arrays available, so this must stay portable). LaunchAgents
  # fire at user LOGIN, not raw power-on; without autologin that means "log
  # back in after restart", which is the accurate claim to make, not
  # "survives a cold boot before anyone signs in".
  local pairs=(
    "ai.hermes.gateway:ai.hermes.gateway.plist"
    "ai.hussh-one.dashboard:ai.hussh-one.dashboard.plist"
    "ai.hushh.one.litellm-proxy:ai.hushh.one.litellm-proxy.plist"
    "ai.hushh.one.litellm-shim:ai.hushh.one.litellm-shim.plist"
    "ai.openwebui.hermes:ai.openwebui.hermes.plist"
  )

  local pair label plist_file plist_path run_at_load
  for pair in "${pairs[@]}"; do
    label="${pair%%:*}"
    plist_file="${pair##*:}"
    plist_path="$agents_dir/$plist_file"

    if [[ ! -f "$plist_path" ]]; then
      warn "boot-persistence: $plist_file missing at $plist_path (not installed on this machine)"
      continue
    fi

    run_at_load="$(/usr/libexec/PlistBuddy -c "Print :RunAtLoad" "$plist_path" 2>/dev/null || echo "MISSING")"
    if [[ "$run_at_load" != "true" ]]; then
      fail "boot-persistence: $plist_file has RunAtLoad=$run_at_load (expected true) — will NOT auto-start after reboot/re-login"
      continue
    fi

    # RunAtLoad=true alone doesn't survive a reboot if the job was never
    # bootstrapped into launchd's persistent DB (e.g. plist edited by hand
    # and never (re)loaded). Confirm launchd actually knows about the label.
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      pass "boot-persistence: $label — RunAtLoad=true, registered with launchd"
    else
      fail "boot-persistence: $plist_file has RunAtLoad=true but launchd has no record of '$label' — run: launchctl bootstrap gui/\$(id -u) $plist_path"
    fi
  done
}

check_dashboard_chat() {
  if ! command -v curl >/dev/null 2>&1; then
    if [[ "$REQUIRE_SERVICES" == "1" ]]; then
      fail "curl is required for dashboard health checks"
    else
      warn "curl unavailable; dashboard health skipped"
    fi
    return 0
  fi
  local html
  html="$(curl -fsS --max-time 2 "$DASHBOARD_URL/" 2>/dev/null || true)"
  if [[ -z "$html" ]]; then
    if [[ "$REQUIRE_SERVICES" == "1" ]]; then
      fail "dashboard not reachable at $DASHBOARD_URL"
    else
      warn "dashboard not reachable at $DASHBOARD_URL"
    fi
    return 0
  fi
  if [[ "$html" == *"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true"* ]]; then
    pass "dashboard embedded chat flag is enabled"
  else
    fail "dashboard reachable but embedded chat flag is not true; start with hermes dashboard --tui"
  fi
}

check_open_webui_health() {
  if ! command -v curl >/dev/null 2>&1; then
    if [[ "$REQUIRE_SERVICES" == "1" ]]; then
      fail "curl is required for Open WebUI health checks"
    else
      warn "curl unavailable; Open WebUI health skipped"
    fi
    return 0
  fi
  local url status
  for url in "$OPEN_WEBUI_URL/health" "$OPEN_WEBUI_URL/"; do
    status="$(curl -sS -L --max-time 2 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    case "$status" in
      2*|3*)
        pass "Open WebUI health is reachable at $OPEN_WEBUI_URL"
        return 0
        ;;
    esac
  done
  if [[ "$REQUIRE_SERVICES" == "1" ]]; then
    fail "Open WebUI health not reachable at $OPEN_WEBUI_URL"
  else
    warn "Open WebUI health not reachable at $OPEN_WEBUI_URL; supervisor start/restart will attempt recovery"
  fi
}

check_whatsapp_health() {
  if ! command -v curl >/dev/null 2>&1; then
    warn "curl unavailable; WhatsApp health skipped"
    return 0
  fi
  local body
  body="$(curl -fsS --max-time 2 "$WHATSAPP_HEALTH_URL" 2>/dev/null || true)"
  if [[ -z "$body" ]]; then
    if [[ "$REQUIRE_SERVICES" == "1" ]]; then
      fail "WhatsApp bridge health not reachable at $WHATSAPP_HEALTH_URL"
    else
      warn "WhatsApp bridge health not reachable at $WHATSAPP_HEALTH_URL"
    fi
    return 0
  fi
  if [[ "$body" == *'"connected":true'* || "$body" == *'"status":"connected"'* ]]; then
    pass "WhatsApp bridge health reports connected"
  else
    warn "WhatsApp bridge reachable but not connected; pairing is per-machine"
    printf '%s\n' "$body" | head -c 400 | sed 's/^/  /'
    printf '\n'
  fi
}

check_vertex_profile() {
  local py
  py="$(python_bin)"
  if [[ -z "$py" || ! -x "$py" ]]; then
    fail "Python runtime not found for Vertex profile check"
    return 0
  fi
  if "$py" - <<'PY'
from providers import get_provider_profile

profile = get_provider_profile("google-vertex-claude")
assert profile is not None
assert profile.auth_type == "gcp_sdk"
assert profile.api_mode == "anthropic_messages"
models = set(profile.fallback_models or ())
assert {"claude-opus-4-8", "claude-sonnet-4-6"}.issubset(models)
print("Vertex Claude provider profile resolves through gcp_sdk.")
PY
  then
    pass "Vertex Claude provider profile is registered"
  else
    fail "Vertex Claude provider profile check failed"
  fi
}

run_live_vertex_smoke() {
  local hermes
  hermes="$(hermes_bin)"
  if [[ "$LIVE_VERTEX" != "1" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    pass "live Vertex smoke skipped in dry-run mode"
    return 0
  fi
  if [[ ! -x "$hermes" ]]; then
    fail "Hermes binary unavailable for live Vertex smoke"
    return 0
  fi
  for model in claude-opus-4-8 claude-sonnet-4-6; do
    if "$hermes" chat --provider=google-vertex-claude -m "$model" -q "reply with ok" >/tmp/hussh-one-vertex.$$ 2>/tmp/hussh-one-vertex-err.$$; then
      pass "live Vertex smoke passed for $model"
    else
      warn "live Vertex smoke failed for $model"
      sed 's/^/  /' /tmp/hussh-one-vertex-err.$$ >&2
    fi
  done
  rm -f /tmp/hussh-one-vertex.$$ /tmp/hussh-one-vertex-err.$$
}

check_copilot_byok() {
  # VS Code Copilot BYOK (Vertex ADC) stack is optional per-machine. If the
  # launchers aren't installed, emit a hint (not a failure) pointing at setup.
  local proxy_launcher="$HERMES_HOME/scripts/start_litellm_proxy.sh"
  local shim_launcher="$HERMES_HOME/scripts/start_litellm_shim.sh"
  local shim_py="$HERMES_HOME/scripts/litellm_auth_shim.py"

  if [[ ! -f "$proxy_launcher" || ! -f "$shim_launcher" || ! -f "$shim_py" ]]; then
    warn "Copilot BYOK not installed; run: scripts/hussh-one-copilot-setup.sh --start"
    return 0
  fi
  pass "Copilot BYOK assets present (proxy launcher, shim launcher, shim)"

  if [[ "$DRY_RUN" == "1" ]]; then
    pass "Copilot BYOK live probe skipped in dry-run mode"
    return 0
  fi

  # Probe the shim (:8644). Default auth semantics are deterministic
  # (no-auth -> 401); the explicit VS Code compatibility mode permits a
  # headerless request only at this loopback listener.
  local py
  py="$(python_bin)"
  if [[ -z "$py" || ! -x "$py" ]]; then
    warn "Python runtime not found for Copilot BYOK probe"
    return 0
  fi
  local allow_anonymous=0
  if grep -q 'HUSSH_SHIM_ALLOW_LOOPBACK_ANONYMOUS="1"' "$shim_launcher" 2>/dev/null; then
    allow_anonymous=1
    pass "Copilot BYOK loopback compatibility mode accepts missing or blank-bearer local requests"
  fi
  if ALLOW_ANONYMOUS="$allow_anonymous" "$py" - <<'PY'
import os, urllib.request, urllib.error, sys
def code(path, auth=None):
    req = urllib.request.Request("http://127.0.0.1:8644"+path)
    if auth: req.add_header("Authorization", "Bearer "+auth)
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e: return e.code
    except Exception: return None
noauth = code("/v1/models")
health = code("/healthz")              # 200 if shim+upstream alive
expected_noauth = 200 if os.environ.get("ALLOW_ANONYMOUS") == "1" else 401
sys.exit(0 if (noauth == expected_noauth and health == 200) else 1)
PY
  then
    if [[ "$allow_anonymous" == "1" ]]; then
      pass "Copilot BYOK shim live on :8644 (loopback compatibility, upstream healthy)"
    else
      pass "Copilot BYOK shim live on :8644 (deterministic 401, upstream healthy)"
    fi
  else
    warn "Copilot BYOK shim not responding correctly on :8644; start it: scripts/hussh-one-copilot-setup.sh --start (the reaper also self-heals it)"
  fi

  # Guard the silent-fallback regression: VS Code's chatLanguageModels.json must
# carry a literal key on the Vertex ADC provider, NOT a ${input:...} secret-store
  # reference. If the secret evaporates (VS Code update / keychain change),
  # Copilot sends an empty bearer, the shim 401s, and Copilot silently falls back
  # to its metered hosted model — surfacing as a bogus "credit limit" error.
  local want_key="${KEY:-}"
  [[ -z "$want_key" ]] && want_key="$(grep -o 'LITELLM_MASTER_KEY="[^"]*"' "$proxy_launcher" 2>/dev/null | cut -d'"' -f2 || true)"
  local vs_dir
  for vs_dir in \
    "$HOME/Library/Application Support/Code - Insiders/User" \
    "$HOME/Library/Application Support/Code/User" \
    "$HOME/.config/Code - Insiders/User" \
    "$HOME/.config/Code/User"; do
    local cfg="$vs_dir/chatLanguageModels.json"
    [[ -f "$cfg" ]] || continue
    if WANT_KEY="$want_key" CFG="$cfg" "$py" - <<'PY'
import json, os, sys
cfg = os.environ["CFG"]; want = os.environ.get("WANT_KEY", "")
try:
    data = json.load(open(cfg))
except Exception:
    sys.exit(2)
vertex = [b for b in data if isinstance(b, dict) and b.get("name") == "Hussh One Vertex ADC"]
if not vertex:
    sys.exit(3)  # endpoint absent — not configured here
for b in vertex:
    k = b.get("apiKey", "")
    if not k or "${input:" in str(k):
        sys.exit(1)  # missing/placeholder key — the silent-fallback trap
    if want and k != want:
        sys.exit(4)  # stale key — won't match the shim
sys.exit(0)
PY
    then
      pass "VS Code Vertex ADC key present & matches shim ($(basename "$(dirname "$vs_dir")"))"
    else
      case $? in
        3) : ;;  # endpoint not in this edition — silent
        4) warn "VS Code Vertex ADC key STALE in $cfg (won't match shim); re-run: scripts/hussh-one-copilot-setup.sh" ;;
        *) warn "VS Code Vertex ADC key MISSING/placeholder in $cfg — Copilot will silently fall back to a metered model (bogus 'credit limit'). Re-run: scripts/hussh-one-copilot-setup.sh" ;;
      esac
    fi
  done

  # Guard the smart-tool-usage steering file. BYOK models (Vertex Claude/Gemini)
  # aren't tuned for Copilot's tool ecosystem, so without this always-on user
  # instructions file they over-spawn subagents and shell out `cat`/`sed`
  # instead of using native read/edit tools. Ships via write_copilot_instructions().
  local instr="$HOME/.copilot/instructions/hussh-one-tooling.instructions.md"
  if [[ -f "$instr" ]] && grep -q "Smart tool usage" "$instr" 2>/dev/null; then
    pass "Copilot smart-tool-usage instructions present (~/.copilot/instructions)"
  else
    warn "Copilot smart-tool-usage instructions MISSING — BYOK models will over-delegate & use 'cat' instead of native tools. Re-run: scripts/hussh-one-copilot-setup.sh"
  fi
}

check_changelog_freshness() {
  # docs/hussh-one/CHANGELOG.md is the crystal-clear, dated index of every
  # Hussh-One-only capability (WhatsApp/capsules, Vertex ADC, Copilot BYOK,
  # Open WebUI, ...). Stale = the whole point of the index is defeated, so
  # this is a doctor check, not just a health-index probe.
  local checker="scripts/hussh-one-changelog-check.py"
  if [[ ! -f "$checker" ]]; then
    warn "changelog freshness checker missing: $checker"
    return 0
  fi
  local python_bin="${PYTHON:-python3}"
  if "$python_bin" "$checker" >/tmp/hussh-one-changelog.$$ 2>&1; then
    pass "changelog current ($(tail -1 /tmp/hussh-one-changelog.$$))"
  else
    warn "changelog stale — undocumented Hussh-One commits found:"
    sed 's/^/  /' /tmp/hussh-one-changelog.$$ >&2
  fi
  rm -f /tmp/hussh-one-changelog.$$
}

check_branch_and_remote
check_required_files || true
check_legacy_branding
check_config
check_supervisor_status
check_boot_persistence
check_dashboard_chat
check_open_webui_health
check_whatsapp_health
check_vertex_profile
check_changelog_freshness
run_live_vertex_smoke
check_copilot_byok

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'Hussh One doctor failed with %s failure(s) and %s warning(s).\n' "$FAILURES" "$WARNINGS" >&2
  exit 1
fi

printf 'Hussh One doctor passed with %s warning(s).\n' "$WARNINGS"
