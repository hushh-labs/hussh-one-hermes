# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Self-healing across the LM Studio session-stickiness bug.

Found 2026-09-01 by direct test on two shipping models: once a model has
loaded at some context within one running LM Studio process, neither the CLI
flag, the persisted default config file, nor a plain unload-and-reload can
move it to a different context. Only a fresh app process does. ``ensure_context``
encodes the one legitimate recovery (restart once, retry once) as orchestration
logic that is fully testable without touching a real app or a real server.
"""

from __future__ import annotations

from hermes_cli.hussh_one_routing import host as H


class _Host:
    """A fake LM Studio whose load behaviour and residency are scripted."""

    def __init__(self, *, loads_at=None):
        self._resident = []
        # A queue of contexts each successive load() call returns, so a test
        # can script "stuck, then fixed after restart" precisely.
        self._loads_at = list(loads_at or [])
        self.load_calls = []
        self.restart_calls = 0

    def resident(self):
        return list(self._resident)

    def unload(self, identifier):
        self._resident = [e for e in self._resident if e.get("identifier") != identifier]
        return True

    def load(self, model, context_length):
        self.load_calls.append((model, context_length))
        got = self._loads_at.pop(0) if self._loads_at else context_length
        self._resident = [{"identifier": model, "context": got}]
        return got

    def restart(self):
        # The scripted _loads_at queue already encodes what each successive
        # load() call returns, so a restart is just a count here; the retry
        # that follows pops the next (by construction, fixed) queue entry.
        self.restart_calls += 1


class TestEnsureContextWithoutRestart:
    def test_a_clean_match_returns_the_loaded_context(self):
        host = _Host()
        result = H.ensure_context(
            "m1", 98304, unload=host.unload, resident=host.resident, load=host.load
        )
        assert result == 98304
        assert host.load_calls == [("m1", 98304)]

    def test_a_mismatch_is_returned_not_hidden_when_no_restart_is_given(self):
        # Matches how ladder.walk already treats a mismatched rung: reported,
        # never silently retried, when there is no recovery path configured.
        host = _Host(loads_at=[262144])
        result = H.ensure_context(
            "m1", 98304, unload=host.unload, resident=host.resident, load=host.load
        )
        assert result == 262144
        assert len(host.load_calls) == 1

    def test_a_failed_drain_gives_up_without_attempting_a_load(self):
        def _stuck_resident():
            return [{"identifier": "ghost"}]

        def _refuses_to_unload(_identifier):
            return False

        calls = []
        result = H.ensure_context(
            "m1", 98304,
            unload=_refuses_to_unload,
            resident=_stuck_resident,
            load=lambda m, c: calls.append((m, c)) or c,
        )
        assert result is None
        assert calls == []


class TestEnsureContextWithRestart:
    def test_a_mismatch_triggers_exactly_one_restart_and_a_retry(self):
        # First load comes back stuck at the old context; the retry after
        # restart() honours the request, exactly the sequence proven live.
        host = _Host(loads_at=[262144, 98304])
        result = H.ensure_context(
            "m1", 98304,
            unload=host.unload, resident=host.resident,
            load=host.load, restart=host.restart,
        )
        assert result == 98304
        assert host.restart_calls == 1
        assert len(host.load_calls) == 2

    def test_a_still_stuck_model_after_the_restart_is_reported_not_looped(self):
        # Two restarts never happen per call: a second mismatch on a fresh
        # process means the request cannot be met, not that it is stale.
        host = _Host(loads_at=[262144, 262144, 98304])
        result = H.ensure_context(
            "m1", 98304,
            unload=host.unload, resident=host.resident,
            load=host.load, restart=host.restart,
        )
        assert result == 262144
        assert host.restart_calls == 1
        assert len(host.load_calls) == 2

    def test_max_restarts_can_be_raised_explicitly(self):
        host = _Host(loads_at=[262144, 262144, 98304])
        result = H.ensure_context(
            "m1", 98304,
            unload=host.unload, resident=host.resident,
            load=host.load, restart=host.restart, max_restarts=2,
        )
        assert result == 98304
        assert host.restart_calls == 2

    def test_no_restart_is_attempted_when_the_first_load_already_matches(self):
        host = _Host()
        H.ensure_context(
            "m1", 98304,
            unload=host.unload, resident=host.resident,
            load=host.load, restart=host.restart,
        )
        assert host.restart_calls == 0


class TestRestartAppIsDarwinOnly:
    def test_a_non_darwin_platform_refuses_rather_than_hangs(self, monkeypatch):
        monkeypatch.setattr(H.sys, "platform", "linux")
        try:
            H.restart_app(timeout=1)
        except RuntimeError as exc:
            assert "macOS" in str(exc)
        else:
            raise AssertionError("expected a RuntimeError on non-darwin")


class TestRestartAppClearsTheStaleLock:
    """The real defect, not a stand-in for it.

    SIGKILL is the only way to stop this app (it ignores SIGTERM), and
    SIGKILL skips the shutdown path that removes Electron's SingletonLock
    file. Left behind, every subsequent launch silently no-ops -- no error,
    no process, nothing on the port -- which is indistinguishable from "LM
    Studio crashed" from the outside. Found live: this exact lock was removed
    by hand once, never encoded into restart_app, so every kill after that
    first manual fix recreated the same trap for the next call. These pin the
    fix by exercising the real function end to end, with only subprocess and
    the network faked -- a reimplementation of the lock-removal logic here
    would prove nothing about whether restart_app itself does it.
    """

    def _patch_io(self, monkeypatch, *, server_up=True):
        calls = []
        monkeypatch.setattr(
            H.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})(),
        )
        if server_up:
            monkeypatch.setattr(
                H.urllib.request, "urlopen", lambda *a, **kw: object()
            )
        else:
            def _refuse(*a, **kw):
                raise OSError("connection refused")
            monkeypatch.setattr(H.urllib.request, "urlopen", _refuse)
        monkeypatch.setattr(H.time, "sleep", lambda _s: None)
        return calls

    def test_a_stale_lock_from_an_earlier_kill_is_removed(self, monkeypatch, tmp_path):
        lock = tmp_path / "SingletonLock"
        lock.symlink_to("Mac-99999")  # points at a pid that no longer exists
        monkeypatch.setattr(H, "_LMSTUDIO_SINGLETON_LOCK", lock)
        self._patch_io(monkeypatch)

        H.restart_app(timeout=5)
        assert not lock.exists()

    def test_no_lock_present_is_not_an_error(self, monkeypatch, tmp_path):
        # The common case: a clean quit already removed it. unlink() on a
        # missing path must not raise past this point.
        lock = tmp_path / "SingletonLock"
        monkeypatch.setattr(H, "_LMSTUDIO_SINGLETON_LOCK", lock)
        self._patch_io(monkeypatch)

        H.restart_app(timeout=5)  # must not raise

    def test_the_lock_is_removed_before_the_relaunch_is_attempted(self, monkeypatch, tmp_path):
        lock = tmp_path / "SingletonLock"
        lock.symlink_to("Mac-99999")
        monkeypatch.setattr(H, "_LMSTUDIO_SINGLETON_LOCK", lock)
        calls = self._patch_io(monkeypatch)

        H.restart_app(timeout=5)
        # 'open -a LM Studio' is the second subprocess call, after the pkill;
        # by then the lock must already be gone, or the relaunch it triggers
        # inherits the exact silent no-op this fix exists to prevent.
        assert lock.exists() is False
        assert any(cmd[:2] == ["open", "-a"] for cmd in calls)

    def test_the_default_timeout_has_real_headroom_over_the_measured_cold_start(self):
        # Measured on this machine: ~128s from kill to the server answering.
        # 120s (the old default) was BELOW that -- three restarts this
        # session were misdiagnosed as crashes because of exactly this gap.
        measured_cold_start_s = 128
        assert H.DEFAULT_RESTART_TIMEOUT_S > measured_cold_start_s * 2
