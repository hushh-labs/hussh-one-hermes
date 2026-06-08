#!/usr/bin/env bash
# Guard Hussh One fork contracts after upstream Hermes or plugin updates.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "error: required Hussh One file is missing: $path" >&2
    exit 1
  fi
}

require_file "HUSSH_ONE.md"
require_file "hermes_cli/brand.py"
require_file "hermes_cli/skins/hussh-one.yaml"
require_file "hermes_cli/dashboard_themes/hussh-one.yaml"
require_file "plugins/model-providers/google-vertex-claude/__init__.py"
require_file "scripts/hussh-one-bootstrap.sh"
require_file "scripts/hussh-one-supervisor.sh"
require_file "scripts/hussh-one-doctor.sh"
require_file "scripts/hussh-one-restart.sh"

legacy_brand_pattern='hushh''-puppy|hussh ''puppy|HUSSH''_PUPPY'
if rg -n "$legacy_brand_pattern" --glob '!tests/hermes_cli/test_hussh_one_branding.py'; then
  echo "error: legacy Hussh One branding text found" >&2
  exit 1
fi

legacy_provider_pattern='anthropic''-vertex'
if rg -n "$legacy_provider_pattern" \
  hermes_cli agent providers plugins gateway scripts tests tools run_agent.py cli.py HUSSH_ONE.md; then
  echo "error: legacy Vertex Claude provider name found" >&2
  exit 1
fi

if [[ -x "scripts/run_tests.sh" ]]; then
  scripts/run_tests.sh \
    tests/agent/test_anthropic_adapter.py \
    tests/agent/test_vertex_claude_integration.py \
    tests/hermes_cli/test_model_switch*.py \
    tests/gateway/test_whatsapp_reply_prefix.py \
    tests/hermes_cli/test_hussh_one_branding.py \
    -- -q
else
  if [[ -x ".venv/bin/pytest" ]]; then
    PYTEST=(.venv/bin/pytest)
  else
    PYTEST=(python -m pytest)
  fi
  "${PYTEST[@]}" \
    tests/agent/test_anthropic_adapter.py \
    tests/agent/test_vertex_claude_integration.py \
    tests/hermes_cli/test_model_switch*.py \
    tests/gateway/test_whatsapp_reply_prefix.py \
    tests/hermes_cli/test_hussh_one_branding.py \
    -q
fi

command -v node >/dev/null 2>&1 || {
  echo "error: node is required to verify the WhatsApp bridge" >&2
  exit 1
}
node --check scripts/whatsapp-bridge/bridge.js
bash -n \
  scripts/hussh-one-bootstrap.sh \
  scripts/hussh-one-supervisor.sh \
  scripts/hussh-one-doctor.sh \
  scripts/hussh-one-restart.sh

dashboard_url="${HUSSH_ONE_DASHBOARD_URL:-http://127.0.0.1:9119}"
if [[ "${HUSSH_ONE_SKIP_DASHBOARD_HEALTH:-0}" != "1" ]]; then
  if command -v curl >/dev/null 2>&1; then
    dashboard_html="$(curl -fsS --max-time 2 "$dashboard_url/" 2>/dev/null || true)"
    if [[ -n "$dashboard_html" ]]; then
      if [[ "$dashboard_html" != *"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true"* ]]; then
        echo "error: dashboard is reachable at $dashboard_url but embedded chat is not enabled" >&2
        echo "hint: restart with scripts/hussh-one-restart.sh or hermes dashboard --tui" >&2
        exit 1
      fi
      echo "Hussh One dashboard chat health passed."
    elif [[ "${HUSSH_ONE_REQUIRE_DASHBOARD_CHAT:-0}" == "1" ]]; then
      echo "error: dashboard is not reachable at $dashboard_url" >&2
      exit 1
    else
      echo "Hussh One dashboard chat health skipped (dashboard not reachable)."
    fi
  elif [[ "${HUSSH_ONE_REQUIRE_DASHBOARD_CHAT:-0}" == "1" ]]; then
    echo "error: curl is required for dashboard chat health checks" >&2
    exit 1
  else
    echo "Hussh One dashboard chat health skipped (curl not available)."
  fi
fi

echo "Hussh One guard passed."
