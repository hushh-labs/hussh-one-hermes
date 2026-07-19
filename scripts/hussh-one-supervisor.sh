#!/usr/bin/env bash
# Hussh One lifecycle manager for dashboard + messaging gateway.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACTION=""
MANAGER="${HUSSH_ONE_SUPERVISOR:-auto}"
CLEAN_CONFLICTS=0
DRY_RUN="${HUSSH_ONE_DRY_RUN:-0}"

DASHBOARD_HOST="${HUSSH_ONE_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${HUSSH_ONE_DASHBOARD_PORT:-9119}"
WHATSAPP_PORT="${HUSSH_ONE_WHATSAPP_PORT:-8473}"
OPEN_WEBUI_HOST="${HUSSH_ONE_OPEN_WEBUI_HOST:-}"
OPEN_WEBUI_PORT="${HUSSH_ONE_OPEN_WEBUI_PORT:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-}"
HERMES_PYTHON_BIN="${HUSSH_ONE_PYTHON_BIN:-}"
SERVICE_NOFILE_LIMIT="${HUSSH_ONE_NOFILE_LIMIT:-65536}"
DASHBOARD_WATCHDOG_INTERVAL="${HUSSH_ONE_DASHBOARD_WATCHDOG_INTERVAL:-5}"
DASHBOARD_MEM_CAP_MB="${HUSSH_ONE_DASHBOARD_MEM_CAP_MB:-6144}"

DASHBOARD_SCREEN="${HUSSH_ONE_DASHBOARD_SCREEN:-hermes-dashboard-hussh-one}"
GATEWAY_SCREEN="${HUSSH_ONE_GATEWAY_SCREEN:-hermes-gateway-hussh-one}"
DASHBOARD_LABEL="${HUSSH_ONE_DASHBOARD_LAUNCHD_LABEL:-ai.hussh-one.dashboard}"
DASHBOARD_UNIT="${HUSSH_ONE_DASHBOARD_SYSTEMD_UNIT:-hussh-one-dashboard.service}"
# Kept only to retire the pre-single-owner detached watchdog during upgrade.
DASHBOARD_WATCHDOG_PID="${HUSSH_ONE_DASHBOARD_WATCHDOG_PID:-$HERMES_HOME/hussh-one-dashboard.watchdog.pid}"
DASHBOARD_WATCHDOG_SCRIPT="$SCRIPT_DIR/hussh-one-dashboard-watchdog.py"
DASHBOARD_LOG="${HUSSH_ONE_DASHBOARD_LOG:-$HERMES_HOME/logs/hussh-one-dashboard.log}"
DASHBOARD_ERR_LOG="${HUSSH_ONE_DASHBOARD_ERR_LOG:-$HERMES_HOME/logs/hussh-one-dashboard.error.log}"
GATEWAY_LOG="${HUSSH_ONE_GATEWAY_LOG:-$HERMES_HOME/logs/hussh-one-gateway.log}"
GATEWAY_ERR_LOG="${HUSSH_ONE_GATEWAY_ERR_LOG:-$HERMES_HOME/logs/hussh-one-gateway.error.log}"
OPEN_WEBUI_LABEL="${HUSSH_ONE_OPEN_WEBUI_LAUNCHD_LABEL:-ai.openwebui.hermes}"
OPEN_WEBUI_UNIT="${HUSSH_ONE_OPEN_WEBUI_SYSTEMD_UNIT:-openwebui-hermes.service}"
OPEN_WEBUI_SCREEN="${HUSSH_ONE_OPEN_WEBUI_SCREEN:-openwebui-hermes}"
OPEN_WEBUI_LAUNCHER="${HUSSH_ONE_OPEN_WEBUI_LAUNCHER:-$HOME/.local/bin/start-open-webui-hermes.sh}"
OPEN_WEBUI_LOG="${HUSSH_ONE_OPEN_WEBUI_LOG:-$HERMES_HOME/logs/openwebui.log}"

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-supervisor.sh {install|start|stop|restart|status} [options]

