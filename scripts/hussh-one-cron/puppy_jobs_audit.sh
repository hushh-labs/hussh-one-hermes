#!/bin/bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Daily contract audit of the on-device cron jobs (deterministic half of
# `hermes puppy jobs`). Runs after the last daily job. Prints a short
# WhatsApp-sized note ONLY when a run failed its contract; silence means every
# job delivered within its own contract. The blinded judge half is a separate
# session's work: the queue it needs is written to $OUT/run every day.
set -u
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO=/Users/kushaltrivedi/Documents/GitHub/hussh-one-hermes-agent
DAY=$(date +%Y-%m-%d)
OUT="$HERMES_HOME/puppy-jobs/$DAY"
SECRETS="$HERMES_HOME/puppy-jobs/secrets/$DAY"
mkdir -p "$OUT" "$SECRETS"
cd "$REPO" || exit 1
# 6h at 07:30 covers the whole 03:10 to 07:00 daily window and nothing older.
table=$("$REPO/.venv/bin/python" -m hermes_cli.main puppy jobs collect --since 6h \
  --out "$OUT" --seal "$SECRETS/seal.json" --identity "$SECRETS/identity.json" 2>/dev/null)
printf '%s\n' "$table" > "$OUT/collect.txt"
# A recovery in the same window should clear an earlier failure. Summarise the
# latest execution for each job from the machine-readable result instead of
# notifying on every historical row in the window.
if [ -f "$OUT/runs.json" ]; then
summary=$(python3 - "$OUT/runs.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
ok = 0
bad = []
for name, entries in (data.get("per_job") or {}).items():
    if not entries:
        continue
    latest = max(entries, key=lambda row: row.get("claimed_at", ""))
    if latest.get("contract_failures") or latest.get("indeterminate"):
        if latest.get("indeterminate"):
            reason = "the latest run was not verified; review the saved evidence."
        else:
            reason = "the latest report missed its presentation contract."
        bad.append((name, reason))
    else:
        ok += 1
print(ok)
print(len(bad))
for name, reason in bad:
    print(f"{name}|{reason}")
PY
)
else
  summary="0\n0"
fi
ok=$(printf '%s\n' "$summary" | sed -n '1p')
bad=$(printf '%s\n' "$summary" | sed -n '2p')
if [ "$bad" -gt 0 ]; then
  printf '*🤫 Hussh One* · *Job Audit*\n======================================\n\n'
  printf '• %s job(s) within contract, %s outside it in the last 6h\n\n' "$ok" "$bad"
  # Keep the owner-facing report useful on a phone. Raw provider errors,
  # timestamps and local paths remain in the saved evidence only.
  printf '%s\n' "$summary" | sed -n '3,$p' | while IFS='|' read -r name message; do
    [ -n "$name" ] && printf '• %s — %s\n\n' "$name" "$message"
  done
  printf '• Detailed evidence is saved locally for review.\n'
fi
