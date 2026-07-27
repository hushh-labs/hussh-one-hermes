#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Bootstrap Open WebUI against Hermes Agent's OpenAI-compatible API server.
#
# Idempotent by design:
# - ensures $HERMES_HOME/.env has API server settings
# - installs Open WebUI into ~/.local/open-webui-venv
# - writes a reusable launcher at ~/.local/bin/start-open-webui-hermes.sh
# - optionally installs a user service (launchd on macOS, systemd --user on Linux)
#
# Usage:
#   bash scripts/setup_open_webui.sh
#
# Optional environment overrides:
#   OPEN_WEBUI_PORT=8080
#   OPEN_WEBUI_HOST=127.0.0.1
#   OPEN_WEBUI_NAME='Johnny Hermes'
#   OPEN_WEBUI_ENABLE_SIGNUP=true
#   OPEN_WEBUI_ENABLE_SERVICE=auto   # auto|true|false
#   OPEN_WEBUI_ENABLE_TITLE_GENERATION=False  # True = LLM auto-titles (extra agent call/msg)
#   OPEN_WEBUI_ENABLE_TAGS_GENERATION=False   # True = LLM auto-tags (extra agent call/msg)
#   OPEN_WEBUI_AUTH=False  # default; passwordless and loopback-only
#   OPEN_WEBUI_VERSION=0.10.2
#   OPEN_WEBUI_MODELS_CACHE_TTL=300
#   OPEN_WEBUI_VENV=~/.local/open-webui-venv
#   OPEN_WEBUI_DATA_DIR=~/.local/share/open-webui/data
#   HERMES_API_PORT=8642
#   HERMES_API_HOST=127.0.0.1
#   HERMES_API_MODEL_NAME='🤫 Hussh One'
#   HERMES_HOME=~/.hermes
#   HERMES_BIN=/path/to/this/checkout/.venv/bin/hermes

OPEN_WEBUI_PORT="${OPEN_WEBUI_PORT:-8080}"
OPEN_WEBUI_HOST="${OPEN_WEBUI_HOST:-127.0.0.1}"
OPEN_WEBUI_NAME="${OPEN_WEBUI_NAME:-🤫 Hussh One}"
OPEN_WEBUI_ENABLE_SIGNUP="${OPEN_WEBUI_ENABLE_SIGNUP:-true}"
OPEN_WEBUI_ENABLE_SERVICE="${OPEN_WEBUI_ENABLE_SERVICE:-auto}"
# Hussh One performance defaults: keep Open WebUI at 1 Hermes agent call per
# message. Override to True only if you want LLM-generated chat titles/tags
# (each costs a full extra server-side agent run on the heavy engine).
OPEN_WEBUI_ENABLE_TITLE_GENERATION="${OPEN_WEBUI_ENABLE_TITLE_GENERATION:-False}"
OPEN_WEBUI_ENABLE_TAGS_GENERATION="${OPEN_WEBUI_ENABLE_TAGS_GENERATION:-False}"
# Hussh One is a personal, loopback-only agent by default. Existing single-user
# databases are migrated without changing the user ID, preserving chats and
# settings. Authenticated or multi-user deployments must opt in explicitly.
OPEN_WEBUI_AUTH="${OPEN_WEBUI_AUTH:-False}"
OPEN_WEBUI_VERSION="${OPEN_WEBUI_VERSION:-0.10.2}"
OPEN_WEBUI_MODELS_CACHE_TTL="${OPEN_WEBUI_MODELS_CACHE_TTL:-300}"
OPEN_WEBUI_API_TIMEOUT="${OPEN_WEBUI_API_TIMEOUT:-300}"
OPEN_WEBUI_MODEL_LIST_TIMEOUT="${OPEN_WEBUI_MODEL_LIST_TIMEOUT:-10}"
OPEN_WEBUI_VENV="${OPEN_WEBUI_VENV:-$HOME/.local/open-webui-venv}"
OPEN_WEBUI_DATA_DIR="${OPEN_WEBUI_DATA_DIR:-$HOME/.local/share/open-webui/data}"
OPEN_WEBUI_CORS_ALLOW_ORIGIN="${OPEN_WEBUI_CORS_ALLOW_ORIGIN:-http://${OPEN_WEBUI_HOST}:${OPEN_WEBUI_PORT};http://localhost:${OPEN_WEBUI_PORT}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SETUP_SCRIPT="${HUSSH_ONE_SOURCE_SETUP:-$SCRIPT_DIR/setup_open_webui.sh}"
REPO_ROOT="${HUSSH_ONE_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HERMES_HOME/.env}"
HERMES_BIN="${HERMES_BIN:-$REPO_ROOT/.venv/bin/hermes}"
HERMES_API_PORT="${HERMES_API_PORT:-8642}"
HERMES_API_HOST="${HERMES_API_HOST:-127.0.0.1}"
HERMES_API_CONNECT_HOST="${HERMES_API_CONNECT_HOST:-127.0.0.1}"
HERMES_API_MODEL_NAME="${HERMES_API_MODEL_NAME:-🤫 Hussh One}"
HERMES_API_BASE_URL="http://${HERMES_API_CONNECT_HOST}:${HERMES_API_PORT}/v1"
LAUNCHER_PATH="$HOME/.local/bin/start-open-webui-hermes.sh"
LOG_DIR="$HERMES_HOME/logs"
RUNTIME_CONFIG_PATH="$HERMES_HOME/open-webui.env"
SETUP_REVISION_PATH="$HERMES_HOME/open-webui-setup.sha256"
MANAGED_SETUP_SCRIPT="$HERMES_HOME/scripts/setup_open_webui.sh"

log() {
  printf '[open-webui-bootstrap] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_hermes_bin() {
  if [[ -x "$HERMES_BIN" ]]; then
    return 0
  fi
  echo "Repository Hermes binary not found or not executable: $HERMES_BIN" >&2
  echo "Set HERMES_BIN to this checkout's Hermes executable after creating its virtualenv." >&2
  exit 1
}

choose_python() {
  if [[ -x "$OPEN_WEBUI_VENV/bin/python" ]] &&
      "$OPEN_WEBUI_VENV/bin/python" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 13)))' >/dev/null 2>&1; then
    echo "$OPEN_WEBUI_VENV/bin/python"
  elif command -v python3.11 >/dev/null 2>&1 &&
      python3.11 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))' >/dev/null 2>&1; then
    echo python3.11
  elif command -v python3 >/dev/null 2>&1 &&
      python3 -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 13)))' >/dev/null 2>&1; then
    echo python3
  else
    echo "A runnable Python 3.11 or 3.12 interpreter is required." >&2
    exit 1
  fi
}

upsert_env() {
  local key="$1"
  local value="$2"
  local file="$3"

  mkdir -p "$(dirname "$file")"
  touch "$file"

  python3 - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
out = []
seen = False
for raw in lines:
    stripped = raw.strip()
    if stripped.startswith(f"{key}="):
        if not seen:
            out.append(f"{key}={value}")
            seen = True
        continue
    out.append(raw)
if not seen:
    if out and out[-1] != "":
        out.append("")
    out.append(f"{key}={value}")
path.write_text("\n".join(out).rstrip() + "\n")
PY
}

get_env_value() {
  local key="$1"
  local file="$2"
  python3 - "$file" "$key" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    raise SystemExit(0)
for raw in path.read_text().splitlines():
    line = raw.strip()
    if line.startswith(f"{key}="):
        print(line.split("=", 1)[1])
        raise SystemExit(0)
PY
}

generate_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

file_sha256() {
  python3 - "$1" <<'PY'
import hashlib
from pathlib import Path
import sys

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

shell_quote() {
  python3 - "$1" <<'PY'
import shlex
import sys
print(shlex.quote(sys.argv[1]))
PY
}

can_use_systemd_user() {
  [[ "$(uname -s)" == "Linux" ]] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1

  local uid runtime_dir bus_path
  uid="$(id -u)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$uid}"
  bus_path="$runtime_dir/bus"

  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "$runtime_dir" ]]; then
    export XDG_RUNTIME_DIR="$runtime_dir"
  fi
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$bus_path" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$bus_path"
  fi

  systemctl --user show-environment >/dev/null 2>&1
}