Options:
  --manager auto|launchd|systemd|s6|screen
  --clean-conflicts          Stop fallback screen sessions before service start/restart
  --host HOST                Dashboard host (default: 127.0.0.1)
  --dashboard-port PORT      Dashboard port (default: 9119)
  --whatsapp-port PORT       WhatsApp bridge port (default: 8473)
  --open-webui-port PORT     Open WebUI port (default: 8080)
  --dry-run                  Print selected actions without mutating services
  -h, --help                 Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|start|stop|restart|status)
      ACTION="$1"
      shift
      ;;
    --manager)
      MANAGER="${2:-}"
      shift 2
      ;;
    --clean-conflicts)
      CLEAN_CONFLICTS=1
      shift
      ;;
    --host)
      DASHBOARD_HOST="${2:-}"
      shift 2
      ;;
    --dashboard-port)
      DASHBOARD_PORT="${2:-}"
      shift 2
      ;;
    --whatsapp-port)
      WHATSAPP_PORT="${2:-}"
      shift 2
      ;;
    --open-webui-port)
      OPEN_WEBUI_PORT="${2:-}"
      shift 2
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

if [[ -z "$OPEN_WEBUI_HOST" ]]; then
  OPEN_WEBUI_HOST="$(read_open_webui_setting OPEN_WEBUI_HOST || true)"
fi
if [[ -z "$OPEN_WEBUI_PORT" ]]; then
  OPEN_WEBUI_PORT="$(read_open_webui_setting OPEN_WEBUI_PORT || true)"
fi
OPEN_WEBUI_HOST="${OPEN_WEBUI_HOST:-127.0.0.1}"
if [[ ! "$OPEN_WEBUI_PORT" =~ ^[0-9]+$ ]]; then
  OPEN_WEBUI_PORT=8080
fi

if [[ -z "$ACTION" ]]; then
  usage >&2
  exit 2
fi

case "$MANAGER" in
  auto|launchd|systemd|s6|screen) ;;
  *)
    echo "error: unsupported manager '$MANAGER'" >&2
    exit 2
    ;;
esac

