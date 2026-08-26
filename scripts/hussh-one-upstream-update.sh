#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Safely reconcile and distribute the official Hermes upstream for Hussh One.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ACTION="check"
RESTART=0
MANAGER="${HUSSH_ONE_SUPERVISOR:-auto}"
DRY_RUN="${HUSSH_ONE_DRY_RUN:-0}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCHEDULE_HOUR="${HUSSH_ONE_UPDATE_HOUR:-3}"
SCHEDULE_MINUTE="${HUSSH_ONE_UPDATE_MINUTE:-17}"

ORIGIN_HTTPS="https://github.com/hushh-labs/hussh-one-hermes.git"
UPSTREAM_HTTPS="https://github.com/NousResearch/hermes-agent.git"
SCHEDULE_LABEL="ai.hussh-one.upstream-update"

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-upstream-update.sh [action] [options]

Actions:
  --check                 Fetch and report whether official Hermes is newer (default)
  --apply                 Merge a verified upstream update into Hussh One main and push origin
  --install-daily         Register a daily guarded --apply job for this machine
  --remove-daily          Remove this machine's registered daily updater
  --status                Show the local updater schedule and repository sync state

Options:
  --restart               Restart Hussh One services and run doctor after a successful --apply
  --manager NAME          auto|launchd|systemd|s6|screen (default: auto)
  --hour HOUR             Daily schedule hour, 0-23 (default: 3)
  --minute MINUTE         Daily schedule minute, 0-59 (default: 17)
  --dry-run               Print scheduler actions without modifying the machine
  -h, --help              Show this help

The updater never changes main unless origin/main is current, upstream/main
merges cleanly, and scripts/hussh-one-guard.sh passes. Conflicts leave main
untouched for a maintainer to reconcile on a normal sync/upstream-* branch.
USAGE
}

log() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

run_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'dry-run:'; printf ' %q' "$@"; printf '\n'
    return 0
  fi
  "$@"
}

python_bin() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    command -v python
  fi
}

refresh_runtime_dependencies() {
  local python
  local -a npm=()
  python="$(python_bin)"
  [[ -n "$python" && -x "$python" ]] || die "repository .venv Python is required for an update"
  # Hermes pins an npm range because a short-lived npm 11.16 regression breaks
  # workspace installs. Corepack gives every supported Node install the same
  # known-good package manager without globally replacing the user's npm.
  if command -v corepack >/dev/null 2>&1; then
    npm=(corepack npm@11.17.0)
  elif command -v npm >/dev/null 2>&1; then
    npm=(npm)
  else
    die "npm or corepack is required for an update"
  fi

  log "Reconciling Python and locked Node dependencies for the verified upstream revision..."
  "$python" -m pip install -e ".[all,dev]"
  "${npm[@]}" ci
  "${npm[@]}" run build --workspace @hermes/ink
  "${npm[@]}" --prefix ui-tui run build
  "${npm[@]}" --prefix web run build
}

