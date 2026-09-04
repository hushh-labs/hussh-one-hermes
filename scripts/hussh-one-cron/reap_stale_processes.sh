#!/bin/bash
# Wrapper to run the stale process reaper with the correct Hermes virtualenv python.
# --quiet = watchdog mode: stays SILENT (no stdout) when the system is clean, so the
# no_agent cron only delivers a message when it actually reaps a stale/runaway process.
/Users/kushaltrivedi/Documents/GitHub/hussh-one-hermes-agent/.venv/bin/python /Users/kushaltrivedi/.hermes/scripts/reap_stale_processes.py --quiet