if [[ -z "$HERMES_BIN" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/hermes" ]]; then
    HERMES_BIN="$REPO_ROOT/.venv/bin/hermes"
  elif command -v hermes >/dev/null 2>&1; then
    HERMES_BIN="$(command -v hermes)"
  else
    HERMES_BIN="$REPO_ROOT/.venv/bin/hermes"
  fi
fi

if [[ -z "$HERMES_PYTHON_BIN" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    HERMES_PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    HERMES_PYTHON_BIN="$(command -v python3)"
  else
    HERMES_PYTHON_BIN="$HERMES_BIN"
  fi
fi

if [[ "$DRY_RUN" != "1" && ! -x "$HERMES_BIN" ]]; then
  echo "error: Hermes binary not found or not executable: $HERMES_BIN" >&2
  echo "hint: run scripts/hussh-one-bootstrap.sh first" >&2
  exit 1
fi

export HERMES_HOME
mkdir -p "$HERMES_HOME/logs"

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

shell_quote() {
  printf '%q' "$1"
}

xml_escape() {
  printf '%s' "$1" \
    | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

is_macos() {
  [[ "$(uname -s)" == "Darwin" ]]
}

is_linux() {
  [[ "$(uname -s)" == "Linux" ]]
}

is_container() {
  [[ -f /.dockerenv || -f /run/.containerenv || -n "${container:-}" || -n "${HERMES_CONTAINER:-}" ]]
}

detect_manager() {
  if [[ "$MANAGER" != "auto" ]]; then
    printf '%s\n' "$MANAGER"
    return 0
  fi

  if is_macos && command -v launchctl >/dev/null 2>&1; then
    printf 'launchd\n'
    return 0
  fi

  if is_container && command -v s6-svc >/dev/null 2>&1 && { [[ -d /run/s6/services ]] || [[ -d /var/run/s6/services ]]; }; then
    printf 's6\n'
    return 0
  fi

  if is_linux && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    printf 'systemd\n'
    return 0
  fi

  if command -v screen >/dev/null 2>&1; then
    printf 'screen\n'
    return 0
  fi

  echo "error: no supported supervisor found (launchd, systemd, s6, or screen)" >&2
  exit 1
}

port_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
  fi
}

screen_session_exists() {
  local name="$1"
  command -v screen >/dev/null 2>&1 || return 1
  screen -ls 2>/dev/null | grep -Eq "([.]|[[:space:]])${name}([[:space:]]|$)"
}

stop_screen_session() {
  local name="$1"
  command -v screen >/dev/null 2>&1 || return 0
  if screen_session_exists "$name"; then
    run_cmd screen -S "$name" -X quit || true
  fi
}

kill_port_listeners() {
  local port="$1"
  local pids
  pids="$(port_pids "$port")"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    run_cmd kill $pids || true
  fi
}

pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

legacy_dashboard_watchdog_active() {
  local pid=""
  if [[ -f "$DASHBOARD_WATCHDOG_PID" ]]; then
    pid="$(tr -d '[:space:]' < "$DASHBOARD_WATCHDOG_PID" 2>/dev/null || true)"
  fi
  if pid_alive "$pid" && ps -p "$pid" -o command= 2>/dev/null | grep -Fq "dashboard-watchdog"; then
    return 0
  fi
  if [[ "$DRY_RUN" != "1" ]]; then
    rm -f "$DASHBOARD_WATCHDOG_PID" 2>/dev/null || true
  fi
  return 1
}

retire_legacy_dashboard_watchdog() {
  local pid=""
  if [[ -f "$DASHBOARD_WATCHDOG_PID" ]]; then
    pid="$(tr -d '[:space:]' < "$DASHBOARD_WATCHDOG_PID" 2>/dev/null || true)"
  fi
  if legacy_dashboard_watchdog_active; then
    log "Retiring legacy detached dashboard watchdog ($pid)"
    run_cmd kill "$pid" || true
  fi
  if [[ "$DRY_RUN" != "1" ]]; then
    rm -f "$DASHBOARD_WATCHDOG_PID" 2>/dev/null || true
  fi
}

launchd_domain() {
  printf 'gui/%s\n' "$(id -u)"
}

launchd_target() {
  printf '%s/%s\n' "$(launchd_domain)" "$DASHBOARD_LABEL"
}

launchd_job_active() {
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl print "$(launchd_target)" >/dev/null 2>&1
}

launchd_gateway_active() {
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl print "$(launchd_domain)/ai.hermes.gateway" >/dev/null 2>&1
}

systemd_job_active() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-active --quiet "$DASHBOARD_UNIT" >/dev/null 2>&1
}

systemd_gateway_active() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-active --quiet hermes-gateway.service >/dev/null 2>&1
}

selected_dashboard_active() {
  local selected="$1"
  case "$selected" in
    launchd) launchd_job_active ;;
    systemd) systemd_job_active ;;
    screen) screen_session_exists "$DASHBOARD_SCREEN" ;;
    s6) s6_service_dir hussh-one-dashboard >/dev/null 2>&1 || s6_service_dir hermes-dashboard >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

selected_gateway_active() {
  local selected="$1"
  case "$selected" in
    launchd) launchd_gateway_active ;;
    systemd) systemd_gateway_active ;;
    screen) screen_session_exists "$GATEWAY_SCREEN" ;;
    s6) s6_service_dir hermes-gateway >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