install_macos_dependencies() {
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    if ! command -v pandoc >/dev/null 2>&1; then
      log 'Installing pandoc with Homebrew (recommended by Open WebUI docs)...'
      brew install pandoc
    fi
  fi
}

install_open_webui() {
  local py
  py="$(choose_python)"
  log "Using Python interpreter: $py"
  if [[ "$py" != "$OPEN_WEBUI_VENV/bin/python" ]]; then
    "$py" -m venv "$OPEN_WEBUI_VENV"
  fi
  # shellcheck disable=SC1090
  source "$OPEN_WEBUI_VENV/bin/activate"
  if "$OPEN_WEBUI_VENV/bin/python" - "$OPEN_WEBUI_VERSION" <<'PY'
import importlib.metadata
import sys

try:
    installed = importlib.metadata.version("open-webui")
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)
raise SystemExit(installed != sys.argv[1])
PY
  then
    log "Open WebUI ${OPEN_WEBUI_VERSION} is already installed; reusing the tested runtime."
    return 0
  fi
  # Open WebUI's pinned torch stack currently requires setuptools<82.
  # Keep bootstrap tooling inside that tested constraint instead of briefly
  # installing an incompatible latest setuptools and relying on backtracking.
  "$py" -m pip install --upgrade pip wheel "setuptools<82"
  "$py" -m pip install "open-webui==${OPEN_WEBUI_VERSION}"
}

prepare_passwordless_database() {
  case "$OPEN_WEBUI_AUTH" in
    false|False|FALSE|0) ;;
    *) return 0 ;;
  esac

  case "$OPEN_WEBUI_HOST" in
    127.0.0.1|localhost|::1|\[::1\]) ;;
    *)
      echo "OPEN_WEBUI_AUTH=False is allowed only on a loopback host." >&2
      echo "Set OPEN_WEBUI_AUTH=True before binding Open WebUI to a network interface." >&2
      exit 1
      ;;
  esac

  local database="$OPEN_WEBUI_DATA_DIR/webui.db"
  [[ -f "$database" ]] || return 0

  WEBUI_AUTH=False "$OPEN_WEBUI_VENV/bin/python" - "$database" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import bcrypt
import sqlite3
import sys

database = Path(sys.argv[1])
with sqlite3.connect(database) as connection:
    users = connection.execute('SELECT id, email FROM "user"').fetchall()
    auths = connection.execute("SELECT id, email FROM auth").fetchall()

if not users:
    raise SystemExit(0)
if len(users) != 1 or len(auths) != 1 or users[0][0] != auths[0][0]:
    raise SystemExit(
        "Passwordless Hussh One migration requires exactly one matching local "
        "Open WebUI user. Re-run with OPEN_WEBUI_AUTH=True."
    )
if users[0][1] == "admin@localhost" and auths[0][1] == "admin@localhost":
    raise SystemExit(0)

backup_dir = database.parent / "backups"
backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = backup_dir / f"webui.db.pre-passwordless-{timestamp}.bak"
with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
    source.backup(target)
backup.chmod(0o600)

user_id = users[0][0]
with sqlite3.connect(database) as connection:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        'UPDATE "user" SET email = ? WHERE id = ?',
        ("admin@localhost", user_id),
    )
    connection.execute(
        "UPDATE auth SET email = ?, password = ?, active = 1 WHERE id = ?",
        (
            "admin@localhost",
            bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode("utf-8"),
            user_id,
        ),
    )
    connection.commit()

print(
    "[open-webui-bootstrap] Preserved the existing user and chats while "
    f"enabling loopback passwordless access. Backup: {backup}"
)
PY
}

enforce_passwordless_ui_config() {
  case "$OPEN_WEBUI_AUTH" in
    false|False|FALSE|0) ;;
    *) return 1 ;;
  esac

  local database="$OPEN_WEBUI_DATA_DIR/webui.db"
  [[ -f "$database" ]] || return 1

  "$OPEN_WEBUI_VENV/bin/python" - "$database" <<'PY'
from pathlib import Path
import sqlite3
import sys

