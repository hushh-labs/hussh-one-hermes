# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Push-on-change presence.

The point of this module is to stop paying for a fixed poll, so the tests that
matter are the ones proving it does not quietly become one, and that a
telemetry push can never break the thing that triggered it.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_pkm.presence import PresencePublisher, build_snapshot


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Publisher:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[dict] = []
        self.result = result

    def __call__(self, snapshot: dict) -> bool:
        self.sent.append(dict(snapshot))
        return self.result


def _make(state: dict, clock: _Clock, publisher: _Publisher, **kwargs):
    return PresencePublisher(
        publish=publisher,
        snapshot=lambda: dict(state),
        clock=clock,
        **kwargs,
    )


class TestOnlyPushesOnChange:
    def test_an_unchanged_snapshot_is_not_resent(self):
        clock, publisher = _Clock(), _Publisher()
        state = {"current_model": "gemma", "busy": False}
        presence = _make(state, clock, publisher)

        assert presence.on_event("connect") is True
        clock.advance(60)
        # Nothing about the machine changed. Sending again would be a poll with
        # extra steps, which is exactly what this module replaces.
        assert presence.on_event("session_start") is False
        assert len(publisher.sent) == 1

    def test_a_real_change_is_pushed(self):
        clock, publisher = _Clock(), _Publisher()
        state = {"current_model": "gemma", "busy": False}
        presence = _make(state, clock, publisher)
        presence.on_event("connect")

        clock.advance(60)
        state["current_model"] = "qwen"
        assert presence.on_event("model_loaded") is True
        assert publisher.sent[-1]["current_model"] == "qwen"
        assert publisher.sent[-1]["reason"] == "model_loaded"

    def test_the_reason_alone_does_not_count_as_a_change(self):
        # Otherwise every event would push, since the reason always differs,
        # and the change detection would do nothing at all.
        clock, publisher = _Clock(), _Publisher()
        presence = _make({"busy": False}, clock, publisher)
        presence.on_event("connect")
        clock.advance(60)
        assert presence.on_event("session_end") is False


class TestCoalescing:
    def test_a_burst_collapses_into_one_push(self):
        clock, publisher = _Clock(), _Publisher()
        state = {"current_model": "a"}
        presence = _make(state, clock, publisher, min_interval_seconds=5.0)

        presence.on_event("connect")
        for name in ("b", "c", "d"):
            clock.advance(0.5)
            state["current_model"] = name
            presence.on_event("model_loaded")

        # A model load emits several transitions; the owner experiences one.
        assert len(publisher.sent) == 1
        assert presence.has_pending_change is True

    def test_a_coalesced_change_is_not_lost(self):
        clock, publisher = _Clock(), _Publisher()
        state = {"current_model": "a"}
        presence = _make(state, clock, publisher, min_interval_seconds=5.0)
        presence.on_event("connect")

        clock.advance(0.5)
        state["current_model"] = "final"
        presence.on_event("model_loaded")
        assert presence.has_pending_change is True

        assert presence.flush() is True
        assert publisher.sent[-1]["current_model"] == "final"
        assert presence.has_pending_change is False

    def test_flush_is_a_noop_when_nothing_is_pending(self):
        clock, publisher = _Clock(), _Publisher()
        presence = _make({"busy": False}, clock, publisher)
        presence.on_event("connect")
        assert presence.flush() is False
        assert len(publisher.sent) == 1