detect_conflicts() {
  local selected="$1"
  local conflicts=()
  local dashboard_pids=""
  local whatsapp_pids=""

  if [[ "$selected" != "screen" ]]; then
    if screen_session_exists "$DASHBOARD_SCREEN"; then
      conflicts+=("screen:$DASHBOARD_SCREEN")
    fi
    if screen_session_exists "$GATEWAY_SCREEN"; then
      conflicts+=("screen:$GATEWAY_SCREEN")
    fi
  fi

  if [[ "$selected" == "screen" ]]; then
    if launchd_job_active; then
      conflicts+=("launchd:$DASHBOARD_LABEL")
    fi
    if systemd_job_active; then
      conflicts+=("systemd:$DASHBOARD_UNIT")
    fi
  fi

  if [[ "$DRY_RUN" != "1" && ( "$ACTION" == "start" || "$ACTION" == "restart" ) ]]; then
    dashboard_pids="$(port_pids "$DASHBOARD_PORT")"
    if [[ -n "$dashboard_pids" ]] && ! selected_dashboard_active "$selected"; then
      conflicts+=("port:$DASHBOARD_PORT dashboard listener pid(s): ${dashboard_pids//$'\n'/, }")
    fi
    whatsapp_pids="$(port_pids "$WHATSAPP_PORT")"
    if [[ -n "$whatsapp_pids" ]] && ! selected_gateway_active "$selected"; then
      conflicts+=("port:$WHATSAPP_PORT gateway/WhatsApp listener pid(s): ${whatsapp_pids//$'\n'/, }")
    fi
  fi

  if [[ "${#conflicts[@]}" -gt 0 ]]; then
    printf '%s\n' "${conflicts[@]}"
  fi
}

clean_conflicts() {
  local selected="$1"
  if [[ "$selected" != "screen" ]]; then
    stop_screen_session "$DASHBOARD_SCREEN"
    stop_screen_session "$GATEWAY_SCREEN"
    if ! selected_dashboard_active "$selected"; then
      kill_port_listeners "$DASHBOARD_PORT"
    fi
    if ! selected_gateway_active "$selected"; then
      kill_port_listeners "$WHATSAPP_PORT"
    fi
  fi
  if [[ "$selected" == "screen" ]]; then
    launchd_stop_dashboard || true
    systemd_stop_dashboard || true
  fi
}

require_no_conflicts() {
  local selected="$1"
  local conflicts
  conflicts="$(detect_conflicts "$selected" || true)"
  if [[ -z "$conflicts" ]]; then
    return 0
  fi

  if [[ "$CLEAN_CONFLICTS" == "1" ]]; then
    log "Cleaning conflicting Hussh One supervisors:"
    printf '%s\n' "$conflicts" | sed 's/^/  - /'
    clean_conflicts "$selected"
    return 0
  fi

  echo "error: conflicting Hussh One supervisor state detected for manager '$selected':" >&2
  printf '%s\n' "$conflicts" | sed 's/^/  - /' >&2
  echo "hint: rerun with --clean-conflicts after confirming these are stale." >&2
  exit 1
}

gateway_service() {
  local verb="$1"
  case "$verb" in
    install|start|stop|restart|status)
      run_cmd "$HERMES_BIN" gateway "$verb"
      ;;
    *)
      echo "error: unsupported gateway verb: $verb" >&2
      exit 2
      ;;
  esac
}

dashboard_command_line() {
  printf '%q dashboard --host %q --port %q --tui --no-open' \
    "$HERMES_BIN" "$DASHBOARD_HOST" "$DASHBOARD_PORT"
}

dashboard_url() {
  printf 'http://%s:%s\n' "$DASHBOARD_HOST" "$DASHBOARD_PORT"
}

open_webui_url() {
  printf 'http://%s:%s\n' "$OPEN_WEBUI_HOST" "$OPEN_WEBUI_PORT"
}

open_webui_http_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  local url status
  for url in "$(open_webui_url)/health" "$(open_webui_url)/"; do
    status="$(curl -sS -L --max-time 3 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    case "$status" in
      2*|3*) return 0 ;;
    esac
  done
  return 1
}

