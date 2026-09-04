#!/usr/bin/env bash
# Cron wrapper: invoke the version-controlled hushh-research tmp/ janitor.
# Real logic lives in the repo so it stays reviewed/versioned; this wrapper
# only exists because cron scripts must reside under ~/.hermes/scripts/.
# Stays SILENT (no stdout) on success so the no_agent cron sends nothing;
# only emits output on failure so a broken janitor surfaces an alert.
set -euo pipefail
JANITOR="/Users/kushaltrivedi/Documents/GitHub/hushh-research/scripts/maintenance/clean_tmp.sh"
if [ ! -x "$JANITOR" ]; then
  echo "tmp janitor missing or not executable: $JANITOR" >&2
  exit 1
fi
# Run quietly; capture summary but do not print on success (watchdog pattern).
out="$("$JANITOR" 2>&1)" || { echo "tmp janitor FAILED:"; echo "$out"; exit 1; }
# Success: silent. (Uncomment next line if you want a daily confirmation line.)
# echo "$out"
exit 0
