#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Repair a rejected Hussh Consent MCP credential without exposing its value.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
RESTART=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: scripts/hussh-one-consent-repair.sh [options]

Refresh the active profile's Hussh Consent MCP developer credential only after
the hosted connector has rejected it. The replacement is read once from the
authorized UAT GCP Secret Manager entry, atomically written mode 0600, and is
never printed or placed in MCP configuration.

Options:
  --restart       Restart Hussh One after a successful repair
  --dry-run       Verify the local command path without contacting GCP
  -h, --help      Show this help
USAGE
}

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart) RESTART=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run: would refresh HUSHH_CONSENT_MCP_TOKEN from the authorized UAT Secret Manager entry"
  exit 0
fi

PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "repository .venv Python is required for consent credential repair"

# Keep the secret entirely in the Python process. In particular, never place
# it in a shell variable, command line, log, config file, or child process.
"$PYTHON" - "$ENV_FILE" <<'PY'
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

try:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
except ImportError as exc:
    raise SystemExit(f"Google authentication support is unavailable: {exc}") from exc

destination = Path(sys.argv[1])
project = (
    os.getenv("GOOGLE_CLOUD_PROJECT")
    or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    or "hushh-pda-uat"
).strip()

try:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    response = AuthorizedSession(credentials).get(
        "https://secretmanager.googleapis.com/v1/projects/"
        f"{project}/secrets/HUSHH_TECHNOLOGIES_PARTNER_MCP_TOKEN/versions/latest:access",
        timeout=20,
    )
    response.raise_for_status()
    token = base64.b64decode(response.json()["payload"]["data"]).decode().strip()
except Exception as exc:
    raise SystemExit(
        "could not retrieve the Hussh Consent MCP credential using existing GCP ADC"
    ) from exc

if not token or "\n" in token or "\r" in token:
    raise SystemExit("Secret Manager returned an invalid Hussh Consent MCP credential")

lines = destination.read_text(encoding="utf-8").splitlines() if destination.exists() else []
replacement = f"HUSHH_CONSENT_MCP_TOKEN={token}"
updated: list[str] = []
replaced = False
for line in lines:
    if line.startswith("HUSHH_CONSENT_MCP_TOKEN="):
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

log "Hussh Consent MCP credential repaired securely in the active profile."
if [[ "$RESTART" == "1" ]]; then
  "$SCRIPT_DIR/hussh-one-supervisor.sh" restart --manager "${HUSSH_ONE_SUPERVISOR:-auto}" --clean-conflicts
fi