open_webui_service_installed() {
  local selected="$1"
  case "$selected" in
    launchd) [[ -f "$HOME/Library/LaunchAgents/${OPEN_WEBUI_LABEL}.plist" ]] ;;
    systemd) [[ -f "$HOME/.config/systemd/user/${OPEN_WEBUI_UNIT}" ]] ;;
    screen) [[ -x "$OPEN_WEBUI_LAUNCHER" ]] ;;
    *) return 1 ;;
  esac
}

screen_start_open_webui() {
  if [[ "$DRY_RUN" != "1" ]] && ! command -v screen >/dev/null 2>&1; then
    return 1
  fi
  if [[ ! -x "$OPEN_WEBUI_LAUNCHER" ]]; then
    return 1
  fi
  mkdir -p "$(dirname "$OPEN_WEBUI_LOG")"
  stop_screen_session "$OPEN_WEBUI_SCREEN"
  local shell_bin="${SHELL:-/bin/sh}"
  local command
  command="while true; do $(shell_quote "$OPEN_WEBUI_LAUNCHER") >> $(shell_quote "$OPEN_WEBUI_LOG") 2>&1; sleep 3; done"
  run_cmd screen -dmS "$OPEN_WEBUI_SCREEN" "$shell_bin" -lc "$command"
}

restart_open_webui() {
  local selected="$1"
  case "$selected" in
    launchd)
      run_cmd launchctl kickstart -k "$(launchd_domain)/$OPEN_WEBUI_LABEL"
      ;;
    systemd)
      run_cmd systemctl --user restart "$OPEN_WEBUI_UNIT"
      ;;
    screen)
      screen_start_open_webui
      ;;
    *)
      log "Open WebUI service is not managed by $selected; no restart action is available."
      return 1
      ;;
  esac
}

ensure_open_webui() {
  local selected="$1"
  if ! open_webui_service_installed "$selected"; then
    log "Open WebUI: not installed for $selected (bootstrap will provision it when available)."
    return 0
  fi
  if open_webui_http_healthy; then
    log "Open WebUI: healthy ($(open_webui_url))"
    return 0
  fi
  log "Open WebUI: unhealthy at $(open_webui_url); restarting its managed service."
  if ! restart_open_webui "$selected"; then
    log "Open WebUI: restart could not be requested."
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local attempt
  for attempt in 1 2 3 4 5 6; do
    sleep 1
    if open_webui_http_healthy; then
      log "Open WebUI: recovered after restart ($(open_webui_url))"
      return 0
    fi
  done
  log "Open WebUI: remains unhealthy after restart; inspect $OPEN_WEBUI_LOG."
  return 0
}

open_webui_status() {
  local selected="$1"
  if ! open_webui_service_installed "$selected"; then
    log "Open WebUI: not installed for $selected"
    return 0
  fi
  if open_webui_http_healthy; then
    log "Open WebUI: healthy ($(open_webui_url))"
  else
    log "Open WebUI: unhealthy ($(open_webui_url)); restart will self-heal it"
  fi
}

