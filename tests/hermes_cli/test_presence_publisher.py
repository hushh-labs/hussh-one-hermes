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
        import hermes_cli.hussh_one_lmstudio as lm

        monkeypatch.setattr(
            lm, "host_memory", lambda: {"total_gb": 128.0, "available_gb": 32.0}
        )
        snapshot = build_snapshot()
        assert snapshot["ram_used_pct"] == 75.0

    def test_an_unreadable_host_does_not_break_the_snapshot(self, monkeypatch):
        import hermes_cli.hussh_one_lmstudio as lm

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


class TestHeartbeatWireShape:
    """The snapshot is the body. One reads telemetry at the top level.

    Measured on UAT: a body of ``{"heartbeat": {...}}`` reached the server,
    pydantic dropped the unknown key, and One stored ``last_heartbeat_at``
    with ``heartbeat: null``. The devices page could never say which model
    was running.
    """

    class _State:
        api_base = "https://api.example"
        device_id = "tdv_x"

    class _Http:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def post(self, url, headers=None, json=None):
            self.calls.append({"url": url, "headers": headers, "json": json})

            class _Response:
                status_code = 200

            return _Response()

    def _client(self, monkeypatch):
        from hermes_cli.hussh_one_pkm.client import HusshIdentityClient

        client = HusshIdentityClient.__new__(HusshIdentityClient)
        client._id_token = "tok"
        client.http = self._Http()
        state = self._State()
        monkeypatch.setattr(
            HusshIdentityClient, "read_state", lambda self: state, raising=False
        )
        return client

    def test_the_snapshot_is_posted_flat_with_current_model_at_the_top_level(
        self, monkeypatch
    ):
        client = self._client(monkeypatch)
        snapshot = {"current_model": "gemma", "busy": False, "active_sessions": 1}

        assert client.post_heartbeat(snapshot) is True

        assert len(client.http.calls) == 1
        call = client.http.calls[0]
        assert call["json"] == snapshot
        assert call["json"]["current_model"] == "gemma"
        assert "heartbeat" not in call["json"]
        assert call["url"].endswith("/api/account/trusted-devices/tdv_x/heartbeat")
        assert call["headers"]["Authorization"] == "Bearer tok"

    def test_a_non_2xx_is_reported_not_raised(self, monkeypatch):
        client = self._client(monkeypatch)

        def _post(url, headers=None, json=None):
            class _Response:
                status_code = 401

            return _Response()

        client.http.post = _post
        assert client.post_heartbeat({"busy": False}) is False


class TestModelAndVersionReaders:
    """What the bridge puts in the snapshot, read the way ``agent_section`` reads it."""

    def test_model_default_wins_and_is_stripped(self):
        from hermes_cli.hussh_one_pkm.presence import current_model_from_config

        cfg = {"model": {"default": "  qwen/qwen3-30b  ", "model": "legacy"}}
        assert current_model_from_config(cfg) == "qwen/qwen3-30b"

    def test_legacy_model_key_is_the_fallback(self):
        from hermes_cli.hussh_one_pkm.presence import current_model_from_config

        assert current_model_from_config({"model": {"model": "gemma"}}) == "gemma"

    @pytest.mark.parametrize(
        "cfg", [None, {}, {"model": "not-a-dict"}, {"model": {"default": None}}]
    )
    def test_an_unreadable_pin_is_empty_not_wrong(self, cfg):
        from hermes_cli.hussh_one_pkm.presence import current_model_from_config

        assert current_model_from_config(cfg) == ""

    def test_current_model_reads_the_config_at_call_time(self, monkeypatch):
        import hermes_cli.config as config
        from hermes_cli.hussh_one_pkm.presence import current_model

        pins = iter(["gemma", "qwen"])
        monkeypatch.setattr(
            config, "load_config_readonly", lambda: {"model": {"default": next(pins)}}
        )
        assert current_model() == "gemma"
        assert current_model() == "qwen"

    def test_a_config_failure_never_blocks_a_heartbeat(self, monkeypatch):
        import hermes_cli.config as config
        from hermes_cli.hussh_one_pkm.presence import current_model

        def _explode():
            raise OSError("config unreadable")

        monkeypatch.setattr(config, "load_config_readonly", _explode)
        assert current_model() == ""

    def test_agent_version_is_the_package_version(self):
        import hermes_cli
        from hermes_cli.hussh_one_pkm.presence import agent_version

        assert agent_version() == hermes_cli.__version__
        assert agent_version()

    def test_an_empty_model_is_omitted_from_the_snapshot(self):
        # build_snapshot drops "" values, so an unreadable pin is absent on the
        # wire rather than reported as a model named "".
        snapshot = build_snapshot(current_model="", agent_version="")
        assert "current_model" not in snapshot
        assert "agent_version" not in snapshot


class TestBridgeSnapshotCarriesTheModel:
    """The owner's devices page can only name the model if the bridge sends it."""

    class _Keychain:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def get(self, account):
            return self.values.get(account)

        def set(self, account, secret):
            self.values[account] = secret

        def delete(self, account):
            self.values.pop(account, None)

    def test_each_push_reads_the_pin_at_that_moment(self, tmp_path, monkeypatch):
        import hermes_cli
        import hermes_cli.config as config
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        pins = {"default": "gemma"}
        monkeypatch.setattr(config, "load_config_readonly", lambda: {"model": dict(pins)})

        bridge = HusshVaultBridge(
            profile_home=tmp_path, keychain=self._Keychain()  # type: ignore[arg-type]
        )
        snapshot_fn = bridge.presence._snapshot

        first = snapshot_fn()
        assert first["current_model"] == "gemma"
        assert first["agent_version"] == hermes_cli.__version__
        assert first["active_sessions"] == 0
        assert first["busy"] is False

        # A model swap after the bridge was built is a transition, not a stale
        # reading: the lambda reads the config, it does not capture it.
        pins["default"] = "qwen"
        assert snapshot_fn()["current_model"] == "qwen"

    def test_a_config_failure_still_produces_a_snapshot(self, tmp_path, monkeypatch):
        import hermes_cli.config as config
        from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

        def _explode():
            raise OSError("config unreadable")

        monkeypatch.setattr(config, "load_config_readonly", _explode)
        bridge = HusshVaultBridge(
            profile_home=tmp_path, keychain=self._Keychain()  # type: ignore[arg-type]
        )
        snapshot = bridge.presence._snapshot()
        assert "current_model" not in snapshot
        assert snapshot["active_sessions"] == 0


class TestTextFieldsFitTheServer:
    def test_long_model_id_is_capped_so_the_beat_is_never_refused(self):
        # One keeps 120 characters of text and used to refuse the WHOLE beat
        # when one field ran over. A device whose beats are refused reads as
        # gone while it is healthy, so the cap is applied here as well.
        from hermes_cli.hussh_one_pkm.presence import SERVER_TEXT_MAX, build_snapshot

        long_id = "lmstudio-community/" + ("x" * 200)
        snapshot = build_snapshot(current_model=long_id, agent_version="v" * 300)
        assert len(snapshot["current_model"]) == SERVER_TEXT_MAX
        assert snapshot["current_model"] == long_id[:SERVER_TEXT_MAX]
        assert len(snapshot["agent_version"]) == SERVER_TEXT_MAX

    def test_blank_text_is_still_omitted(self):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        snapshot = build_snapshot(current_model="   ", agent_version="")
        assert "current_model" not in snapshot
        assert "agent_version" not in snapshot


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
