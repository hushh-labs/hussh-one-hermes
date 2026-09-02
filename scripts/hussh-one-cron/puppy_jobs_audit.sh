#!/bin/bash
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
bad=$(printf '%s\n' "$table" | grep -c '^!! ')
ok=$(printf '%s\n' "$table" | grep -c '^OK ')
if [ "$bad" -gt 0 ]; then
  printf '*🤫 Hussh One* · *Job Audit*\n======================================\n\n'
  printf '• %s run(s) within contract, %s outside it in the last 6h\n\n' "$ok" "$bad"
  printf '%s\n' "$table" | grep '^!! ' | sed 's/^!! /• /' | cut -c1-160 | sed 's/$/\n/'
  printf '\n• Queue for the judge: %s/run\n' "$OUT"
fi