write_launchd_dashboard_plist() {
  local plist="$HOME/Library/LaunchAgents/${DASHBOARD_LABEL}.plist"
  local path_env
  if [[ "$DRY_RUN" != "1" && ! -f "$DASHBOARD_WATCHDOG_SCRIPT" ]]; then
    echo "error: dashboard watchdog is missing: $DASHBOARD_WATCHDOG_SCRIPT" >&2
    return 1
  fi
  path_env="$REPO_ROOT/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
  mkdir -p "$(dirname "$plist")"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: write launchd plist $plist"
    return 0
  fi
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$DASHBOARD_LABEL")</string>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$HERMES_HOME")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$HERMES_PYTHON_BIN")</string>
    <string>$(xml_escape "$DASHBOARD_WATCHDOG_SCRIPT")</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_HOME</key>
    <string>$(xml_escape "$HERMES_HOME")</string>
    <key>PATH</key>
    <string>$(xml_escape "$path_env")</string>
    <key>VIRTUAL_ENV</key>
    <string>$(xml_escape "$REPO_ROOT/.venv")</string>
    <key>HUSSH_ONE_REPO_ROOT</key>
    <string>$(xml_escape "$REPO_ROOT")</string>
    <key>HUSSH_ONE_DASHBOARD_HOST</key>
    <string>$(xml_escape "$DASHBOARD_HOST")</string>
    <key>HUSSH_ONE_DASHBOARD_PORT</key>
    <string>$(xml_escape "$DASHBOARD_PORT")</string>
    <key>HUSSH_ONE_DASHBOARD_WATCHDOG_INTERVAL</key>
    <string>$(xml_escape "$DASHBOARD_WATCHDOG_INTERVAL")</string>
    <key>HUSSH_ONE_DASHBOARD_LOG</key>
    <string>$(xml_escape "$DASHBOARD_LOG")</string>
    <key>HUSSH_ONE_DASHBOARD_ERR_LOG</key>
    <string>$(xml_escape "$DASHBOARD_ERR_LOG")</string>
    <key>HUSSH_ONE_NOFILE_LIMIT</key>
    <string>$(xml_escape "$SERVICE_NOFILE_LIMIT")</string>
    <key>HUSSH_ONE_DASHBOARD_MEM_CAP_MB</key>
    <string>$(xml_escape "$DASHBOARD_MEM_CAP_MB")</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>SoftResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key>
    <integer>$(xml_escape "$SERVICE_NOFILE_LIMIT")</integer>
  </dict>
  <key>HardResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key>
    <integer>$(xml_escape "$SERVICE_NOFILE_LIMIT")</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$DASHBOARD_LOG")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$DASHBOARD_ERR_LOG")</string>
</dict>
</plist>
PLIST
  log "Wrote launchd dashboard plist: $plist"
}

launchd_bootstrap_dashboard() {
  local plist="$HOME/Library/LaunchAgents/${DASHBOARD_LABEL}.plist"
  write_launchd_dashboard_plist
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: launchctl bootstrap $(launchd_domain) $plist"
    return 0
  fi
  launchctl bootout "$(launchd_target)" >/dev/null 2>&1 || true
  # The legacy detached watchdog did not terminate its child on exit.  Clear
  # that orphan before bootstrapping the single launchd-owned watchdog, or the
  # new watchdog would correctly wait on an unowned listener forever.
  kill_port_listeners "$DASHBOARD_PORT"
  launchctl bootstrap "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true
}

launchd_start_dashboard() {
  retire_legacy_dashboard_watchdog
  launchd_bootstrap_dashboard
  run_cmd launchctl kickstart -k "$(launchd_target)" || true
}

launchd_stop_dashboard() {
  retire_legacy_dashboard_watchdog
  if command -v launchctl >/dev/null 2>&1; then
    run_cmd launchctl bootout "$(launchd_target)" >/dev/null 2>&1 || true
  fi
  kill_port_listeners "$DASHBOARD_PORT"
}

launchd_status_dashboard() {
  if launchd_job_active; then
    log "dashboard launchd: loaded ($DASHBOARD_LABEL)"
  else
    log "dashboard launchd: not loaded ($DASHBOARD_LABEL)"
  fi
  log "dashboard watchdog: launchd-owned ($DASHBOARD_WATCHDOG_SCRIPT)"
}

