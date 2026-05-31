#!/usr/bin/env bash
# Restart local Hussh One dashboard + gateway with embedded TUI chat enabled.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DASHBOARD_SCREEN="${HUSSH_ONE_DASHBOARD_SCREEN:-hermes-dashboard-hussh-one}"
GATEWAY_SCREEN="${HUSSH_ONE_GATEWAY_SCREEN:-hermes-gateway-hussh-one}"
DASHBOARD_HOST="${HUSSH_ONE_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${HUSSH_ONE_DASHBOARD_PORT:-9119}"
WHATSAPP_PORT="${HUSSH_ONE_WHATSAPP_PORT:-3000}"
HERMES_BIN="${HERMES_BIN:-$REPO_ROOT/.venv/bin/hermes}"
DASHBOARD_LOG="${HUSSH_ONE_DASHBOARD_LOG:-$HOME/.hermes/logs/dashboard-screen.log}"
GATEWAY_LOG="${HUSSH_ONE_GATEWAY_LOG:-$HOME/.hermes/logs/gateway-screen.log}"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "error: Hermes binary not found or not executable: $HERMES_BIN" >&2
  exit 1
fi

if ! command -v screen >/dev/null 2>&1; then
  echo "error: screen is required for the local Hussh One restart helper" >&2
  exit 1
fi

stop_screen() {
  local name="$1"
  screen -S "$name" -X quit >/dev/null 2>&1 || true
}

kill_listener() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids >/dev/null 2>&1 || true
  fi
}

mkdir -p "$(dirname "$DASHBOARD_LOG")" "$(dirname "$GATEWAY_LOG")"

stop_screen "$DASHBOARD_SCREEN"
stop_screen "$GATEWAY_SCREEN"
sleep 2
kill_listener "$DASHBOARD_PORT"
kill_listener "$WHATSAPP_PORT"
sleep 1

screen -dmS "$DASHBOARD_SCREEN" zsh -lc \
  "cd '$REPO_ROOT' && '$HERMES_BIN' dashboard --host '$DASHBOARD_HOST' --port '$DASHBOARD_PORT' --tui --no-open >> '$DASHBOARD_LOG' 2>&1"

screen -dmS "$GATEWAY_SCREEN" zsh -lc \
  "cd '$REPO_ROOT' && '$HERMES_BIN' gateway run --replace >> '$GATEWAY_LOG' 2>&1"

echo "Hussh One dashboard: http://$DASHBOARD_HOST:$DASHBOARD_PORT"
echo "Hussh One gateway screen: $GATEWAY_SCREEN"
