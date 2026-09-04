# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Push-on-change presence.

The point of this module is to stop paying for a fixed poll, so the tests that
matter are the ones proving it does not quietly become one, and that a
telemetry push can never break the thing that triggered it.
"""

from __future__ import annotations

import json
import sqlite3

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


class _Stores:
    """A profile home carrying just the two stores the summaries read."""

    def __init__(self, home):
        self.home = home

    def jobs(self, records, *, wrapped: bool = True):
        path = self.home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": records} if wrapped else records
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def sessions(self, rows):
        """Write session rows in the real column shape the query reads."""
        path = self.home / "state.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "create table sessions ("
                " id text primary key, title text, message_count integer,"
                " last_activity_at real, started_at real not null,"
                " archived integer not null default 0,"
                " hidden integer not null default 0,"
                " title_source text,"
                " parent_session_id text)"
            )
            connection.executemany(
                "insert into sessions (id, title, message_count, last_activity_at,"
                " started_at, archived, hidden, title_source, parent_session_id)"
                " values (:id, :title, :message_count, :last_activity_at,"
                " :started_at, :archived, :hidden, :title_source,"
                " :parent_session_id)",
                [
                    {
                        "id": row.get("id", f"s{index}"),
                        "title": row.get("title"),
                        "message_count": row.get("message_count", 0),
                        "last_activity_at": row.get("last_activity_at"),
                        "started_at": row.get("started_at", 1000.0),
                        "archived": row.get("archived", 0),
                        "hidden": row.get("hidden", 0),
                        # Defaults to a title the person typed, so a case that
                        # is not about provenance reads as one carryable row.
                        # The provenance cases set this explicitly.
                        "title_source": row.get("title_source", "user"),
                        "parent_session_id": row.get("parent_session_id"),
                    }
                    for index, row in enumerate(rows)
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return path


def _job(**overrides):
    """A cron record shaped like the store's own, payload fields included.

    The payload fields are here on purpose: they are what the summary must
    never carry, so every test that builds a job builds one that could leak.
    """
    record = {
        "id": "job_1",
        "name": "Doctor page",
        "prompt": "PROMPT-SECRET check the doctor inbox and page me",
        "script": "/Users/owner/bin/page.sh",
        "workdir": "/Users/owner/WORKDIR-SECRET",
        "base_url": "https://provider.example/v1",
        "monitor_url": "https://monitor.example/hook",
        "model": "qwen/qwen3-30b",
        "schedule": {"kind": "interval", "display": "every 15m", "expr": "*/15 * * * *"},
        "schedule_display": "every 15m",
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "next_run_at": "2026-09-04T03:10:00+00:00",
        "last_status": "ok",
        "last_error": "LASTERROR-SECRET traceback with output in it",
    }
    record.update(overrides)
    return record


class TestNameDerivedFromContentIsNotAName:
    """The leak an allow-list alone does not stop.

    Reading only permitted field names is not enough when the STORE has already
    put forbidden content into a permitted field. ``cron.jobs`` fills an empty
    name with the first 50 characters of the prompt, then that record can be
    saved, so the file itself holds the prompt in the name column. Every case
    here fails if the derivation guard is removed.
    """

    def test_a_name_that_is_really_the_prompt_is_refused(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        prompt = (
            "Read the founder's private notes at /Users/someone/secrets and "
            "summarise anything about the acquisition"
        )
        _Stores(tmp_path).jobs(
            [_job(name=prompt[:50].strip(), prompt=prompt, id="j-derived")]
        )

        rows = scheduled_summary(home=tmp_path)

        assert rows == [], "a prompt wearing the name field must not be carried"

    def test_a_name_that_is_really_the_script_path_is_refused(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        script = "/Users/someone/Documents/private/run_payroll.py"
        _Stores(tmp_path).jobs(
            [_job(name=script[:50].strip(), prompt="", script=script, id="j-script")]
        )

        rows = scheduled_summary(home=tmp_path)

        assert rows == [], "a filesystem path is not a name"

    def test_a_real_name_survives_even_when_a_prompt_exists(self, tmp_path):
        # The guard must not be so broad that it drops legitimate rows. Every
        # one of the founder's own jobs has a real name AND a prompt.
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs(
            [_job(name="Doctor page", prompt="Run the deterministic doctor script")]
        )

        rows = scheduled_summary(home=tmp_path)

        assert [row["name"] for row in rows] == ["Doctor page"]

    def test_the_refusal_survives_truncation(self, tmp_path):
        # The name reaching the guard is already cut to the wire cap, so a
        # comparison demanding exact equality would miss a long prompt.
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        prompt = "x" * 300
        _Stores(tmp_path).jobs([_job(name=prompt[:50], prompt=prompt, id="j-long")])

        rows = scheduled_summary(home=tmp_path)

        assert rows == []


class TestScheduledSummary:
    """What the machine says about its scheduled work, and what it never says."""

    def test_the_row_is_exactly_the_permitted_fields(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs([_job()])
        rows = scheduled_summary(home=tmp_path)

        assert rows == [
            {
                "name": "Doctor page",
                "when": "every 15m",
                "paused": False,
                "next_at": 1788491400,
                "last": "ok",
            }
        ]

    def test_no_payload_field_survives_even_when_the_record_carries_one(self, tmp_path):
        # The record is built field by field from named sources, never copied
        # and stripped, so a field added upstream cannot ship by default. This
        # is the whole reason the local job API cannot be passed through.
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        _Stores(tmp_path).jobs([_job()])
        wire = json.dumps(build_snapshot(home=tmp_path))

        assert "PROMPT-SECRET" not in wire
        assert "WORKDIR-SECRET" not in wire
        assert "LASTERROR-SECRET" not in wire
        assert "page.sh" not in wire
        assert "provider.example" not in wire
        assert "monitor.example" not in wire
        assert "qwen3-30b" not in wire
        # And the permitted half did land, so the assertions above are not
        # passing on an empty snapshot.
        assert "Doctor page" in wire

    def test_a_long_name_is_cut_to_the_wire_width(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import (
            SCHEDULED_NAME_MAX,
            SCHEDULED_WHEN_MAX,
            scheduled_summary,
        )

        long_name = "n" * 200
        long_when = "every " + "0" * 200
        _Stores(tmp_path).jobs(
            [_job(name=long_name, schedule_display=long_when)]
        )
        row = scheduled_summary(home=tmp_path)[0]

        assert row["name"] == long_name[:SCHEDULED_NAME_MAX]
        assert len(row["name"]) == SCHEDULED_NAME_MAX
        assert row["when"] == long_when[:SCHEDULED_WHEN_MAX]
        assert len(row["when"]) == SCHEDULED_WHEN_MAX

    def test_at_most_ten_rows_and_the_soonest_are_the_ones_kept(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import SUMMARY_MAX_ROWS, scheduled_summary

        # Written newest-last so a summary that merely truncated the file would
        # keep the wrong half.
        _Stores(tmp_path).jobs(
            [
                _job(id=f"job_{index}", name=f"job {index}", next_run_at=2000000000 - index)
                for index in range(25)
            ]
        )
        rows = scheduled_summary(home=tmp_path)

        assert len(rows) == SUMMARY_MAX_ROWS
        assert [row["name"] for row in rows] == [f"job {index}" for index in range(24, 14, -1)]

    def test_paused_work_sinks_below_live_work(self, tmp_path):
        # A paused job keeps the next_run_at it had when it was paused, forever
        # in the past. Sorting on time alone would let stopped work fill every
        # slot and hide the next real run.
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs(
            [
                _job(id="p", name="paused one", enabled=False, state="paused",
                     paused_at="2026-07-14T18:13:57+00:00", next_run_at=1000),
                _job(id="l", name="live one", next_run_at=2000000000),
            ]
        )
        rows = scheduled_summary(home=tmp_path)

        assert [row["name"] for row in rows] == ["live one", "paused one"]
        assert [row["paused"] for row in rows] == [False, True]

    def test_a_half_paused_record_reads_as_off(self, tmp_path):
        # enabled=true with a pause marker is a record the scheduler refuses to
        # fire. The owner is being told whether the work runs, so it is off.
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs(
            [_job(enabled=True, state="scheduled", paused_at="2026-07-14T18:13:57+00:00")]
        )
        assert scheduled_summary(home=tmp_path)[0]["paused"] is True

    def test_a_job_with_no_name_is_dropped_not_filled_from_its_prompt(self, tmp_path):
        # The local job API labels a nameless job with the first 50 characters
        # of its prompt. That fallback here would be the leak itself.
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs([_job(name="   "), _job(id="ok", name="named")])
        rows = scheduled_summary(home=tmp_path)

        assert [row["name"] for row in rows] == ["named"]

    def test_a_job_with_no_schedule_words_is_dropped(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs([_job(schedule_display="", schedule={"kind": "interval"})])
        assert scheduled_summary(home=tmp_path) == []

    def test_the_schedule_mapping_is_read_by_named_key_only(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs(
            [
                _job(
                    schedule_display="",
                    schedule={"kind": "cron", "expr": "0 5 * * 0", "notes": "NOTES-SECRET"},
                )
            ]
        )
        rows = scheduled_summary(home=tmp_path)

        assert rows[0]["when"] == "0 5 * * 0"
        assert "NOTES-SECRET" not in json.dumps(rows)

    def test_an_unreadable_next_run_omits_only_that_field(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        _Stores(tmp_path).jobs([_job(next_run_at="not a timestamp", last_status="")])
        row = scheduled_summary(home=tmp_path)[0]

        assert "next_at" not in row
        assert "last" not in row
        assert row["name"] == "Doctor page"

    def test_no_cron_store_is_no_rows(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import scheduled_summary

        assert scheduled_summary(home=tmp_path) == []


class TestOnlyTitlesThePersonOrTheAgentWrote:
    """A derived title IS the person's own first message, verbatim.

    ``hermes_state`` records where a title came from. Carrying a ``derived``
    one would put message content into a heartbeat stored on a server, which is
    the single thing this feature must never do. Every case here fails if the
    provenance filter is removed.
    """

    def test_a_derived_title_is_message_content_and_is_refused(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {
                    "title": "my mother's diagnosis came back and I need to",
                    "title_source": "derived",
                    "message_count": 12,
                    "last_activity_at": 1788491400.0,
                }
            ]
        )

        assert conversations_summary(home=tmp_path) == []

    def test_a_legacy_null_provenance_is_refused(self, tmp_path):
        # NULL predates the column. Its own ranking docstring says such rows
        # "were almost always set by the old auto-titler", so an unknown
        # provenance is not a licence to publish.
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {
                    "title": "whatever I happened to type first",
                    "title_source": None,
                    "message_count": 4,
                    "last_activity_at": 1788491400.0,
                }
            ]
        )

        assert conversations_summary(home=tmp_path) == []

    def test_titles_the_person_or_the_agent_wrote_are_carried(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {
                    "id": "a",
                    "title": "Tax planning",
                    "title_source": "user",
                    "message_count": 9,
                    "last_activity_at": 1788491400.0,
                },
                {
                    "id": "b",
                    "title": "Reviewing the deploy pipeline",
                    "title_source": "llm",
                    "message_count": 3,
                    "last_activity_at": 1788491000.0,
                },
                {
                    "id": "c",
                    "title": "so I was thinking about the thing where we",
                    "title_source": "derived",
                    "message_count": 40,
                    "last_activity_at": 1788491900.0,
                },
            ]
        )

        rows = conversations_summary(home=tmp_path)

        # The derived one is the FRESHEST, so a filter applied after the limit
        # rather than inside the query would have kept it.
        assert [row["title"] for row in rows] == [
            "Tax planning",
            "Reviewing the deploy pipeline",
        ]


class TestConversationsSummary:
    def test_the_row_is_exactly_the_permitted_fields_newest_first(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {"id": "a", "title": "Older chat", "message_count": 4,
                 "last_activity_at": 1000.0, "started_at": 900.0},
                {"id": "b", "title": "Newest chat", "message_count": 12,
                 "last_activity_at": 5000.5, "started_at": 900.0},
            ]
        )
        rows = conversations_summary(home=tmp_path)

        assert rows == [
            {"title": "Newest chat", "messages": 12, "at": 5000},
            {"title": "Older chat", "messages": 4, "at": 1000},
        ]

    def test_an_untitled_conversation_is_dropped_never_previewed(self, tmp_path):
        # A conversation with no title has no permitted name, and every local
        # lister fills that gap with the first message. There is no message
        # here to fall back to, by construction.
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {"id": "a", "title": None, "message_count": 3, "started_at": 9000.0},
                {"id": "b", "title": "   ", "message_count": 3, "started_at": 9001.0},
                {"id": "c", "title": "Real one", "message_count": 3, "started_at": 100.0},
            ]
        )
        assert [row["title"] for row in conversations_summary(home=tmp_path)] == ["Real one"]

    def test_archived_hidden_and_child_sessions_stay_out(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        _Stores(tmp_path).sessions(
            [
                {"id": "a", "title": "Archived", "started_at": 9000.0, "archived": 1},
                {"id": "b", "title": "Hidden", "started_at": 9001.0, "hidden": 1},
                {"id": "c", "title": "Subagent run", "started_at": 9002.0,
                 "parent_session_id": "d"},
                {"id": "d", "title": "The conversation", "started_at": 100.0},
            ]
        )
        assert [row["title"] for row in conversations_summary(home=tmp_path)] == [
            "The conversation"
        ]

    def test_a_long_title_is_cut_and_the_list_is_capped(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import (
            CONVERSATION_TITLE_MAX,
            SUMMARY_MAX_ROWS,
            conversations_summary,
        )

        _Stores(tmp_path).sessions(
            [
                {"id": f"s{index}", "title": ("t" * 200) + str(index),
                 "message_count": 1, "last_activity_at": 1000.0 + index,
                 "started_at": 1.0}
                for index in range(25)
            ]
        )
        rows = conversations_summary(home=tmp_path)

        assert len(rows) == SUMMARY_MAX_ROWS
        assert all(len(row["title"]) == CONVERSATION_TITLE_MAX for row in rows)
        # Freshest kept, so the truncated half is the useful one.
        assert rows[0]["title"].endswith("t")

    def test_the_message_count_stays_in_the_permitted_range(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import (
            CONVERSATION_MESSAGES_MAX,
            conversations_summary,
        )

        _Stores(tmp_path).sessions(
            [
                {"id": "a", "title": "Huge", "message_count": 10 ** 9,
                 "last_activity_at": 2000.0, "started_at": 1.0},
                {"id": "b", "title": "Null", "message_count": None,
                 "last_activity_at": 1000.0, "started_at": 1.0},
            ]
        )
        rows = conversations_summary(home=tmp_path)

        assert rows[0]["messages"] == CONVERSATION_MESSAGES_MAX
        assert rows[1]["messages"] == 0

    def test_the_read_cannot_write_to_the_owners_database(self, tmp_path):
        # SessionDB() runs migrations and flushes queued token counts, so
        # building one here would make a heartbeat a writer. mode=ro cannot.
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        path = _Stores(tmp_path).sessions(
            [{"id": "a", "title": "Chat", "message_count": 1, "started_at": 10.0}]
        )
        before = path.stat().st_mtime_ns
        assert conversations_summary(home=tmp_path)
        assert path.stat().st_mtime_ns == before

    def test_no_session_store_is_no_rows(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import conversations_summary

        assert conversations_summary(home=tmp_path) == []


class TestSummariesNeverBlockABeat:
    """A summary is a bonus. Liveness is the point, and it must still land."""

    def test_a_raising_scheduled_read_omits_only_its_own_key(self, tmp_path, monkeypatch):
        from hermes_cli.hussh_one_pkm import presence

        _Stores(tmp_path).sessions(
            [{"id": "a", "title": "Chat", "message_count": 2,
              "last_activity_at": 50.0, "started_at": 10.0}]
        )

        def _explode(**_kwargs):
            raise RuntimeError("jobs.json is corrupt")

        monkeypatch.setattr(presence, "scheduled_summary", _explode)
        snapshot = presence.build_snapshot(current_model="gemma", home=tmp_path)

        assert "scheduled" not in snapshot
        assert snapshot["current_model"] == "gemma"
        assert snapshot["conversations"][0]["title"] == "Chat"

    def test_a_raising_conversation_read_omits_only_its_own_key(self, tmp_path, monkeypatch):
        from hermes_cli.hussh_one_pkm import presence

        _Stores(tmp_path).jobs([_job()])

        def _explode(**_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(presence, "conversations_summary", _explode)
        snapshot = presence.build_snapshot(current_model="gemma", home=tmp_path)

        assert "conversations" not in snapshot
        assert snapshot["current_model"] == "gemma"
        assert snapshot["scheduled"][0]["name"] == "Doctor page"

    def test_a_corrupt_cron_store_omits_only_the_scheduled_key(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        path = tmp_path / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        snapshot = build_snapshot(current_model="gemma", home=tmp_path)
        assert "scheduled" not in snapshot
        assert snapshot["current_model"] == "gemma"

    def test_an_empty_store_is_absent_not_an_empty_list(self, tmp_path):
        # "The device did not report" and "the device has nothing scheduled"
        # are different answers. Sending [] would collapse them into one.
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        _Stores(tmp_path).jobs([])
        _Stores(tmp_path).sessions([])

        snapshot = build_snapshot(current_model="gemma", home=tmp_path)
        assert "scheduled" not in snapshot
        assert "conversations" not in snapshot

    def test_a_beat_from_a_bare_home_still_carries_the_machine(self, tmp_path):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        snapshot = build_snapshot(current_model="gemma", active_sessions=2, home=tmp_path)
        assert snapshot["current_model"] == "gemma"
        assert snapshot["active_sessions"] == 2
        assert "scheduled" not in snapshot
        assert "conversations" not in snapshot

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