write_systemd_dashboard_unit() {
  local unit_dir="$HOME/.config/systemd/user"
  local unit_path="$unit_dir/$DASHBOARD_UNIT"
  local path_env
  path_env="$REPO_ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
  mkdir -p "$unit_dir"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: write systemd unit $unit_path"
    return 0
  fi
  cat > "$unit_path" <<UNIT
[Unit]
Description=Hussh One dashboard
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
Environment=HERMES_HOME=$HERMES_HOME
Environment=VIRTUAL_ENV=$REPO_ROOT/.venv
Environment=PATH=$path_env
ExecStart=$HERMES_PYTHON_BIN -m hermes_cli.main dashboard --host $DASHBOARD_HOST --port $DASHBOARD_PORT --tui --no-open
Restart=on-failure
RestartSec=5
LimitNOFILE=$SERVICE_NOFILE_LIMIT

[Install]
WantedBy=default.target
UNIT
  log "Wrote systemd dashboard unit: $unit_path"
}

systemd_install_dashboard() {
  write_systemd_dashboard_unit
  run_cmd systemctl --user daemon-reload
  run_cmd systemctl --user enable "$DASHBOARD_UNIT"
}

systemd_start_dashboard() {
  systemd_install_dashboard
  run_cmd systemctl --user restart "$DASHBOARD_UNIT"
}

systemd_stop_dashboard() {
  if command -v systemctl >/dev/null 2>&1; then
    run_cmd systemctl --user stop "$DASHBOARD_UNIT" >/dev/null 2>&1 || true
  fi
}

systemd_status_dashboard() {
  if systemd_job_active; then
    log "dashboard systemd: active ($DASHBOARD_UNIT)"
  else
    log "dashboard systemd: inactive ($DASHBOARD_UNIT)"
  fi
}

screen_start() {
  if [[ "$DRY_RUN" != "1" ]] && ! command -v screen >/dev/null 2>&1; then
    echo "error: screen is required for fallback manager mode" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$DASHBOARD_LOG")" "$(dirname "$GATEWAY_LOG")"
  stop_screen_session "$DASHBOARD_SCREEN"
  stop_screen_session "$GATEWAY_SCREEN"
  kill_port_listeners "$DASHBOARD_PORT"
  kill_port_listeners "$WHATSAPP_PORT"
  local shell_bin="${SHELL:-/bin/sh}"
  local dashboard_cmd gateway_cmd
  dashboard_cmd="ulimit -n $(shell_quote "$SERVICE_NOFILE_LIMIT") >/dev/null 2>&1 || true; cd $(shell_quote "$REPO_ROOT") && while true; do $(dashboard_command_line) >> $(shell_quote "$DASHBOARD_LOG") 2>> $(shell_quote "$DASHBOARD_ERR_LOG"); rc=\$?; [ \$rc -eq 0 ] && exit 0; sleep 5; done"
  gateway_cmd="ulimit -n $(shell_quote "$SERVICE_NOFILE_LIMIT") >/dev/null 2>&1 || true; cd $(shell_quote "$REPO_ROOT") && while true; do $(shell_quote "$HERMES_BIN") gateway run --replace >> $(shell_quote "$GATEWAY_LOG") 2>> $(shell_quote "$GATEWAY_ERR_LOG"); rc=\$?; [ \$rc -eq 0 ] && exit 0; sleep 5; done"
  run_cmd screen -dmS "$DASHBOARD_SCREEN" "$shell_bin" -lc "$dashboard_cmd"
  run_cmd screen -dmS "$GATEWAY_SCREEN" "$shell_bin" -lc "$gateway_cmd"
}

screen_stop() {
  stop_screen_session "$DASHBOARD_SCREEN"
  stop_screen_session "$GATEWAY_SCREEN"
}

screen_status() {
  if screen_session_exists "$DASHBOARD_SCREEN"; then
    log "dashboard screen: running ($DASHBOARD_SCREEN)"
  else
    log "dashboard screen: not running ($DASHBOARD_SCREEN)"
  fi
  if screen_session_exists "$GATEWAY_SCREEN"; then
    log "gateway screen: running ($GATEWAY_SCREEN)"
  else
    log "gateway screen: not running ($GATEWAY_SCREEN)"
  fi
}

