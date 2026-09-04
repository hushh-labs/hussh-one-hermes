# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Restart without cutting anyone off mid-answer.

The failure this prevents is losing the owner's reply. The failures it must not
CAUSE are an agent that can never be updated, and one left permanently refusing
new turns after a cancelled restart.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import hussh_one_graceful_restart as gr


class _Lease:
    def __init__(self, idle: bool) -> None:
        self.idle = idle


class _Registry:
    """A lease registry whose busy set can be scripted per poll."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self._leases: dict = {}
        self._advance()

    def _advance(self) -> None:
        busy = self._script.pop(0) if self._script else []
        self._leases = {s: _Lease(idle=False) for s in busy}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _sleeper(clock, registry):
    def _sleep(seconds):
        clock.now += seconds
        registry._advance()

    return _sleep


class TestDrainWaitsForRealTurns:
    def test_an_idle_agent_is_ready_immediately(self, tmp_path):
        registry = _Registry([[]])
        status = gr.drain(registry=registry, hermes_home=tmp_path)
        assert status.phase == gr.PHASE_READY
        assert status.active_turns == 0

    def test_it_waits_until_the_last_turn_finishes(self, tmp_path):
        clock = _Clock()
        registry = _Registry([["s1", "s2"], ["s1"], []])
        status = gr.drain(
            registry=registry,
            clock=clock,
            sleep=_sleeper(clock, registry),
            hermes_home=tmp_path,
        )
        assert status.phase == gr.PHASE_READY
        assert clock.now > 0  # it actually waited

    def test_an_unreadable_lease_counts_as_busy(self):
        # Guessing "free" would restart through the turn this exists to protect.
        class _Broken:
            @property
            def idle(self):
                raise RuntimeError("lease is wedged")

        class _Reg:
            _leases = {"s1": _Broken()}

        count, sessions = gr.count_active_turns(_Reg())
        assert count == 1
        assert sessions == ["s1"]

    def test_a_registry_without_leases_reports_none_rather_than_crashing(self):
        assert gr.count_active_turns(object()) == (0, [])


class TestTheDeadline:
    def test_it_gives_up_and_names_what_it_would_interrupt(self, tmp_path):
        # An agent that can never be updated because one session is wedged is
        # its own kind of broken.
        clock = _Clock()
        registry = _Registry([["stuck"]] * 50)
        status = gr.drain(
            registry=registry,
            timeout_s=5.0,
            clock=clock,
            sleep=_sleeper(clock, registry),
            hermes_home=tmp_path,
        )
        assert status.phase == gr.PHASE_ABANDONED
        assert status.interrupted_sessions == ["stuck"]

    def test_an_abandoned_drain_does_not_restart_by_default(self, tmp_path):
        clock = _Clock()
        registry = _Registry([["stuck"]] * 50)
        signalled = []
        status = gr.graceful_restart(
            registry=registry,
            timeout_s=5.0,
            clock=clock,
            sleep=_sleeper(clock, registry),
            hermes_home=tmp_path,
        )
        assert status.phase == gr.PHASE_ABANDONED
        assert signalled == []

    def test_force_restarts_anyway(self, tmp_path, monkeypatch):
        clock = _Clock()
        registry = _Registry([["stuck"]] * 50)
        sent = []
        monkeypatch.setattr(gr.os, "kill", lambda pid, sig: sent.append(sig))
        status = gr.graceful_restart(
            registry=registry,
            timeout_s=5.0,
            force=True,
            clock=clock,
            sleep=_sleeper(clock, registry),
            hermes_home=tmp_path,
        )
        assert status.phase == gr.PHASE_RESTARTING
        assert sent  # the signal was actually sent


class TestQuiesceIsAlwaysLifted:
    def test_a_cancelled_drain_resumes_new_turns(self, tmp_path):
        # Otherwise a cancelled restart leaves the agent permanently refusing
        # work -- worse than the interrupted turn it was avoiding.
        resumed = []
        clock = _Clock()
        registry = _Registry([["s1"]] * 50)

        class _Cancelled(BaseException):
            """A BaseException, like a real cancel, but not one pytest treats
            as a session abort."""

        def _explode(_seconds):
            raise _Cancelled()

        with pytest.raises(_Cancelled):
            gr.drain(
                registry=registry,
                quiesce=lambda: None,
                resume=lambda: resumed.append(True),
                clock=clock,
                sleep=_explode,
                hermes_home=tmp_path,
            )
        assert resumed == [True]

    def test_an_abandoned_drain_resumes_when_not_forcing(self, tmp_path):
        resumed = []
        clock = _Clock()
        registry = _Registry([["stuck"]] * 50)
        gr.graceful_restart(
            registry=registry,
            resume=lambda: resumed.append(True),
            timeout_s=5.0,
            clock=clock,
            sleep=_sleeper(clock, registry),
            hermes_home=tmp_path,
        )
        assert resumed == [True]

    def test_a_failing_quiesce_is_recorded_not_swallowed(self, tmp_path):
        def _bad():
            raise RuntimeError("cannot quiesce")

        status = gr.drain(
            registry=_Registry([[]]), quiesce=_bad, hermes_home=tmp_path
        )
        # New turns keep arriving, so the drain may not converge. Say so
        # rather than appearing to make progress for the full deadline.
        assert "quiesce failed" in status.reason


class TestStatusIsVisible:
    def test_the_phase_is_published_for_the_app_to_read(self, tmp_path):
        gr.drain(registry=_Registry([[]]), hermes_home=tmp_path)
        published = json.loads(gr.status_path(tmp_path).read_text())
        assert published["phase"] == gr.PHASE_READY
        assert "message" in published

    def test_the_message_is_a_sentence_not_a_status_code(self):
        status = gr.RestartStatus(phase=gr.PHASE_DRAINING, active_turns=2)
        assert status.message() == "Waiting for 2 replies to finish before restarting."
        assert gr.RestartStatus(phase=gr.PHASE_DRAINING, active_turns=1).message() == (
            "Waiting for 1 reply to finish before restarting."
        )

    def test_reading_with_no_published_status_reports_idle(self, tmp_path):
        assert gr.read_status(tmp_path)["phase"] == gr.PHASE_IDLE

    def test_publishing_never_raises_on_an_unwritable_home(self):
        # A restart must not fail because it could not describe itself.
        gr.publish(gr.RestartStatus(), hermes_home="/dev/null/nope")

    def test_an_abandoned_status_says_what_did_not_finish(self):
        status = gr.RestartStatus(
            phase=gr.PHASE_ABANDONED, waited_s=90.0, interrupted_sessions=["a", "b"]
        )
        assert "did not finish" in status.message()


class TestRestartUsesTheExistingPath:
    def test_it_sends_sigusr1(self, tmp_path):
        sent = []
        gr.restart_now(
            status=gr.RestartStatus(),
            hermes_home=tmp_path,
            pid=1234,
            signaller=lambda pid, sig: sent.append((pid, sig)),
        )
        assert sent == [(1234, gr.signal.SIGUSR1)]

    def test_a_failed_signal_returns_to_idle_rather_than_claiming_restart(
        self, tmp_path
    ):
        def _fail(pid, sig):
            raise PermissionError("not allowed")

        status = gr.RestartStatus()
        assert (
            gr.restart_now(
                status=status, hermes_home=tmp_path, pid=1, signaller=_fail
            )
            is False
        )
        assert status.phase == gr.PHASE_IDLE