database = Path(sys.argv[1])
with sqlite3.connect(database) as connection:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(config)").fetchall()
    }
    if not {"key", "value"}.issubset(columns):
        raise SystemExit(1)
    current = connection.execute(
        'SELECT value FROM config WHERE "key" = ?',
        ("ui.enable_login_form",),
    ).fetchone()
    if current is not None and str(current[0]).lower() == "false":
        raise SystemExit(1)
    connection.execute(
        """
        INSERT INTO config ("key", value, updated_at)
        VALUES (?, json('false'), unixepoch())
        ON CONFLICT ("key") DO UPDATE
        SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("ui.enable_login_form",),
    )
    connection.commit()
PY
}

restart_open_webui_service() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl kickstart -k "gui/$(id -u)/ai.openwebui.hermes"
  elif can_use_systemd_user; then
    systemctl --user restart openwebui-hermes.service
  else
    return 1
  fi
}

# Open WebUI auto-loads /static/custom.css and /static/loader.js from its bundled
# frontend (index.html references both). We drop in a small reasoning-block
# enhancer so the "Thinking…" panel behaves like the Hermes TUI:
#   * auto-EXPANDS while the model is actively thinking (streaming),
#   * auto-COLLAPSES the moment thinking finishes,
#   * is capped to a fixed, scrollable height instead of growing forever.
# Idempotent: re-running setup re-writes these files. We locate the static dir
# under the freshly created venv so a clean install always gets it.
install_static_assets() {
  local static_dir
  static_dir="$(
    "$OPEN_WEBUI_VENV/bin/python" - <<'PY'
import os
import open_webui
base = os.path.dirname(open_webui.__file__)
# Prefer the served frontend static dir; fall back to the package static dir.
for candidate in (
    os.path.join(base, "frontend", "static"),
    os.path.join(base, "static"),
):
    if os.path.isdir(candidate):
        print(candidate)
        break
PY
  )"
  if [[ -z "$static_dir" || ! -d "$static_dir" ]]; then
    log 'Could not locate Open WebUI static dir; skipping reasoning-panel assets.'
    return 0
  fi

  log "Installing Hermes reasoning-panel assets into: $static_dir"

  cat > "$static_dir/custom.css" <<'CSS'
/*
 * Hermes reasoning / "thinking" panel: fixed, scrollable height so a long chain
 * of thought never pushes the answer far down the page. loader.js tags the
 * reasoning content container with `data-hushh-thinking-body` (Open WebUI's
 * built markup uses dynamic Svelte hashes we cannot target directly).
 */
[data-hushh-thinking-body] {
  max-height: 16rem;
  overflow-y: auto;
  scrollbar-width: thin;
  scroll-behavior: smooth;
  border-radius: 0.5rem;
}

[data-hushh-thinking-body][data-hushh-thinking-live="true"] {
  /* While streaming, jump straight to the newest thought (no smooth lag). */
  scroll-behavior: auto;
}

[data-hushh-thinking-body]::-webkit-scrollbar {
  width: 8px;
}

[data-hushh-thinking-body]::-webkit-scrollbar-thumb {
  background-color: rgb(156 163 175 / 0.5);
  border-radius: 9999px;
}

.dark [data-hushh-thinking-body]::-webkit-scrollbar-thumb {
  background-color: rgb(107 114 128 / 0.5);
}

#hushh-composer-controls {
  display: inline-flex;
  flex: 0 1 auto;
  align-items: center;
  gap: 0.35rem;
  margin-inline: 0.25rem;
  min-width: 0;
  max-width: min(23rem, 42vw);
  white-space: nowrap;
  vertical-align: middle;
}

#hushh-composer-controls select {
  min-width: 0;
  max-width: 10rem;
  height: 2rem;
  border: 1px solid rgb(209 213 219 / 0.8);
  border-radius: 9999px;
  background: rgb(255 255 255 / 0.9);
  color: rgb(55 65 81);
  padding: 0 1.6rem 0 0.65rem;
  font-size: 0.75rem;
  line-height: 1;
  text-overflow: ellipsis;
}

#hushh-model-select {
  width: clamp(7rem, 14vw, 10rem);
}

#hushh-reasoning-select {
  width: clamp(6.6rem, 12vw, 8.6rem);
}

.dark #hushh-composer-controls select {
  border-color: rgb(75 85 99 / 0.8);
  background: rgb(17 24 39 / 0.9);
  color: rgb(229 231 235);
}

#hushh-changelog-menu-item {
  min-width: 0;
}

#sidebar-changelog-button {
  min-width: 0;
}

.hushh-changelog-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hushh-changelog-topbar {
  padding: 1rem clamp(1rem, 3vw, 2rem);
}

.hushh-changelog-content {
  width: min(100%, 64rem);
  margin-inline: auto;
  padding: clamp(1rem, 4vw, 3rem);
}

#hushh-changelog-view table {
  width: 100%;
}

@media (max-width: 720px) {
  #hushh-composer-controls {
    gap: 0.2rem;
    margin-inline: 0.1rem;
    max-width: 44vw;
  }

  #hushh-composer-controls select {
    height: 1.9rem;
    padding-inline: 0.5rem 1.25rem;
    font-size: 0.6875rem;
  }

  #hushh-model-select {
    width: min(26vw, 7rem);
  }

  #hushh-reasoning-select {
    width: min(18vw, 5.8rem);
  }

  .hushh-changelog-topbar {
    align-items: flex-start;
    gap: 0.75rem;
  }

  #hushh-changelog-view .overflow-hidden {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  #hushh-changelog-view table {
    min-width: 34rem;
  }
}
CSS

  cat > "$static_dir/loader.js" <<'JS'
(() => {
  // ---------------------------------------------------------------------------
  // Part 1: Canonical Hussh One title defaults
  // ---------------------------------------------------------------------------
  const brandTitles = new Set(["Hussh One", "🤫 Hussh One"]);
  const appTitle = "🤫 Hussh One";
  const fallbackChatTitle = appTitle;
  let lastGoodDocumentTitle = appTitle;

  // ---------------------------------------------------------------------------
  // Part 2: Browser tab title hygiene
  // ---------------------------------------------------------------------------
  const htmlEntityTextarea = document.createElement("textarea");

  const compactText = (value) => (value || "").replace(/\s+/g, " ").trim();

  const decodeHtmlEntities = (value) => {
    htmlEntityTextarea.innerHTML = value || "";
    return htmlEntityTextarea.value;
  };

  const stripTitleMarkup = (value) => {
    let text = decodeHtmlEntities(String(value || ""));
    text = text.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, " ");
    text = text.replace(/<think\b[^>]*>[\s\S]*$/gi, " ");
    text = text.replace(/<\/?think\b[^>]*>/gi, " ");
    text = text.replace(/<[^>]+>/g, " ");
    text = text.replace(/\bThought\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b/gi, " ");
    return compactText(text);
  };

  const isLeakyTitle = (value) => {
    const raw = String(value || "");
    const decoded = decodeHtmlEntities(raw);
    return (
      /<\/?[a-z][\s\S]*>/i.test(decoded) ||
      /\bThought\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b/i.test(decoded) ||
      /^Thinking(?:\.\.\.)?$/i.test(compactText(decoded))
    );
  };

  const isGenericTitle = (value) => {
    const text = compactText(stripTitleMarkup(value)).toLowerCase();
    return !text || text === "open webui" || text === appTitle.toLowerCase();
  };

  const isUsableChatTitle = (value) => {
    const text = compactText(stripTitleMarkup(value));
    if (text.length < 3 || text.length > 120) return false;
    if (/^(new chat|open webui|🤫?\s*hussh one)$/i.test(text)) return false;
    if (brandTitles.has(text)) return false;
    return !isLeakyTitle(text);
  };

  const textForElement = (element) => compactText(stripTitleMarkup(element && element.textContent));

  const activeChatPath = () => {
    const match = window.location.pathname.match(/^\/c\/[^/?#]+/);
    return match ? match[0] : "";
  };

  const currentSidebarChatTitle = () => {
    const path = activeChatPath();
    if (!path) return "";

    const links = Array.from(document.querySelectorAll("a[href]"));
    const activeLinks = links.filter((link) => {
      try {
        return new URL(link.getAttribute("href"), window.location.origin).pathname === path;
      } catch (_e) {
        return false;
      }
    });

    for (const link of activeLinks) {
      const text = textForElement(link);
      if (isUsableChatTitle(text)) return text;
    }

    return "";
  };

  const fallbackVisibleChatTitle = () => {
    if (isUsableChatTitle(lastGoodDocumentTitle) && !isGenericTitle(lastGoodDocumentTitle)) {
      return lastGoodDocumentTitle;
    }
    return fallbackChatTitle;
  };

  const shouldSkipTextNode = (node) => {
    const parent = node && node.parentElement;
    if (!parent) return true;
    const tag = (parent.tagName || "").toLowerCase();
    if (["script", "style", "textarea", "input", "code", "pre"].includes(tag)) return true;
    const text = compactText(node.nodeValue || "");
    if (!text) return true;
    if (!isLeakyTitle(text)) return true;

    const rect = parent.getBoundingClientRect ? parent.getBoundingClientRect() : null;
    if (rect && (rect.width > 560 || rect.height > 96)) return true;
    return false;
  };

  const sanitizeVisibleLeakyTitles = () => {
    if (!document.createTreeWalker) return;
    const showText =
      (window.NodeFilter && window.NodeFilter.SHOW_TEXT) ||
      (typeof NodeFilter !== "undefined" && NodeFilter.SHOW_TEXT) ||
      4;
    const walker = document.createTreeWalker(document.body || document.documentElement, showText);
    const replacements = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!shouldSkipTextNode(node)) replacements.push(node);
    }

    const replacement = currentSidebarChatTitle() || fallbackVisibleChatTitle();
    for (const node of replacements) {
      node.nodeValue = replacement;
    }
  };

  const syncDocumentTitle = () => {
    const current = document.title || "";
    const sanitized = stripTitleMarkup(current);
    const sidebarTitle = currentSidebarChatTitle();
    const shouldUseSidebar =
      sidebarTitle &&
      (isLeakyTitle(current) || isGenericTitle(current) || sanitized !== current.trim());
    const next = shouldUseSidebar
      ? sidebarTitle
      : isUsableChatTitle(sanitized)
        ? sanitized
        : lastGoodDocumentTitle;

    if (next && next !== document.title) {
      document.title = next;
    }
    if (isUsableChatTitle(next)) {
      lastGoodDocumentTitle = next;
    }
  };

  // ---------------------------------------------------------------------------
  // Part 2b: Disabled-auth sign-in chrome suppression
  // ---------------------------------------------------------------------------
  const disabledAuthChromeSuppressionEnabled = true;
  const AUTH_PROMPT_RE =
    /\b(?:sign\s*in|sign\s*up|log\s*in|login|register|create\s+(?:an\s+)?account|continue\s+with\s+(?:google|github|microsoft|sso)|you\s+need\s+to\s+(?:sign|log)\s+in|not\s+signed\s+in)\b/i;
  const AUTH_REQUIRED_RE =
    /\b(?:authentication|authorization)\s+(?:required|needed|to\s+continue)\b/i;
  const AUTH_SUCCESS_RE =
    /\b(?:you(?:'|’)?re|you\s+are)\s+now\s+logged\s+in\.?\b/i;

  const isDisabledAuthPromptText = (value) => {
    const text = compactText(decodeHtmlEntities(String(value || "")));
    if (!text || text.length > 260) return false;

    if (/\bbridge\s+authentication\s+required\b/i.test(text)) return false;

    return AUTH_PROMPT_RE.test(text) || AUTH_REQUIRED_RE.test(text) || AUTH_SUCCESS_RE.test(text);
  };

  const isToastLikeElement = (element) => {
    if (!element) return false;
    const getAttr = (name) =>
      element.getAttribute ? String(element.getAttribute(name) || "").toLowerCase() : "";
    if (element.hasAttribute && element.hasAttribute("data-sonner-toast")) return true;
    const role = getAttr("role");
    if (role === "alert" || role === "status") return true;
    const className = String(element.className || "").toLowerCase();
    return className.includes("sonner") || className.includes("toast") || className.includes("toaster");
  };

  const closestElement = (element, predicate, maxDepth = 8) => {
    let current = element;
    let depth = 0;
    while (current && depth <= maxDepth) {
      if (predicate(current)) return current;
      current = current.parentElement;
      depth += 1;
    }
    return null;
  };

  const isCompactVisibleChromeElement = (element) => {
    if (!element) return false;
    const tag = String(element.tagName || "").toLowerCase();
    if (["script", "style", "textarea", "input", "code", "pre"].includes(tag)) return false;
    if (!["a", "button", "div", "form", "h1", "h2", "h3", "label", "p", "section", "span"].includes(tag)) {
      return false;
    }

    const text = compactText(element.textContent || "");
    if (!isDisabledAuthPromptText(text)) return false;
    if (!element.getBoundingClientRect) return true;
    const rect = element.getBoundingClientRect();
    return !rect || (rect.width <= 760 && rect.height <= 360);
  };

  const hideDisabledAuthElement = (element) => {
    if (!element) return;
    if (element.setAttribute) {
      element.setAttribute("data-hushh-disabled-auth-hidden", "");
      element.setAttribute("aria-hidden", "true");
    }
    if (element.style) {
      element.style.display = "none";
      element.style.visibility = "hidden";
    }
  };

  const disabledAuthPromptContainer = (node) => {
    const parent = node && node.parentElement;
    if (!parent) return null;
    const toast = closestElement(parent, isToastLikeElement, 10);
    if (toast) return toast;
    return closestElement(parent, isCompactVisibleChromeElement, 5);
  };

  const suppressDisabledAuthChrome = () => {
    if (!disabledAuthChromeSuppressionEnabled || !document.createTreeWalker) return;
    const root = document.body || document.documentElement;
    if (!root) return;

    const showText =
      (window.NodeFilter && window.NodeFilter.SHOW_TEXT) ||
      (typeof NodeFilter !== "undefined" && NodeFilter.SHOW_TEXT) ||
      4;
    const walker = document.createTreeWalker(root, showText);
    const containers = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!isDisabledAuthPromptText(node && node.nodeValue)) continue;
      const container = disabledAuthPromptContainer(node);
      if (container) containers.push(container);
    }

    for (const container of containers) {
      hideDisabledAuthElement(container);
    }
  };

  // ---------------------------------------------------------------------------
  // Part 2c: Registry-driven model + reasoning controls
  // ---------------------------------------------------------------------------
  const MODEL_STORAGE_KEY = "hushh.openwebui.model";
  const REASONING_STORAGE_KEY = "hushh.openwebui.reasoning";
  let selectedModel = localStorage.getItem(MODEL_STORAGE_KEY) || "";
  let selectedReasoning = localStorage.getItem(REASONING_STORAGE_KEY) || "medium";
  let composerControlsPending = false;

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input && input.url;
    if (
      url &&
      /\/api\/chat\/completions(?:\?|$)/.test(url) &&
      init &&
      typeof init.body === "string"
    ) {
      try {
        const payload = JSON.parse(init.body);
        if (selectedModel) payload.model = selectedModel;
        payload.reasoning_effort = selectedReasoning;
        init = { ...init, body: JSON.stringify(payload) };
      } catch (_error) {
        // Preserve the upstream request untouched if its body is not JSON.
      }
    }
    return nativeFetch(input, init);
  };

  const modelEntries = async () => {
    try {
      const response = await nativeFetch("/api/models");
      if (!response.ok) return [];
      const payload = await response.json();
      const values = Array.isArray(payload) ? payload : payload.data || payload.models || [];
      return values
        .map((entry) => ({
          id: String(entry && (entry.id || entry.name) || "").trim(),
          name: String(entry && (entry.name || entry.id) || "").trim(),
        }))
        .filter((entry) => entry.id);
    } catch (_error) {
      return [];
    }
  };

  const composerForm = () => {
    const input = document.querySelector(
      "textarea, #chat-input[contenteditable='true'], [contenteditable='true'][data-placeholder]",
    );
    if (!input) return null;
    return input.closest("form") || input.parentElement?.parentElement || null;
  };

  const installComposerControls = async () => {
    if (composerControlsPending || document.getElementById("hushh-composer-controls")) return;
    const form = composerForm();
    if (!form) return;

    composerControlsPending = true;
    const models = await modelEntries();
    composerControlsPending = false;
    if (!models.length || document.getElementById("hushh-composer-controls")) return;
    if (!selectedModel || !models.some((entry) => entry.id === selectedModel)) {
      selectedModel = models[0].id;
      localStorage.setItem(MODEL_STORAGE_KEY, selectedModel);
    }

    const controls = document.createElement("div");
    controls.id = "hushh-composer-controls";
    controls.setAttribute("aria-label", "Hussh One model controls");

    const modelSelect = document.createElement("select");
    modelSelect.id = "hushh-model-select";
    modelSelect.title = "Model";
    modelSelect.setAttribute("aria-label", "Model");
    for (const entry of models) {
      const option = document.createElement("option");
      option.value = entry.id;
      option.textContent = entry.name;
      option.selected = entry.id === selectedModel;
      modelSelect.appendChild(option);
    }
    modelSelect.addEventListener("change", () => {
      selectedModel = modelSelect.value;
      localStorage.setItem(MODEL_STORAGE_KEY, selectedModel);
    });

    const reasoningSelect = document.createElement("select");
    reasoningSelect.id = "hushh-reasoning-select";
    reasoningSelect.title = "Thinking level";
    reasoningSelect.setAttribute("aria-label", "Thinking level");
    for (const [value, label] of [
      ["none", "Thinking off"],
      ["low", "Thinking low"],
      ["medium", "Thinking medium"],
      ["high", "Thinking high"],
      ["xhigh", "Thinking max"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === selectedReasoning;
      reasoningSelect.appendChild(option);
    }
    reasoningSelect.addEventListener("change", () => {
      selectedReasoning = reasoningSelect.value;
      localStorage.setItem(REASONING_STORAGE_KEY, selectedReasoning);
    });

    controls.append(modelSelect, reasoningSelect);
    const buttons = Array.from(form.querySelectorAll("button"));
    const dictate = buttons.find((button) =>
      /dictat|voice|microphone|record/i.test(
        `${button.getAttribute("aria-label") || ""} ${button.title || ""}`,
      ),
    );
    if (dictate && dictate.parentElement) {
      dictate.insertAdjacentElement("afterend", controls);
    } else {
      form.appendChild(controls);
    }
  };

  // ---------------------------------------------------------------------------
  // Part 3: Reasoning / "thinking" block UX
  // ---------------------------------------------------------------------------
  const LIVE_RE = /Thinking|Analyzing|Exploring/i;
  const DONE_RE = /Thought for|Thought\b|Analyzed|Explored/i;

  const state = new WeakMap();
  let selfClicking = false;

  const reasoningHeader = (root) => {
    const first = root.firstElementChild;
    if (!first) return null;
    const cls = (first.className || "").toString();
    if (!cls.includes("cursor-pointer")) return null;
    const text = (first.textContent || "").trim();
    if (!LIVE_RE.test(text) && !DONE_RE.test(text)) return null;
    return first;
  };

  const isExpanded = (root) => root.children.length > 1;

  const clickHeader = (header) => {
    selfClicking = true;
    try {
      const opts = { bubbles: true, cancelable: true, composed: true };
      try {
        header.dispatchEvent(new PointerEvent("pointerdown", opts));
      } catch (_e) {}
      header.dispatchEvent(new PointerEvent("pointerup", opts));
    } finally {
      setTimeout(() => {
        selfClicking = false;
      }, 0);
    }
  };

  const tagBody = (root) => {
    for (let i = 1; i < root.children.length; i++) {
      const body = root.children[i];
      if (body && !body.hasAttribute("data-hushh-thinking-body")) {
        body.setAttribute("data-hushh-thinking-body", "");
      }
    }
  };

  const processReasoning = () => {
    const roots = document.querySelectorAll("div.w-full.space-y-1");
    for (const root of roots) {
      const header = reasoningHeader(root);
      if (!header) continue;

      let st = state.get(root);
      if (!st) {
        st = { autoExpanded: false, settled: false, userToggled: false };
        state.set(root, st);
      }

      const label = (header.textContent || "").trim();
      const live = LIVE_RE.test(label) && !DONE_RE.test(label);
      const expanded = isExpanded(root);

      if (expanded) tagBody(root);

      if (st.userToggled) continue;

      if (live) {
        if (!expanded) clickHeader(header);
        for (let i = 1; i < root.children.length; i++) {
          const body = root.children[i];
          if (body) {
            body.setAttribute("data-hushh-thinking-live", "true");
            body.scrollTop = body.scrollHeight;
          }
        }
      } else {
        for (let i = 1; i < root.children.length; i++) {
          root.children[i].removeAttribute("data-hushh-thinking-live");
        }
        if (st.autoExpanded && !st.settled && expanded) {
          clickHeader(header);
          st.settled = true;
        }
      }

      if (live && expanded) st.autoExpanded = true;
    }
  };

  document.addEventListener(
    "pointerup",
    (event) => {
      if (selfClicking) return;
      const header = event.target.closest && event.target.closest(".cursor-pointer");
      if (!header) return;
      const root = header.parentElement;
      if (!root || !root.classList || !root.classList.contains("space-y-1")) return;
      if (!reasoningHeader(root)) return;
      const st = state.get(root) || { autoExpanded: false, settled: false };
      st.userToggled = true;
      state.set(root, st);
    },
    true,
  );

  // ---------------------------------------------------------------------------
  // Part 4: Sidebar Changelog view (static info instead of chat component)
  // ---------------------------------------------------------------------------
  let isChangelogActive = false;
  let lastPathname = window.location.pathname;

  const CHANGELOG_HTML = `
<div class="space-y-8">
  <div class="flex items-center gap-4 border-b border-gray-100 dark:border-gray-800 pb-6">
    <div class="p-3 bg-blue-50 dark:bg-blue-950/30 rounded-2xl text-blue-600 dark:text-blue-400">
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="size-8">
        <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
      </svg>
    </div>
    <div>
      <h1 class="text-3xl font-extrabold tracking-tight text-gray-900 dark:text-gray-100">🤫 Hussh One — Changelog &amp; Features</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">What changed, what is available, and the contracts that keep it reliable.</p>
    </div>
  </div>

  <div class="bg-blue-50/50 dark:bg-blue-950/20 border-l-4 border-blue-500 p-5 rounded-r-2xl">
    <p class="text-sm text-gray-700 dark:text-gray-300 italic leading-relaxed">
      <strong>hussh</strong> = <strong>Hu</strong>man <strong>S</strong>ecure <strong>S</strong>ocket <strong>H</strong>ost. Overlay on Hermes Agent — a single, secure personal agent present across every surface. Every feature has a module, a config knob, a test, and a doc page.
    </p>
  </div>

  <section class="space-y-4">
    <div class="flex items-end justify-between gap-4">
      <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight">Latest improvements</h2>
      <span class="text-xs text-gray-400 dark:text-gray-500">July 2026</span>
    </div>
    <div class="grid gap-3">
      <article class="rounded-2xl border border-gray-100 dark:border-gray-800 p-4">
        <div class="flex items-center justify-between gap-3">
          <h3 class="m-0 text-sm font-semibold text-gray-900 dark:text-gray-100">Agent and model identity separated</h3>
          <span class="rounded-full bg-green-50 dark:bg-green-950/40 px-2 py-1 text-[0.65rem] font-semibold text-green-700 dark:text-green-300">NEW</span>
        </div>
        <p class="mb-0 mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">Hussh One remains the private agent. The composer now lists only real models from Hermes' shared provider registry, with the configured default first.</p>
      </article>
      <article class="rounded-2xl border border-gray-100 dark:border-gray-800 p-4">
        <h3 class="m-0 text-sm font-semibold text-gray-900 dark:text-gray-100">Native streaming and thinking controls</h3>
        <p class="mb-0 mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">Streaming answers, bounded thinking panels, model choice, and reasoning effort travel through the same OpenAI-compatible Hermes route.</p>
      </article>
      <article class="rounded-2xl border border-gray-100 dark:border-gray-800 p-4">
        <h3 class="m-0 text-sm font-semibold text-gray-900 dark:text-gray-100">Passwordless personal browser access</h3>
        <p class="mb-0 mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">Open WebUI 0.10.2 remains passwordless on loopback, supervised, health-checked, and reconciled during Hussh One onboarding.</p>
      </article>
    </div>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight">Three first-class surfaces</h2>
    <div class="overflow-hidden border border-gray-100 dark:border-gray-800/80 rounded-2xl">
      <table class="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
        <thead class="bg-gray-50/70 dark:bg-gray-950/30">
          <tr>
            <th class="px-5 py-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider w-1/3">Surface</th>
            <th class="px-5 py-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">What it is</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100 dark:divide-gray-800 bg-white dark:bg-transparent">
          <tr>
            <td class="px-5 py-4 text-sm font-semibold text-gray-900 dark:text-gray-100">TUI / Dashboard</td>
            <td class="px-5 py-4 text-sm text-gray-600 dark:text-gray-300"><code>hermes --tui</code> + the embedded real TUI in the web dashboard</td>
          </tr>
          <tr>
            <td class="px-5 py-4 text-sm font-semibold text-gray-900 dark:text-gray-100">WhatsApp</td>
            <td class="px-5 py-4 text-sm text-gray-600 dark:text-gray-300">Branded, owner-gated personal agent with capsules</td>
          </tr>
          <tr>
            <td class="px-5 py-4 text-sm font-semibold text-gray-900 dark:text-gray-100">Open WebUI</td>
            <td class="px-5 py-4 text-sm text-gray-600 dark:text-gray-300">This browser chat, over the OpenAI-compatible API server</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="text-xs text-gray-400 dark:text-gray-500 italic px-1">*All three run the same agent, router, and models.*</p>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2">
      <span>📱</span> WhatsApp Layer
    </h2>
    <ul class="list-disc pl-6 space-y-2.5 text-sm text-gray-600 dark:text-gray-300">
      <li><strong>Stacked brand header</strong> — 3-line header (brand · model [A/S] · divider) on every send</li>
      <li><strong>Owner-only triggering</strong> — injection-proof gating; strict <code>@One</code> tagging in groups and DMs</li>
      <li><strong>Multi-device (LID) auth</strong> — authorizes your linked devices via JID/LID</li>
      <li><strong>Social-group capsules</strong> — sandboxed: isolated memory, read-only toolset, no lateral sends</li>
      <li><strong>Anti-DOS rate limit</strong> — non-owner capsule triggering with configurable rate caps</li>
      <li><strong>Clean output</strong> — no reasoning/logs/jargon; bold-only; autopilot approvals</li>
      <li><strong>Local data &amp; recovery</strong> — WhatsApp history/media retrieval, message edit/recovery</li>
    </ul>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2">
      <span>🖥️</span> CLI / Web / API
    </h2>
    <ul class="list-disc pl-6 space-y-2.5 text-sm text-gray-600 dark:text-gray-300">
      <li><strong>CLI/TUI + Dashboard theming</strong> — the <code>hussh-one</code> skin across terminal and web</li>
      <li><strong>Natural-language model switching</strong> — "switch to opus 4.8" (deterministic, injection-safe)</li>
      <li><strong>Open WebUI browser chat variant</strong> — full web chat over the OpenAI-compatible API server; 1 agent call per message</li>
      <li><strong>TUI model popover sync</strong> — picker opens reflecting the live session model + active provider/model</li>
    </ul>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2">
      <span>🔒</span> Reliability
    </h2>
    <ul class="list-disc pl-6 space-y-2.5 text-sm text-gray-600 dark:text-gray-300">
      <li><strong>Session-model persistence &amp; resume</strong> — sessions keep their model across refresh / <code>--resume</code> / cold restart</li>
      <li><strong>Vertex-Claude pinning</strong> — Claude always routes through GCP Vertex (ADC), never Anthropic-direct</li>
      <li><strong>Dashboard crash resilience (OOM-safe)</strong> — compaction tuning + supervisor RSS soft-cap → clean restart, never SIGKILL</li>
      <li><strong>Open WebUI optimization</strong> — title/tag generation off by default → 1 agent call per message</li>
    </ul>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight">🚦 Deterministic contracts (A–K)</h2>
    <div class="overflow-hidden border border-gray-100 dark:border-gray-800/80 rounded-2xl">
      <table class="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
        <thead class="bg-gray-50/70 dark:bg-gray-950/30">
          <tr>
            <th class="px-5 py-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider w-16">Item</th>
            <th class="px-5 py-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Invariant Contract</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100 dark:divide-gray-800 bg-white dark:bg-transparent">
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">A</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Group routing safeguard</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">B</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Zero-width unicode leakage</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">C</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Upstream update guard</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">D</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Dashboard real-TUI (not forked chat)</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">E</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">NL model switching (deterministic, injection-safe)</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">F</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Capsule sandbox</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">G</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Branding &amp; header</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">H</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Session-model resume</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">I</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Dashboard crash resilience</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">J</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">TUI model popover sync</td></tr>
          <tr><td class="px-5 py-3 text-sm font-semibold text-gray-900 dark:text-gray-100">K</td><td class="px-5 py-3 text-sm text-gray-600 dark:text-gray-300">Open WebUI surface</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="space-y-4">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100 tracking-tight flex items-center gap-2">
      <span>🟣</span> Built on Hermes Agent
    </h2>
    <ul class="list-disc pl-6 space-y-2.5 text-sm text-gray-600 dark:text-gray-300">
      <li><strong>Closed learning loop</strong> — curated memory, autonomous skills, FTS5 cross-session recall</li>
      <li><strong>60+ tools</strong> — file, terminal (6 backends), web/browser, media gen, orchestration</li>
      <li><strong>20+ platforms</strong> — one gateway: CLI, Telegram, Discord, Slack, WhatsApp, and more</li>
      <li><strong>Multi-provider</strong> — Nous Portal, OpenRouter, Vertex, Anthropic, Gemini, local + plugins</li>
    </ul>
  </section>

  <div class="border-t border-gray-100 dark:border-gray-800 pt-6">
    <p class="text-xs text-gray-400 dark:text-gray-500 italic">
      *Source of truth: <code>docs/hussh-one/features</code> in the hussh-one-hermes repo.*
    </p>
  </div>
</div>
  `;

  const injectChangelogButton = () => {
    const list = document.getElementById('pinned-menu-items-list') || document.querySelector('.pb-1\\.5');
    if (list && !document.getElementById('hushh-changelog-menu-item')) {
      const item = document.createElement('div');
      item.id = 'hushh-changelog-menu-item';
      item.className = 'px-[0.4375rem] flex justify-center text-gray-800 dark:text-gray-200';
      item.innerHTML = `
        <button class="group grow flex items-center space-x-3 rounded-2xl px-2.5 py-2 hover:bg-gray-100 dark:hover:bg-gray-900 transition outline-none" id="sidebar-changelog-button" title="Changelog" aria-label="Open Hussh One changelog">
          <div class="flex self-center translate-y-[0.5px] text-gray-500 dark:text-gray-400 group-hover:text-gray-800 dark:group-hover:text-gray-100 transition">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="size-5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
            </svg>
          </div>
          <div class="flex flex-1 self-center translate-y-[0.5px]">
            <div class="hushh-changelog-label self-center text-sm font-primary font-medium text-gray-600 dark:text-gray-300 group-hover:text-gray-800 dark:group-hover:text-gray-100 transition">Changelog</div>
          </div>
        </button>
      `;
      list.appendChild(item);
      return;
    }

    // Open WebUI replaces the sidebar with a compact top bar on narrow
    // screens. Keep Changelog reachable there as an icon-only peer rather
    // than forcing a hidden desktop drawer.
    if (!list && !document.getElementById('sidebar-changelog-button')) {
      const anchor = document.querySelector("button[aria-label='Controls']");
      if (!anchor || !anchor.parentElement) return;
      const button = document.createElement('button');
      button.id = 'sidebar-changelog-button';
      button.className = 'hushh-mobile-changelog rounded-full p-2 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-900';
      button.title = 'Changelog';
      button.setAttribute('aria-label', 'Open Hussh One changelog');
      button.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="size-5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
        </svg>
      `;
      anchor.insertAdjacentElement('afterend', button);
    }
  };

  const updateChangelogView = () => {
    const container = document.getElementById('chat-container');
    if (!container) return;

    let view = document.getElementById('hushh-changelog-view');

    if (isChangelogActive) {
      // Hide normal children of #chat-container
      Array.from(container.children).forEach(child => {
        if (child.id !== 'hushh-changelog-view') {
          child.style.setProperty('display', 'none', 'important');
        }
      });

      // Create or show our changelog view
      if (!view) {
        view = document.createElement('div');
        view.id = 'hushh-changelog-view';
        view.className = 'h-full w-full overflow-y-auto bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 flex flex-col';
        view.innerHTML = `
          <div class="hushh-changelog-topbar border-b border-gray-100 dark:border-gray-800/50 flex justify-between items-center bg-gray-50/50 dark:bg-gray-950/20 backdrop-blur shrink-0">
            <div class="flex items-center gap-3">
              <span class="text-xl">🤫</span>
              <h1 class="text-lg font-semibold font-primary">Hussh One — Changelog</h1>
            </div>
            <div class="text-xs text-gray-500 dark:text-gray-400 font-mono">
              ACTIVE
            </div>
          </div>
          <div class="hushh-changelog-content flex-1">
            <div class="prose prose-gray dark:prose-invert max-w-none font-primary">
              ${CHANGELOG_HTML}
            </div>
          </div>
        `;
        container.appendChild(view);
      } else {
        view.style.display = 'flex';
      }

      // Make the button look active
      const btn = document.getElementById('sidebar-changelog-button');
      if (btn) {
        btn.classList.add('bg-gray-100', 'dark:bg-gray-900');
        const text = btn.querySelector('.font-primary');
        if (text) text.classList.add('text-gray-900', 'dark:text-gray-100');
      }
    } else {
      // Hide our view
      if (view) {
        view.style.display = 'none';
      }

      // Show other children
      Array.from(container.children).forEach(child => {
        if (child.id !== 'hushh-changelog-view') {
          child.style.removeProperty('display');
        }
      });

      // Make the button look inactive
      const btn = document.getElementById('sidebar-changelog-button');
      if (btn) {
        btn.classList.remove('bg-gray-100', 'dark:bg-gray-900');
        const text = btn.querySelector('.font-primary');
        if (text) text.classList.remove('text-gray-900', 'dark:text-gray-100');
      }
    }
  };

  // Listen to clicks for the sidebar button
  document.addEventListener('click', (event) => {
    const changelogBtn = event.target.closest('#sidebar-changelog-button');
    if (changelogBtn) {
      event.preventDefault();
      event.stopPropagation();
      isChangelogActive = !isChangelogActive;
      updateChangelogView();
      return;
    }

    const sidebar = event.target.closest('#sidebar');
    if (sidebar) {
      const linkOrBtn = event.target.closest('a, button');
      if (linkOrBtn && linkOrBtn.id !== 'sidebar-changelog-button') {
        isChangelogActive = false;
        updateChangelogView();
      }
    }
  }, true);

  // Sync state if pathname changes
  const checkRouteChanged = () => {
    if (window.location.pathname !== lastPathname) {
      lastPathname = window.location.pathname;
      isChangelogActive = false;
      updateChangelogView();
    }
  };

  // ---------------------------------------------------------------------------
  // Shared scheduler + observer
  // ---------------------------------------------------------------------------
  const run = () => {
    suppressDisabledAuthChrome();
    sanitizeVisibleLeakyTitles();
    syncDocumentTitle();
    processReasoning();
    void installComposerControls();
    injectChangelogButton();
    checkRouteChanged();
    if (isChangelogActive) {
      updateChangelogView();
    }
  };
  const schedule = () => window.requestAnimationFrame(run);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", schedule, { once: true });
  } else {
    schedule();
  }

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  window.addEventListener("focus", schedule);
  window.addEventListener("pageshow", schedule);
})();
JS
}



write_launcher() {
  mkdir -p "$(dirname "$LAUNCHER_PATH")" "$OPEN_WEBUI_DATA_DIR" "$LOG_DIR"

  local quoted_data_dir quoted_name quoted_base_url quoted_host quoted_port quoted_venv quoted_home quoted_env
  local quoted_repo_root quoted_setup_script quoted_setup_revision quoted_cors_origin quoted_hermes_bin
  quoted_data_dir="$(shell_quote "$OPEN_WEBUI_DATA_DIR")"
  quoted_name="$(shell_quote "$OPEN_WEBUI_NAME")"
  quoted_base_url="$(shell_quote "$HERMES_API_BASE_URL")"
  quoted_host="$(shell_quote "$OPEN_WEBUI_HOST")"
  quoted_port="$(shell_quote "$OPEN_WEBUI_PORT")"
  quoted_venv="$(shell_quote "$OPEN_WEBUI_VENV")"
  quoted_home="$(shell_quote "$HERMES_HOME")"
  quoted_env="$(shell_quote "$HERMES_ENV_FILE")"
  quoted_repo_root="$(shell_quote "$REPO_ROOT")"
  quoted_setup_script="$(shell_quote "$SOURCE_SETUP_SCRIPT")"
  quoted_setup_revision="$(shell_quote "$SETUP_REVISION_PATH")"
  quoted_cors_origin="$(shell_quote "$OPEN_WEBUI_CORS_ALLOW_ORIGIN")"
  quoted_hermes_bin="$(shell_quote "$HERMES_BIN")"
  local quoted_managed_setup
  quoted_managed_setup="$(shell_quote "$MANAGED_SETUP_SCRIPT")"

  cat > "$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HERMES_HOME=${quoted_home}
export HERMES_ENV_FILE=${quoted_env}
HUSSH_ONE_REPO_ROOT=${quoted_repo_root}
HUSSH_ONE_OPEN_WEBUI_SETUP=${quoted_setup_script}
HUSSH_ONE_OPEN_WEBUI_SETUP_REVISION=${quoted_setup_revision}
HUSSH_ONE_OPEN_WEBUI_MANAGED_SETUP=${quoted_managed_setup}

# Reconcile only when the versioned Hussh companion contract changes. A failed
# reconciliation falls through to the last known-good installed runtime.
if [[ "\${HUSSH_ONE_OPEN_WEBUI_RECONCILE:-1}" == "1" && -f "\$HUSSH_ONE_OPEN_WEBUI_SETUP" ]]; then
  current_revision=\$(python3 - "\$HUSSH_ONE_OPEN_WEBUI_SETUP" <<'PY'
import hashlib
from pathlib import Path
import sys
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
  installed_revision=\$(cat "\$HUSSH_ONE_OPEN_WEBUI_SETUP_REVISION" 2>/dev/null || true)
  if [[ "\$current_revision" != "\$installed_revision" ]]; then
    # Background launchd jobs cannot execute scripts directly from macOS
    # protected Documents folders. Copy the reviewed source atomically into
    # HERMES_HOME and execute that managed copy instead.
    if ! python3 - "\$HUSSH_ONE_OPEN_WEBUI_SETUP" "\$HUSSH_ONE_OPEN_WEBUI_MANAGED_SETUP" <<'PY'
from pathlib import Path
import os
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
temporary.write_bytes(source.read_bytes())
temporary.chmod(0o700)
temporary.replace(destination)
PY
    then
      printf '%s\n' "warning: could not stage the managed Open WebUI reconciler" >&2
    elif env \
      HUSSH_ONE_OPEN_WEBUI_RECONCILE=0 \
      HUSSH_ONE_SOURCE_SETUP="\$HUSSH_ONE_OPEN_WEBUI_SETUP" \
      HUSSH_ONE_REPO_ROOT="\$HUSSH_ONE_REPO_ROOT" \
      HERMES_HOME=${quoted_home} \
      HERMES_BIN=${quoted_hermes_bin} \
      OPEN_WEBUI_HOST=${quoted_host} \
      OPEN_WEBUI_PORT=${quoted_port} \
      OPEN_WEBUI_NAME=${quoted_name} \
      OPEN_WEBUI_AUTH=${OPEN_WEBUI_AUTH} \
      OPEN_WEBUI_ENABLE_SIGNUP=${OPEN_WEBUI_ENABLE_SIGNUP} \
      OPEN_WEBUI_DATA_DIR=${quoted_data_dir} \
      OPEN_WEBUI_VENV=${quoted_venv} \
      OPEN_WEBUI_ENABLE_SERVICE=false \
      bash "\$HUSSH_ONE_OPEN_WEBUI_MANAGED_SETUP"; then
      exec env HUSSH_ONE_OPEN_WEBUI_RECONCILE=0 "\$0"
    fi
    printf '%s\n' "warning: Open WebUI companion reconciliation failed; continuing with the last known-good Open WebUI runtime" >&2
  fi
fi

API_KEY=\$(python3 - <<'PY'
import os
from pathlib import Path
p = Path(os.environ["HERMES_ENV_FILE"])
for raw in p.read_text().splitlines():
    line = raw.strip()
    if line.startswith('API_SERVER_KEY='):
        print(line.split('=', 1)[1])
        break
PY
)
export DATA_DIR=${quoted_data_dir}
export WEBUI_NAME=${quoted_name}
export ENABLE_SIGNUP=${OPEN_WEBUI_ENABLE_SIGNUP}
export ENABLE_LOGIN_FORM=False
export ENABLE_PUBLIC_ACTIVE_USERS_COUNT=False
export ENABLE_VERSION_UPDATE_CHECK=False
export OPENAI_API_BASE_URL=${quoted_base_url}
export OPENAI_API_KEY="\$API_KEY"
export ENABLE_OPENAI_API=True
export ENABLE_OLLAMA_API=False
export ENABLE_BASE_MODELS_CACHE=True
export MODELS_CACHE_TTL=${OPEN_WEBUI_MODELS_CACHE_TTL}
export AIOHTTP_CLIENT_TIMEOUT=${OPEN_WEBUI_API_TIMEOUT}
export AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST=${OPEN_WEBUI_MODEL_LIST_TIMEOUT}
export AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST=${OPEN_WEBUI_MODEL_LIST_TIMEOUT}
export CORS_ALLOW_ORIGIN=${quoted_cors_origin}
export OFFLINE_MODE=True
export HF_HUB_OFFLINE=1
export BYPASS_EMBEDDING_AND_RETRIEVAL=True
# Open WebUI still validates an embedding backend during startup even when
# retrieval is bypassed. Keep this local OpenAI-compatible fallback so a fresh
# Hussh One install can boot without downloading a sentence-transformer model.
export RAG_EMBEDDING_ENGINE="openai"
export RAG_EMBEDDING_MODEL="text-embedding-3-small"
export RAG_EMBEDDING_MODEL_AUTO_UPDATE=False
export RAG_RERANKING_MODEL_AUTO_UPDATE=False
export SCARF_NO_ANALYTICS=true
export DO_NOT_TRACK=true
export ANONYMIZED_TELEMETRY=false
# --- Hussh One performance: keep Open WebUI at 1 Hermes agent call per message ---
# Each background auto-task otherwise spins up a FULL server-side Hermes AIAgent
# (same heavy context: system prompt + memory + MCP tool schemas) just to name a
# chat or suggest follow-ups. Disabling them gives genuine TUI-parity efficiency.
export ENABLE_TITLE_GENERATION=${OPEN_WEBUI_ENABLE_TITLE_GENERATION}
export ENABLE_TAGS_GENERATION=${OPEN_WEBUI_ENABLE_TAGS_GENERATION}
export ENABLE_AUTOCOMPLETE_GENERATION=False
export ENABLE_FOLLOW_UP_GENERATION=False
export ENABLE_RETRIEVAL_QUERY_GENERATION=False
export ENABLE_SEARCH_QUERY_GENERATION=False
# --- Hussh One: silence single-user noise (no arena/eval/community calls) ---
export ENABLE_EVALUATION_ARENA_MODELS=False
export ENABLE_MESSAGE_RATING=False
export ENABLE_COMMUNITY_SHARING=False
export ENABLE_REALTIME_CHAT_SAVE=False
# Open WebUI's default SQLite/Chroma persistence is single-worker only.
export UVICORN_WORKERS=1
# Hussh One defaults to passwordless access and enforces loopback binding during
# setup. Existing single-user data is migrated before this launcher starts.
export WEBUI_AUTH=${OPEN_WEBUI_AUTH}
export HOST=${quoted_host}
export PORT=${quoted_port}
source ${quoted_venv}/bin/activate
for _attempt in \$(seq 1 60); do
  if curl -fsS --max-time 2 ${quoted_base_url}/models \
      -H "Authorization: Bearer \$API_KEY" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -fsS --max-time 2 ${quoted_base_url}/models \
    -H "Authorization: Bearer \$API_KEY" >/dev/null 2>&1; then
  printf '%s\n' "Hermes API is unavailable; Open WebUI will retry through its service supervisor." >&2
  exit 1
fi
exec open-webui serve --host "\$HOST" --port "\$PORT"
EOF

  chmod +x "$LAUNCHER_PATH"
}

write_runtime_config() {
  local parent temp
  parent="$(dirname "$RUNTIME_CONFIG_PATH")"
  mkdir -p "$parent"
  temp="${RUNTIME_CONFIG_PATH}.tmp.$$"
  {
    printf 'OPEN_WEBUI_HOST=%q\n' "$OPEN_WEBUI_HOST"
    printf 'OPEN_WEBUI_PORT=%q\n' "$OPEN_WEBUI_PORT"
    printf 'OPEN_WEBUI_LAUNCHER=%q\n' "$LAUNCHER_PATH"
    printf 'OPEN_WEBUI_VERSION=%q\n' "$OPEN_WEBUI_VERSION"
  } > "$temp"
  mv "$temp" "$RUNTIME_CONFIG_PATH"
  chmod 600 "$RUNTIME_CONFIG_PATH"
}

write_setup_revision() {
  local temp
  mkdir -p "$(dirname "$SETUP_REVISION_PATH")"
  temp="${SETUP_REVISION_PATH}.tmp.$$"
  file_sha256 "$SOURCE_SETUP_SCRIPT" > "$temp"
  mv "$temp" "$SETUP_REVISION_PATH"
  chmod 600 "$SETUP_REVISION_PATH"
}

verify_hermes_models() {
  local api_key="$1"
  curl -fsS \
    -H "Authorization: Bearer ${api_key}" \
    "${HERMES_API_BASE_URL}/models" |
    python3 -c 'import json,sys; payload=json.load(sys.stdin); assert isinstance(payload.get("data"), list) and payload["data"], "Hermes advertised no models"'
}

wait_for_open_webui() {
  local attempt
  for attempt in $(seq 1 90); do
    if curl -fsS "http://${OPEN_WEBUI_HOST}:${OPEN_WEBUI_PORT}/health" >/dev/null 2>&1 \
      && curl -fsS "http://${OPEN_WEBUI_HOST}:${OPEN_WEBUI_PORT}/" 2>/dev/null \
        | grep -q '/static/loader.js'; then
      return 0
    fi
    sleep 1
  done
  echo "Open WebUI did not become healthy at http://${OPEN_WEBUI_HOST}:${OPEN_WEBUI_PORT}; another process may own the port" >&2
  return 1
}

wait_for_hermes_api() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "http://${HERMES_API_CONNECT_HOST}:${HERMES_API_PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

ensure_env_permissions() {
  chmod 600 "$HERMES_ENV_FILE" 2>/dev/null || true
}

install_launchd_service() {
  local plist="$HOME/Library/LaunchAgents/ai.openwebui.hermes.plist"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.openwebui.hermes</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${LAUNCHER_PATH}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>${HOME}</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/openwebui.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/openwebui.error.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  launchctl enable "gui/$(id -u)/ai.openwebui.hermes"
  launchctl kickstart -k "gui/$(id -u)/ai.openwebui.hermes"
}

install_systemd_user_service() {
  require_cmd systemctl
  local unit_dir="$HOME/.config/systemd/user"
  local unit="$unit_dir/openwebui-hermes.service"
  mkdir -p "$unit_dir"
  cat > "$unit" <<EOF
[Unit]
Description=Open WebUI connected to Hussh One
After=default.target

[Service]
Type=simple
ExecStart=/bin/bash %h/.local/bin/start-open-webui-hermes.sh
Restart=always
RestartSec=3
WorkingDirectory=%h
Environment=HERMES_HOME=$HERMES_HOME
Environment=HERMES_ENV_FILE=$HERMES_ENV_FILE
StandardOutput=append:$LOG_DIR/openwebui.log
StandardError=append:$LOG_DIR/openwebui.error.log

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now openwebui-hermes.service
}

start_foreground_hint() {
  log "Launcher created at: ${LAUNCHER_PATH}"
  log "Start Open WebUI manually with: ${LAUNCHER_PATH}"
}

main() {
  require_hermes_bin
  require_cmd curl
  require_cmd python3
  export HERMES_HOME

  install_macos_dependencies

  local api_key gateway_restart_required=0 env_before env_after registry_models
  api_key="$(get_env_value API_SERVER_KEY "$HERMES_ENV_FILE")"
  if [[ -z "$api_key" ]]; then
    api_key="$(generate_secret)"
  fi

  log 'Ensuring Hermes API server is configured...'
  env_before="$(file_sha256 "$HERMES_ENV_FILE" 2>/dev/null || true)"
  upsert_env API_SERVER_ENABLED true "$HERMES_ENV_FILE"
  upsert_env API_SERVER_HOST "$HERMES_API_HOST" "$HERMES_ENV_FILE"
  upsert_env API_SERVER_PORT "$HERMES_API_PORT" "$HERMES_ENV_FILE"
  upsert_env API_SERVER_MODEL_NAME "$HERMES_API_MODEL_NAME" "$HERMES_ENV_FILE"
  upsert_env API_SERVER_KEY "$api_key" "$HERMES_ENV_FILE"
  ensure_env_permissions
  env_after="$(file_sha256 "$HERMES_ENV_FILE")"
  [[ "$env_before" == "$env_after" ]] || gateway_restart_required=1

  registry_models="$("$HERMES_BIN" config get gateway.api_server.expose_provider_models 2>/dev/null || true)"
  if [[ "$registry_models" != "true" ]]; then
    "$HERMES_BIN" config set --force gateway.api_server.expose_provider_models true >/dev/null
    gateway_restart_required=1
  fi

  if [[ "$gateway_restart_required" == "1" ]]; then
    log 'Restarting Hermes gateway so changed API settings take effect...'
    "$HERMES_BIN" gateway restart >/dev/null 2>&1 || true
  else
    log 'Hermes API settings are unchanged; preserving the healthy gateway process.'
  fi
  if ! wait_for_hermes_api; then
    log 'Hermes API server did not answer on the first check. Trying to start gateway in the background...'
    HERMES_HOME="$HERMES_HOME" nohup "$HERMES_BIN" gateway run >/dev/null 2>&1 &
    wait_for_hermes_api
  fi
  verify_hermes_models "$api_key"

  log 'Installing Open WebUI into a dedicated virtualenv...'
  install_open_webui
  prepare_passwordless_database
  install_static_assets
  write_launcher
  write_runtime_config
  write_setup_revision

  local service_started=0
  case "$OPEN_WEBUI_ENABLE_SERVICE" in
    true|auto)
      if [[ "$(uname -s)" == "Darwin" ]]; then
        install_launchd_service
        service_started=1
      elif can_use_systemd_user; then
        install_systemd_user_service
        service_started=1
      else
        log 'No usable user service manager detected; falling back to the launcher script.'
        start_foreground_hint
      fi
      ;;
    false)
      start_foreground_hint
      ;;
    *)
      echo "OPEN_WEBUI_ENABLE_SERVICE must be one of: auto, true, false" >&2
      exit 1
      ;;
  esac
  if [[ "$service_started" == "1" ]]; then
    wait_for_open_webui
    if enforce_passwordless_ui_config; then
      log 'Disabling persisted login-form chrome for passwordless Hussh One...'
      restart_open_webui_service
      wait_for_open_webui
    fi
  fi

  log "Done. Open WebUI should be available at: http://${OPEN_WEBUI_HOST}:${OPEN_WEBUI_PORT}"
  log "Hermes API endpoint: ${HERMES_API_BASE_URL}"
  log 'Important: Open WebUI persists connection settings after first launch. If you later save a wrong API key in the Admin UI, update/delete that connection there or reset its database.'
}

main "$@"
