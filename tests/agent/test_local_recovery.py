"""Durable local-attempt records never contain prompt or credential data."""

from __future__ import annotations

import json
import sqlite3

from agent.local_recovery import LocalAttemptLedger, recovery_allowed


def test_attempts_checkpoint_and_finish_are_durable_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.local_recovery.get_hermes_home", lambda: tmp_path)
    ledger = LocalAttemptLedger(session_id="session-1")
    attempt_id = ledger.begin(
        request_id="turn-1",
        model="meta/muse-glimmer",
        base_url="http://127.0.0.1:1234/v1",
        context_tokens=1200,
        message_cursor=7,
        model_generation="meta/muse-glimmer@q4_k_m",
        deadline=1234.5,
    )
    ledger.checkpoint(attempt_id, committed_tool_ids=["tool-1"])
    ledger.finish(
        attempt_id,
        success=False,
        error=RuntimeError("backend stalled; secret-token-must-not-be-stored"),
        committed_tool_ids=["tool-1"],
    )

    conn = sqlite3.connect(tmp_path / "local-runtime.db")
    row = conn.execute(
        "SELECT session_id, request_id, status, failure_class, failure_fingerprint, "
        "committed_tool_ids, context_tokens, message_cursor, model_generation, deadline "
        "FROM local_attempts"
    ).fetchone()
    conn.close()
    assert row[:4] == ("session-1", "turn-1", "failed", "RuntimeError")
    assert row[4]
    assert json.loads(row[5]) == ["tool-1"]
    assert row[6] == 1200
    assert row[7:10] == (7, "meta/muse-glimmer@q4_k_m", 1234.5)
    assert "secret-token" not in (tmp_path / "local-runtime.db").read_bytes().decode(
        "latin1", errors="ignore"
    )


def test_recovery_budget_is_finite():
    assert recovery_allowed(attempts=0, identical_failures=0, max_attempts=3)
    assert not recovery_allowed(attempts=3, identical_failures=0, max_attempts=3)
    assert not recovery_allowed(attempts=0, identical_failures=3, max_attempts=3)
