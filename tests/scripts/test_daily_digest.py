"""Digest truth must come from current execution evidence, not old success text."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

spec=importlib.util.spec_from_file_location('daily_digest',Path(__file__).resolve().parents[2]/'scripts/hussh-one-cron/daily_digest.py')
D=importlib.util.module_from_spec(spec); spec.loader.exec_module(D)


def test_failed_latest_run_cannot_reuse_success_report(tmp_path):
    (tmp_path/'cron').mkdir()
    now=datetime(2026,9,11,12,tzinfo=timezone.utc)
    jobs=[{'id':i,'name':i,'last_status':'ok','last_run_at':now.isoformat()} for i in D.SOURCES]
    (tmp_path/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    con=sqlite3.connect(tmp_path/'cron/executions.db')
    con.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,claimed_at TEXT)')
    for i in D.SOURCES:
        con.execute('INSERT INTO executions VALUES(?,?,?,?)',(i,i,'completed','2026-09-11'))
        p=tmp_path/'cron/output'/i; p.mkdir(parents=True)
        (p/'2026-09-11_12-00-00.md').write_text('PRIVATE RAW PROMPT\n## Response\nVerified result')
    con.commit()
    result=D.collect(tmp_path,now)
    assert result['overall_health']=='Healthy'
    assert ' · ' in result['as_of']
    assert any(x in result['as_of'] for x in (' AM ', ' PM '))
    assert all('T12:' not in item['last_run_display'] for item in result['sources'])
    assert all(s['report']=='Verified result' for s in result['sources'])
    old = tmp_path/'cron/output'/D.SOURCES[1]/'2026-09-11_12-00-00.md'
    old.rename(old.with_name('2026-09-10_12-00-00.md'))
    assert D.collect(tmp_path,now)['sources'][1]['healthy'] is False
    con.execute('INSERT INTO executions VALUES(?,?,?,?)',('failed',D.SOURCES[0],'failed','2026-09-12'))
    con.commit(); con.close()
    result=D.collect(tmp_path,now)
    assert result['overall_health']=='Warning'
    assert result['sources'][0]['report'] is None
    assert 'PRIVATE RAW PROMPT' not in json.dumps(result)


def test_missing_ledger_and_old_job_are_not_healthy(tmp_path):
    (tmp_path/'cron').mkdir()
    (tmp_path/'cron/jobs.json').write_text(json.dumps({'jobs':[{'id':D.SOURCES[0],'last_status':'ok','last_run_at':'2025-01-01T00:00:00+00:00'}]}))
    result=D.collect(tmp_path)
    assert result['overall_health']=='Warning'
    assert not any(s['healthy'] for s in result['sources'])


def test_no_agent_report_ignores_scheduler_preamble(tmp_path):
    (tmp_path/'cron').mkdir()
    now=datetime(2026,9,11,12,tzinfo=timezone.utc)
    jobs=[{'id':i,'name':i,'last_status':'ok','last_run_at':now.isoformat(), 'no_agent': i == D.SOURCES[0]} for i in D.SOURCES]
    (tmp_path/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    con=sqlite3.connect(tmp_path/'cron/executions.db')
    con.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,claimed_at TEXT)')
    for i in D.SOURCES:
        con.execute('INSERT INTO executions VALUES(?,?,?,?)',(i,i,'completed','2026-09-11'))
        p=tmp_path/'cron/output'/i; p.mkdir(parents=True)
        body='*Script report*\n======================================\n\n• Fresh result'
        if i == D.SOURCES[0]:
            body='# Cron Job: Script\n\n**Mode:** no_agent (script)\n\n---\n\n' + body
        else:
            body='PRIVATE RAW PROMPT\n## Response\nVerified result'
        (p/'2026-09-11_12-00-00.md').write_text(body)
    con.commit(); con.close()
    result=D.collect(tmp_path,now)
    assert result['overall_health']=='Healthy'
    assert result['sources'][0]['report'].startswith('*Script report*')
