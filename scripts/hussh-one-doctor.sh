#!/usr/bin/env bash
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
WHATSAPP_HEALTH_URL="${HUSSH_ONE_WHATSAPP_HEALTH_URL:-http://127.0.0.1:3000/health}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

FAILURES=0
WARNINGS=0

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-doctor.sh [options]

Options:
  --manager auto|launchd|systemd|s6|screen
  --require-services       Fail if dashboard or WhatsApp health is not reachable
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
  if [[ "$branch" == "hussh-one-hermes" ]]; then
    pass "branch is hussh-one-hermes"
  else
    warn "current branch is '${branch:-unknown}', expected hussh-one-hermes for Hussh One deployments"
  fi
  if [[ "$origin" == *"hushh-labs/hussh-one-hermes"* ]]; then
    pass "origin points at hushh-labs/hussh-one-hermes"
  else
    warn "origin is '${origin:-unset}', expected hushh-labs/hussh-one-hermes"
  fi
  if command -v gh >/dev/null 2>&1; then
    default_branch="$(gh repo view hushh-labs/hussh-one-hermes --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || true)"
    if [[ -n "$default_branch" ]]; then
      if [[ "$default_branch" == "hussh-one-hermes" ]]; then
        pass "GitHub default branch is hussh-one-hermes"
      else
        warn "GitHub default branch is $default_branch, expected hussh-one-hermes"
      fi
    fi
  fi
}

check_required_files() {
  local files=(
    HUSSH_ONE.md
    docs/hussh-one-upstream-maintenance.md
    hermes_cli/brand.py
    hermes_cli/skins/hussh-one.yaml
    hermes_cli/dashboard_themes/hussh-one.yaml
    plugins/model-providers/google-vertex-claude/__init__.py
    scripts/hussh-one-bootstrap.sh
    scripts/hussh-one-supervisor.sh
    scripts/hussh-one-doctor.sh
    scripts/hussh-one-guard.sh
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
if brand.get("display_name") != "hussh 🤫 One":
    errors.append("brand.display_name is not hussh 🤫 One")
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
if default_model and default_model != "gemini-3.5-flash":
    warnings.append(f"model.default is {default_model}, expected gemini-3.5-flash")
if not provider:
    warnings.append("model.provider is unset; bootstrap will set it to gemini")
if not default_model:
    warnings.append("model.default is unset; bootstrap will set it to gemini-3.5-flash")

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

check_branch_and_remote
check_required_files || true
check_legacy_branding
check_config
check_supervisor_status
check_dashboard_chat
check_whatsapp_health
check_vertex_profile
run_live_vertex_smoke

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'Hussh One doctor failed with %s failure(s) and %s warning(s).\n' "$FAILURES" "$WARNINGS" >&2
  exit 1
fi

printf 'Hussh One doctor passed with %s warning(s).\n' "$WARNINGS"