s6_service_dir() {
  local name="$1"
  for base in /run/s6/services /var/run/s6/services /etc/s6-overlay/s6-rc.d; do
    if [[ -d "$base/$name" ]]; then
      printf '%s/%s\n' "$base" "$name"
      return 0
    fi
  done
  return 1
}

s6_control() {
  local verb="$1"
  local service_dir="$2"
  case "$verb" in
    start|install) run_cmd s6-svc -u "$service_dir" ;;
    stop) run_cmd s6-svc -d "$service_dir" ;;
    restart) run_cmd s6-svc -r "$service_dir" ;;
    status) run_cmd s6-svstat "$service_dir" ;;
  esac
}

s6_action() {
  local verb="$1"
  local gateway_dir dashboard_dir
  gateway_dir="$(s6_service_dir hermes-gateway || true)"
  dashboard_dir="$(s6_service_dir hussh-one-dashboard || s6_service_dir hermes-dashboard || true)"

  if [[ -n "$gateway_dir" ]]; then
    s6_control "$verb" "$gateway_dir"
  else
    log "s6 gateway service not found; container image should run Hermes gateway under its existing supervisor."
  fi

  if [[ -n "$dashboard_dir" ]]; then
    s6_control "$verb" "$dashboard_dir"
  else
    log "s6 dashboard service not found; add a service running: $(dashboard_command_line)"
  fi
}

port_status() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(port_pids "$port")"
  if [[ -n "$pids" ]]; then
    log "$label port $port: listening (pid(s): ${pids//$'\n'/, })"
  else
    log "$label port $port: not listening"
  fi
}

manager="$(detect_manager)"
log "Hussh One supervisor manager: $manager"

case "$ACTION" in
  install)
    require_no_conflicts "$manager"
    case "$manager" in
      launchd)
        gateway_service install
        launchd_bootstrap_dashboard
        ;;
      systemd)
        gateway_service install
        systemd_install_dashboard
        ;;
      s6)
        s6_action install
        ;;
      screen)
        log "screen fallback has no install step; use start or restart."
        ;;
    esac
    ;;
  start)
    require_no_conflicts "$manager"
    case "$manager" in
      launchd)
        gateway_service start
        launchd_start_dashboard
        ;;
      systemd)
        gateway_service start
        systemd_start_dashboard
        ;;
      s6)
        s6_action start
        ;;
      screen)
        screen_start
        ;;
    esac
    ;;
  stop)
    case "$manager" in
      launchd)
        launchd_stop_dashboard
        gateway_service stop
        ;;
      systemd)
        systemd_stop_dashboard
        gateway_service stop
        ;;
      s6)
        s6_action stop
        ;;
      screen)
        screen_stop
        ;;
    esac
    ;;
  restart)
    require_no_conflicts "$manager"
    case "$manager" in
      launchd)
        gateway_service restart
        launchd_start_dashboard
        ;;
      systemd)
        gateway_service restart
        systemd_start_dashboard
        ;;
      s6)
        s6_action restart
        ;;
      screen)
        screen_start
        ;;
    esac
    ;;
  status)
    case "$manager" in
      launchd)
        gateway_service status || true
        launchd_status_dashboard
        ;;
      systemd)
        gateway_service status || true
        systemd_status_dashboard
        ;;
      s6)
        s6_action status || true
        ;;
      screen)
        screen_status
        ;;
    esac
    port_status "$DASHBOARD_PORT" "dashboard"
    port_status "$WHATSAPP_PORT" "whatsapp"
    port_status "$OPEN_WEBUI_PORT" "Open WebUI"
    open_webui_status "$manager"
    log "dashboard url: $(dashboard_url)"
    ;;
esac

if [[ "$ACTION" == "start" || "$ACTION" == "restart" ]]; then
  ensure_open_webui "$manager"
  log "Hussh One dashboard: $(dashboard_url)"
  log "Hussh One gateway health: http://127.0.0.1:${WHATSAPP_PORT}/health"
  log "Hussh One Open WebUI: $(open_webui_url)"
fi
