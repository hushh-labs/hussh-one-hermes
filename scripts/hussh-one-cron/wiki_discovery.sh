#!/bin/bash
# Pre-run discovery for the Wiki Maintenance job. Its stdout is injected into
# the job prompt as context, so the numbers the report must carry come from
# here, not from the model's memory of what it might have run.
REPO=/Users/kushaltrivedi/Documents/GitHub/hushh-research
HW=/Users/kushaltrivedi/.hermes/skills/note-taking/hushh-wiki-mcp/scripts/hw.py
cd "$REPO" 2>/dev/null || { echo "DISCOVERY: repository unavailable at $REPO"; exit 0; }
count=$(git log --since="36 hours ago" --oneline 2>/dev/null | wc -l | tr -d ' ')
echo "DISCOVERY: commits_36h=$count"
echo "<repository-log-36h>"
git log --since="36 hours ago" --format='%h %ad %s' --date=short 2>/dev/null | head -40
echo "</repository-log-36h>"
echo "<diff-stat-last-5>"
git diff --stat HEAD~5..HEAD 2>/dev/null | tail -25
echo "</diff-stat-last-5>"
pages=$(python3 "$HW" wiki_list '{}' 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    total=d.get('total') if isinstance(d,dict) else None
    items=(d.get('entries') or d.get('pages') or d.get('items') or []) if isinstance(d,dict) else d
    print(total if isinstance(total,int) else len(items))
except Exception:
    print('unknown')")
echo "DISCOVERY: wiki_pages=${pages:-unknown}"
