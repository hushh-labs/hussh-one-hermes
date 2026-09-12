#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Read-only, bounded source evidence for the daily model-generated digest."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ('29a6e1247b31', '0c21379ebd21', '23f783d149c7')


def _report_body(text: str, no_agent: bool) -> str:
    """Extract the delivered report from an agent or script output file."""
    _, marker, response = text.rpartition('\n## Response\n')
    if marker:
        return response.strip()
    if no_agent:
        # Script output is persisted with a scheduler preamble and no
        # ``## Response`` delimiter.  Keep only the operator-facing stdout.
        preamble = text.find('\n---\n')
        if preamble >= 0:
            return text[preamble + len('\n---\n'):].strip()
    return ''


def collect(home: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    data = json.loads((home / 'cron/jobs.json').read_text())
    jobs = data.get('jobs', []) if isinstance(data, dict) else data
    by_id = {j['id']: j for j in jobs}
    result = {'as_of': now.astimezone().strftime('%b %-d, %Y · %-I:%M %p %Z'), 'sources': []}
    path = home / 'cron/executions.db'
    con = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) if path.exists() else None
    try:
        for job_id in SOURCES:
            job = by_id.get(job_id, {})
            state = None
            if con:
                row = con.execute('SELECT status FROM executions WHERE job_id=? ORDER BY claimed_at DESC, id DESC LIMIT 1', (job_id,)).fetchone()
                state = row[0] if row else None
            last_run = job.get('last_run_at')
            try:
                run_time = datetime.fromisoformat(last_run)
                age = (now - run_time).total_seconds()
            except (TypeError, ValueError):
                age = None
            healthy = job.get('last_status') == 'ok' and state == 'completed' and age is not None and 0 <= age < 36*3600
            item = {'job_id':job_id, 'name':job.get('name', 'Missing job'), 'job_status':job.get('last_status'), 'execution_status':state, 'last_run_at':last_run, 'last_run_display':run_time.astimezone().strftime('%b %-d, %-I:%M %p %Z') if age is not None else 'Not recorded', 'healthy':healthy, 'report':None}
            if healthy:
                files = list((home / 'cron/output' / job_id).glob('*.md'))
                if files:
                    latest = max(files, key=lambda p:p.stat().st_mtime)
                    with latest.open('rb') as f:
                        f.seek(max(0,latest.stat().st_size-65536))
                        text=f.read().decode('utf-8',errors='replace')
                    try:
                        output_time = datetime.strptime(latest.stem, '%Y-%m-%d_%H-%M-%S').replace(tzinfo=run_time.tzinfo)
                        matches_run = abs((run_time-output_time).total_seconds()) <= 300
                    except ValueError:
                        matches_run = False
                    response = _report_body(text, bool(job.get('no_agent')))
                    if response and matches_run:
                        item['report'] = response[:6000]
                        item['report_truncated'] = len(response) > 6000
                if not item['report']:
                    item['healthy'] = False
                    item['evidence_gap'] = 'Completed run has no identifiable final response'
            result['sources'].append(item)
    finally:
        if con: con.close()
    result['overall_health'] = 'Healthy' if all(s['healthy'] for s in result['sources']) else 'Warning'
    return result


if __name__ == '__main__':
    home=Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes').resolve()
    print(json.dumps(collect(home),ensure_ascii=False,indent=2))