validate_number() {
  local value="$1" max="$2" label="$3"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$label must be a number"
  (( 10#$value <= max )) || die "$label must be between 0 and $max"
}

is_hussh_origin() {
  case "$1" in
    "$ORIGIN_HTTPS"|git@github.com:hushh-labs/hussh-one-hermes.git|ssh://git@github.com/hushh-labs/hussh-one-hermes.git) return 0 ;;
  esac
  return 1
}

is_hermes_upstream() {
  case "$1" in
    "$UPSTREAM_HTTPS"|git@github.com:NousResearch/hermes-agent.git|ssh://git@github.com/NousResearch/hermes-agent.git) return 0 ;;
  esac
  return 1
}

verify_repository_contract() {
  local origin_url upstream_url upstream_push branch
  origin_url="$(git remote get-url origin 2>/dev/null || true)"
  upstream_url="$(git remote get-url upstream 2>/dev/null || true)"
  upstream_push="$(git remote get-url --push upstream 2>/dev/null || true)"
  branch="$(git branch --show-current)"
  is_hussh_origin "$origin_url" || die "origin must point to Hussh One: $ORIGIN_HTTPS"
  is_hermes_upstream "$upstream_url" || die "upstream must point to official Hermes: $UPSTREAM_HTTPS"
  [[ "$upstream_push" == "DISABLED" ]] || die "upstream push URL must be DISABLED"
  [[ "$branch" == "main" ]] || die "run from canonical Hussh One main, not '$branch'"
  [[ -z "$(git status --porcelain)" ]] || die "working tree must be clean before an upstream update"
}

update_attribution_base() {
  local upstream_sha="$1" python
  python="$(python_bin)"
  [[ -n "$python" && -x "$python" ]] || die "Python is required to update LICENSES/attribution.toml"
  "$python" - "$REPO_ROOT/LICENSES/attribution.toml" "$upstream_sha" <<'PY'
from pathlib import Path
import os
import re
import sys
import tempfile

path = Path(sys.argv[1])
sha = sys.argv[2]
if not re.fullmatch(r"[0-9a-f]{40}", sha):
    raise SystemExit("refusing invalid upstream commit")
text = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r'(?m)^upstream_base_commit = "[0-9a-f]{40}"$',
    f'upstream_base_commit = "{sha}"',
    text,
    count=1,
)
if count != 1:
    raise SystemExit("could not locate exactly one upstream_base_commit")
fd, temporary = tempfile.mkstemp(prefix=".attribution.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

lock_dir=""
release_lock() {
  if [[ -n "$lock_dir" && -d "$lock_dir" ]]; then
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}

acquire_lock() {
  lock_dir="$HERMES_HOME/locks/hussh-one-upstream-update.lock"
  mkdir -p "$(dirname "$lock_dir")"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    log "Hussh One upstream update already running; skipping this invocation."
    exit 0
  fi
  trap release_lock EXIT
}

check_update() {
  verify_repository_contract
  run_cmd git fetch origin --tags --prune --quiet
  run_cmd git fetch upstream --tags --prune --quiet
  local behind ahead
  behind="$(git rev-list --count main..upstream/main)"
  ahead="$(git rev-list --count upstream/main..main)"
  if git merge-base --is-ancestor upstream/main main; then
    log "Hussh One is current with official Hermes (Hussh overlay commits: $ahead)."
  else
    log "Official Hermes update available: $behind upstream commit(s); Hussh overlay commits: $ahead."
    log "Run scripts/hussh-one-upstream-update.sh --apply to reconcile it through the guard."
  fi
}

apply_update() {
  acquire_lock
  verify_repository_contract
  git fetch origin --tags --prune --quiet
  git fetch upstream --tags --prune --quiet
  git pull --ff-only origin main
  if git merge-base --is-ancestor upstream/main main; then
    log "Hussh One main already contains official Hermes upstream/main."
    return 0
  fi

  local upstream_sha ts sync_branch safety_tag
  upstream_sha="$(git rev-parse upstream/main)"
  ts="$(date +%Y%m%d-%H%M%S)"
  sync_branch="sync/upstream-$ts"
  safety_tag="safety/main-$ts"
  git tag "$safety_tag" main
  git push origin "$safety_tag"
  log "Created remote safety tag $safety_tag"

  git switch -c "$sync_branch" main
  if ! git merge --no-ff --no-edit upstream/main; then
    warn "Official upstream requires manual conflict resolution. main was not changed."
    git merge --abort || true
    git switch main
    git branch -D "$sync_branch" || true
    return 1
  fi
  update_attribution_base "$upstream_sha"
  git add LICENSES/attribution.toml
  if ! git diff --cached --quiet; then
    git commit -m "chore(license): record official Hermes upstream base"
  fi
  # An upstream import can add Python/Node requirements. Validate the actual
  # Hussh runtime, not the stale environment that happened to be present before
  # this sync (for example tool_search's Snowball dependency).
  refresh_runtime_dependencies
  if ! scripts/hussh-one-guard.sh; then
    warn "Hussh One guard failed. main was not changed; inspect $sync_branch."
    git switch main
    return 1
  fi
  git fetch origin --quiet
  if [[ "$(git rev-parse origin/main)" != "$(git rev-parse main)" ]]; then
    warn "origin/main advanced during validation. main was not changed; inspect $sync_branch."
    git switch main
    return 1
  fi
  git switch main
  git merge --no-ff "$sync_branch" -m "merge: sync official Hermes upstream"
  git push origin main
  git branch -d "$sync_branch"
  git pull --ff-only origin main
  test "$(git branch --show-current)" = "main"
  test -z "$(git status --porcelain)"
  log "Hussh One main updated and pushed from official Hermes $upstream_sha"
  if [[ "$RESTART" == "1" ]]; then
    scripts/hussh-one-supervisor.sh restart --manager "$MANAGER" --clean-conflicts
    scripts/hussh-one-doctor.sh --manager "$MANAGER" --require-services
  fi
}

install_launchd_schedule() {
  local plist="$HOME/Library/LaunchAgents/$SCHEDULE_LABEL.plist" log_dir="$HERMES_HOME/logs"
  mkdir -p "$(dirname "$plist")" "$log_dir"
  if [[ "$DRY_RUN" == "1" ]]; then log "dry-run: install launchd daily updater at $plist"; return 0; fi
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${SCHEDULE_LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>${SCRIPT_DIR}/hussh-one-upstream-update.sh</string><string>--apply</string><string>--restart</string><string>--manager</string><string>${MANAGER}</string></array>
  <key>WorkingDirectory</key><string>${REPO_ROOT}</string>
  <key>EnvironmentVariables</key><dict><key>HERMES_HOME</key><string>${HERMES_HOME}</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>${SCHEDULE_HOUR}</integer><key>Minute</key><integer>${SCHEDULE_MINUTE}</integer></dict>
  <key>StandardOutPath</key><string>${log_dir}/hussh-one-upstream-update.log</string>
  <key>StandardErrorPath</key><string>${log_dir}/hussh-one-upstream-update.error.log</string>
</dict></plist>
PLIST
  local domain="gui/$(id -u)"
  launchctl bootout "$domain/$SCHEDULE_LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "$domain" "$plist"
  launchctl enable "$domain/$SCHEDULE_LABEL" >/dev/null 2>&1 || true
  log "Installed daily Hussh One updater with launchd (${SCHEDULE_HOUR}:${SCHEDULE_MINUTE})."
}

install_systemd_schedule() {
  local unit_dir="$HOME/.config/systemd/user" service="$HOME/.config/systemd/user/hussh-one-upstream-update.service" timer="$HOME/.config/systemd/user/hussh-one-upstream-update.timer"
  mkdir -p "$unit_dir"
  if [[ "$DRY_RUN" == "1" ]]; then log "dry-run: install systemd user daily updater at $timer"; return 0; fi
  cat >"$service" <<SERVICE
[Unit]
Description=Hussh One guarded official Hermes update
[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
Environment="HERMES_HOME=${HERMES_HOME}"
ExecStart=/bin/bash ${SCRIPT_DIR}/hussh-one-upstream-update.sh --apply --restart --manager ${MANAGER}
SERVICE
  cat >"$timer" <<TIMER
[Unit]
Description=Daily Hussh One official Hermes update
[Timer]
OnCalendar=*-*-* ${SCHEDULE_HOUR}:${SCHEDULE_MINUTE}:00
Persistent=true
Unit=hussh-one-upstream-update.service
[Install]
WantedBy=timers.target
TIMER
  systemctl --user daemon-reload
  systemctl --user enable --now hussh-one-upstream-update.timer
  log "Installed daily Hussh One updater with systemd (${SCHEDULE_HOUR}:${SCHEDULE_MINUTE})."
}

install_cron_schedule() {
  command -v crontab >/dev/null 2>&1 || die "no supported daily scheduler found (launchd, systemd, or crontab)"
  local marker="# hussh-one-upstream-update" existing updated tmp
  existing="$(crontab -l 2>/dev/null || true)"
  updated="$(printf '%s\n' "$existing" | grep -vF "$marker" | grep -vF "hussh-one-upstream-update.sh --apply" || true)"
  updated+=$'\n'"${SCHEDULE_MINUTE} ${SCHEDULE_HOUR} * * * HERMES_HOME=$(printf '%q' "$HERMES_HOME") /bin/bash $(printf '%q' "$SCRIPT_DIR/hussh-one-upstream-update.sh") --apply --restart --manager $(printf '%q' "$MANAGER") >>$(printf '%q' "$HERMES_HOME/logs/hussh-one-upstream-update.log") 2>>$(printf '%q' "$HERMES_HOME/logs/hussh-one-upstream-update.error.log") ${marker}"
  if [[ "$DRY_RUN" == "1" ]]; then log "dry-run: install cron daily updater (${SCHEDULE_HOUR}:${SCHEDULE_MINUTE})."; return 0; fi
  mkdir -p "$HERMES_HOME/logs"; tmp="$(mktemp)"; printf '%s\n' "$updated" >"$tmp"; crontab "$tmp"; rm -f "$tmp"
  log "Installed daily Hussh One updater with cron (${SCHEDULE_HOUR}:${SCHEDULE_MINUTE})."
}

install_daily() {
  validate_number "$SCHEDULE_HOUR" 23 "hour"; validate_number "$SCHEDULE_MINUTE" 59 "minute"
  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then install_launchd_schedule
  elif [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then install_systemd_schedule
  else install_cron_schedule; fi
}

remove_daily() {
  local plist="$HOME/Library/LaunchAgents/$SCHEDULE_LABEL.plist"
  if [[ "$DRY_RUN" == "1" ]]; then log "dry-run: remove Hussh One daily updater schedule"; return 0; fi
  if [[ "$(uname -s)" == "Darwin" && -e "$plist" ]]; then launchctl bootout "gui/$(id -u)/$SCHEDULE_LABEL" >/dev/null 2>&1 || true; rm -f "$plist"; fi
  if command -v systemctl >/dev/null 2>&1; then systemctl --user disable --now hussh-one-upstream-update.timer >/dev/null 2>&1 || true; rm -f "$HOME/.config/systemd/user/hussh-one-upstream-update.service" "$HOME/.config/systemd/user/hussh-one-upstream-update.timer"; systemctl --user daemon-reload >/dev/null 2>&1 || true; fi
  if command -v crontab >/dev/null 2>&1; then local tmp; tmp="$(mktemp)"; (crontab -l 2>/dev/null || true) | grep -vF "# hussh-one-upstream-update" | grep -vF "hussh-one-upstream-update.sh --apply" >"$tmp" || true; crontab "$tmp"; rm -f "$tmp"; fi
  log "Removed Hussh One daily updater schedule."
}

schedule_status() {
  verify_repository_contract
  if [[ "$(uname -s)" == "Darwin" && -f "$HOME/Library/LaunchAgents/$SCHEDULE_LABEL.plist" ]]; then log "launchd daily updater is installed."
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user list-timers hussh-one-upstream-update.timer --no-legend 2>/dev/null | grep -q hussh-one-upstream-update; then log "systemd daily updater is installed."
  elif command -v crontab >/dev/null 2>&1 && (crontab -l 2>/dev/null || true) | grep -qF "# hussh-one-upstream-update"; then log "cron daily updater is installed."
  else log "daily updater is not installed."; fi
  check_update
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) ACTION="check"; shift ;; --apply) ACTION="apply"; shift ;; --install-daily) ACTION="install-daily"; shift ;; --remove-daily) ACTION="remove-daily"; shift ;; --status) ACTION="status"; shift ;;
    --restart) RESTART=1; shift ;; --manager) MANAGER="${2:-}"; shift 2 ;; --hour) SCHEDULE_HOUR="${2:-}"; shift 2 ;; --minute) SCHEDULE_MINUTE="${2:-}"; shift 2 ;; --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;; *) die "unknown argument: $1" ;;
  esac
done
case "$MANAGER" in auto|launchd|systemd|s6|screen) ;; *) die "unsupported manager '$MANAGER'" ;; esac
case "$ACTION" in check) check_update ;; apply) apply_update ;; install-daily) install_daily ;; remove-daily) remove_daily ;; status) schedule_status ;; esac