class TestKeepalive:
    def test_nothing_is_sent_before_the_window_elapses(self):
        clock, publisher = _Clock(), _Publisher()
        presence = _make({"busy": False}, clock, publisher, keepalive_seconds=600.0)
        presence.on_event("connect")

        clock.advance(300)
        # Safe to call as often as the caller likes: the window decides, not
        # the caller's cadence.
        for _ in range(50):
            assert presence.keepalive() is False
        assert len(publisher.sent) == 1

    def test_it_fires_once_the_window_elapses_even_with_no_change(self):
        clock, publisher = _Clock(), _Publisher()
        presence = _make({"busy": False}, clock, publisher, keepalive_seconds=600.0)
        presence.on_event("connect")

        clock.advance(601)
        # Its only job: distinguish "nothing changed" from "this machine is
        # gone". Both look identical without it.
        assert presence.keepalive() is True
        assert publisher.sent[-1]["reason"] == "keepalive"

    def test_a_change_resets_the_keepalive_window(self):
        clock, publisher = _Clock(), _Publisher()
        state = {"current_model": "a"}
        presence = _make(state, clock, publisher, keepalive_seconds=600.0)
        presence.on_event("connect")

        clock.advance(590)
        state["current_model"] = "b"
        presence.on_event("model_loaded")
        clock.advance(20)
        # The transition already proved the machine is alive, so the keepalive
        # has nothing left to say for another full window.
        assert presence.keepalive() is False


class TestNeverBreaksItsCaller:
    def test_a_failing_publish_is_swallowed(self):
        def _explode(_snapshot):
            raise RuntimeError("network is down")

        presence = PresencePublisher(
            publish=_explode, snapshot=lambda: {"busy": False}, clock=_Clock()
        )
        # This runs off a model load and a session start. Neither should fail
        # because a telemetry push did.
        assert presence.on_event("model_loaded") is False

    def test_a_failing_snapshot_is_swallowed(self):
        def _explode():
            raise RuntimeError("sysctl unavailable")

        presence = PresencePublisher(
            publish=lambda _s: True, snapshot=_explode, clock=_Clock()
        )
        assert presence.on_event("connect") is False

    def test_a_publisher_returning_false_is_reported_not_raised(self):
        clock, publisher = _Clock(), _Publisher(result=False)
        presence = _make({"busy": False}, clock, publisher)
        assert presence.on_event("connect") is False
        assert len(publisher.sent) == 1


class TestSnapshot:
    def test_it_carries_the_specs_the_owner_sees_on_connect(self):
        snapshot = build_snapshot(current_model="gemma", active_sessions=1, busy=True)
        assert snapshot["current_model"] == "gemma"
        assert snapshot["active_sessions"] == 1
        assert snapshot["busy"] is True
        # brand/processor/ram come from the host and may be absent in CI, but
        # when present they must be the described-not-identifying kind.
        for key in snapshot:
            assert key not in {"hostname", "serial", "serial_number", "mac", "uuid"}

    def test_ram_used_pct_stays_in_range(self, monkeypatch):
        import hermes_cli.lmstudio_manager as lm

        monkeypatch.setattr(
            lm, "host_memory", lambda: {"total_gb": 128.0, "available_gb": 32.0}
        )
        snapshot = build_snapshot()
        assert snapshot["ram_used_pct"] == 75.0

    def test_an_unreadable_host_does_not_break_the_snapshot(self, monkeypatch):
        import hermes_cli.lmstudio_manager as lm

        def _explode():
            raise OSError("no sysctl here")

        monkeypatch.setattr(lm, "host_memory", _explode)
        # A machine that cannot report its memory can still report its model.
        snapshot = build_snapshot(current_model="gemma")
        assert snapshot["current_model"] == "gemma"
        assert "ram_used_pct" not in snapshot

    def test_negative_session_counts_are_floored(self):
        assert build_snapshot(active_sessions=-3)["active_sessions"] == 0


class TestHeartbeatCannotSeal:
    def test_post_heartbeat_never_calls_auth_headers(self, monkeypatch):
        # auth_headers refreshes the token, which runs the revocation check,
        # which can seal the device. A telemetry push must never be able to
        # destroy local data as a side effect of being sent.
        from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

        called: list[str] = []

        def _forbidden(self):
            called.append("auth_headers")
            raise AssertionError("post_heartbeat must not refresh the token")

        monkeypatch.setattr(HusshIdentityClient, "auth_headers", _forbidden)

        client = HusshIdentityClient.__new__(HusshIdentityClient)
        client._id_token = None
        monkeypatch.setattr(
            HusshIdentityClient, "read_state", lambda self: None, raising=False
        )
        assert client.post_heartbeat({"busy": False}) is False
        assert called == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
