"""Regression tests for the PTY reader busy-spin guard (_pty_reader_backoff).

Root cause captured: the dashboard's embedded-TUI PTY reader loop
(pump_pty_to_ws in hermes_cli/web_server.py) could busy-spin a full CPU core
when bridge.read() returned empty *immediately* in a degenerate half-closed
PTY/WS state — observed as the dashboard process running 145h at 101% CPU.
The _pty_reader_backoff() helper makes the loop structurally unable to spin:
suspiciously-fast empty reads earn an escalating backoff sleep, while healthy
idle reads (which block in select) get a zero backoff (yield only).
"""

import pytest

from hermes_cli.web_server import (
    _pty_reader_backoff,
    _PTY_READ_CHUNK_TIMEOUT,
    _PTY_SPIN_FAST_THRESHOLD,
    _PTY_SPIN_BACKOFF_MAX,
)


def test_healthy_idle_read_gets_zero_backoff():
    # A healthy idle read blocks ~_PTY_READ_CHUNK_TIMEOUT in select before
    # returning empty → no backoff, loop just yields (sleep 0).
    assert _pty_reader_backoff(_PTY_READ_CHUNK_TIMEOUT, 5) == 0.0
    # Exactly at the threshold also counts as healthy (>=).
    assert _pty_reader_backoff(_PTY_SPIN_FAST_THRESHOLD, 3) == 0.0


def test_instant_empty_read_gets_positive_backoff():
    # A degenerate read that returns empty almost instantly must earn a
    # positive backoff so the loop cannot peg a core.
    backoff = _pty_reader_backoff(0.0, 1)
    assert backoff > 0.0


def test_backoff_escalates_with_streak():
    # Sustained fast-empty reads escalate the sleep, capped at the max.
    b1 = _pty_reader_backoff(0.0, 1)
    b2 = _pty_reader_backoff(0.0, 2)
    b3 = _pty_reader_backoff(0.0, 3)
    assert b1 < b2 < b3
    assert all(b <= _PTY_SPIN_BACKOFF_MAX for b in (b1, b2, b3))


def test_backoff_capped_at_max():
    # A very long streak is clamped to the ceiling — never an unbounded sleep.
    assert _pty_reader_backoff(0.0, 10_000) == _PTY_SPIN_BACKOFF_MAX


def test_spin_is_impossible_under_sustained_instant_empty_reads():
    # The core guarantee: simulate the failure mode (read() always returns
    # empty instantly). Over many iterations the loop must accumulate real
    # sleep time rather than spinning — i.e. average backoff per iteration is
    # bounded well above zero, so CPU cannot be pegged.
    streak = 0
    total_backoff = 0.0
    iterations = 1000
    for _ in range(iterations):
        backoff = _pty_reader_backoff(0.0, streak + 1)
        assert backoff > 0.0  # every instant-empty read sleeps
        streak += 1
        total_backoff += backoff
    # With escalation+cap, sustained spin sleeps at the ceiling — proving the
    # loop is rate-limited (a true busy-spin would accumulate ~0 sleep).
    assert total_backoff >= iterations * _PTY_SPIN_BACKOFF_MAX * 0.9
