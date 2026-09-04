#!/usr/bin/env bash
# 🤫 Hussh One — thin cron wrapper for the versioned WhatsApp session janitor.
# The real, reviewed logic lives in the repo; this only locates and runs it so
# the weekly no_agent cron has a stable path under ~/.hermes/scripts/.
# Silent on success (watchdog pattern); prints only when files were pruned or on error.
set -euo pipefail

REPO="/Users/kushaltrivedi/Documents/GitHub/hussh-one-hermes-agent"
JANITOR="$REPO/scripts/maintenance/prune_whatsapp_session.py"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

if [ ! -f "$JANITOR" ]; then
  echo "ERROR: WhatsApp session janitor not found at $JANITOR" >&2
  exit 2
fi

exec "$PY" "$JANITOR" "$@"
