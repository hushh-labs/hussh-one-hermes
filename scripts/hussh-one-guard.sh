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

legacy_brand_pattern='hushh''-puppy|hussh ''puppy|HUSSH''_PUPPY'
if rg -n "$legacy_brand_pattern" --glob '!tests/hermes_cli/test_hussh_one_branding.py'; then
  echo "error: legacy Hussh One branding text found" >&2
  exit 1
fi

legacy_provider_pattern='anthropic''-vertex'
if rg -n "$legacy_provider_pattern" \
  hermes_cli agent providers plugins gateway scripts tests run_agent.py cli.py HUSSH_ONE.md; then
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

echo "Hussh One guard passed."
