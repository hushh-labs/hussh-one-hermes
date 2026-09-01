# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Battery reporting.

The reading that must never be wrong is the desktop one. A machine with no
battery has to report absence, not zero: a false reading and a missing one look
identical downstream, and "0%" on a Mac Studio would read as a laptop about to
die.
"""

from __future__ import annotations

import pytest

import hermes_cli.hussh_one_host_metrics as hm

LAPTOP_DISCHARGING = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=7602275)\t27%; discharging; 1:35 remaining present: true"
)
LAPTOP_CHARGING = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=7602275)\t64%; charging; 0:48 remaining present: true"
)
LAPTOP_FULL_ON_AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=7602275)\t100%; charged; 0:00 remaining present: true"
)
LAPTOP_HELD_AT_LIMIT = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=7602275)\t80%; AC attached; not charging present: true"
)
LAPTOP_CALCULATING = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=7602275)\t55%; discharging; 0:00 remaining present: true"
)
DESKTOP = "Now drawing from 'AC Power'"


@pytest.fixture
def on_darwin(monkeypatch):
    monkeypatch.setattr(hm.sys, "platform", "darwin")


def _pmset(monkeypatch, output):
    monkeypatch.setattr(hm, "_run", lambda cmd, timeout=3: output)


class TestDesktopReportsAbsenceNotZero:
    def test_a_machine_with_no_battery_reports_present_false(self, monkeypatch, on_darwin):
        _pmset(monkeypatch, DESKTOP)
        battery = hm.host_battery()
        assert battery["present"] is False
        # The critical assertion: no number at all. A zero here is a false
        # reading, and nothing downstream could tell it from a flat laptop.
        assert "percent" not in battery

    def test_psutil_none_means_no_battery_not_an_error(self, monkeypatch):
        monkeypatch.setattr(hm.sys, "platform", "linux")
        fake = type("P", (), {"sensors_battery": staticmethod(lambda: None)})
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake)
        assert hm.host_battery() == {"present": False}


class TestLaptopStates:
    def test_discharging(self, monkeypatch, on_darwin):
        _pmset(monkeypatch, LAPTOP_DISCHARGING)
        battery = hm.host_battery()
        assert battery["present"] is True
        assert battery["percent"] == 27
        assert battery["state"] == "discharging"
        assert battery["charging"] is False
        assert battery["on_ac"] is False
        assert battery["minutes_remaining"] == 95

    def test_charging(self, monkeypatch, on_darwin):
        _pmset(monkeypatch, LAPTOP_CHARGING)
        battery = hm.host_battery()
        assert battery["charging"] is True
        assert battery["on_ac"] is True
        assert battery["percent"] == 64

    def test_charged_on_ac_is_not_charging(self, monkeypatch, on_darwin):
        # Full and plugged in. Power is available but none is going in, and
        # saying "charging" would misdescribe that.
        _pmset(monkeypatch, LAPTOP_FULL_ON_AC)
        battery = hm.host_battery()
        assert battery["state"] == "charged"
        assert battery["charging"] is False
        assert battery["on_ac"] is True

    def test_held_at_a_charge_limit_is_on_ac_but_not_charging(self, monkeypatch, on_darwin):
        # macOS holds at 80% to preserve battery health. Plugged in, not
        # charging. Reporting this as charging would tell the owner the level
        # is about to rise when it will not.
        _pmset(monkeypatch, LAPTOP_HELD_AT_LIMIT)
        battery = hm.host_battery()
        assert battery["on_ac"] is True
        assert battery["charging"] is False
        assert battery["percent"] == 80

    def test_charging_and_discharging_can_never_both_be_true(self, monkeypatch, on_darwin):
        # Both flags derive from one state string, so a single snapshot cannot
        # contradict itself.
        for output in (
            LAPTOP_DISCHARGING,
            LAPTOP_CHARGING,
            LAPTOP_FULL_ON_AC,
            LAPTOP_HELD_AT_LIMIT,
        ):
            _pmset(monkeypatch, output)
            battery = hm.host_battery()
            if battery["charging"]:
                assert battery["on_ac"] is True


class TestTimeRemaining:
    def test_a_calculating_estimate_is_omitted_not_reported_as_zero(
        self, monkeypatch, on_darwin
    ):
        # macOS prints 0:00 while it works the estimate out. "0 minutes
        # remaining" reads as an imminent shutdown.
        _pmset(monkeypatch, LAPTOP_CALCULATING)
        assert "minutes_remaining" not in hm.host_battery()

    def test_hours_and_minutes_are_summed(self, monkeypatch, on_darwin):
        _pmset(monkeypatch, LAPTOP_DISCHARGING)
        assert hm.host_battery()["minutes_remaining"] == 95


class TestProbeFailures:
    def test_an_unavailable_pmset_does_not_raise(self, monkeypatch, on_darwin):
        _pmset(monkeypatch, "")
        monkeypatch.setitem(__import__("sys").modules, "psutil", None)
        assert hm.host_battery() == {}

    def test_a_raising_probe_is_swallowed(self, monkeypatch, on_darwin):
        def _explode(cmd, timeout=3):
            raise OSError("no pmset here")

        monkeypatch.setattr(hm, "_run", _explode)
        assert hm.host_battery() == {}


class TestPresenceSnapshot:
    def test_a_laptop_snapshot_carries_battery(self, monkeypatch):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        monkeypatch.setattr(
            hm,
            "host_battery",
            lambda: {
                "present": True,
                "percent": 27,
                "charging": False,
                "on_ac": False,
                "minutes_remaining": 102,
            },
        )
        snapshot = build_snapshot(current_model="gemma")
        assert snapshot["battery_pct"] == 27
        assert snapshot["battery_charging"] is False
        assert snapshot["battery_minutes_remaining"] == 102

    def test_a_desktop_snapshot_carries_no_battery_fields(self, monkeypatch):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        monkeypatch.setattr(hm, "host_battery", lambda: {"present": False})
        snapshot = build_snapshot(current_model="gemma")
        for key in (
            "battery_pct",
            "battery_charging",
            "on_ac",
            "battery_minutes_remaining",
        ):
            assert key not in snapshot

    def test_a_failing_battery_probe_does_not_break_the_snapshot(self, monkeypatch):
        from hermes_cli.hussh_one_pkm.presence import build_snapshot

        def _explode():
            raise RuntimeError("probe down")

        monkeypatch.setattr(hm, "host_battery", _explode)
        assert build_snapshot(current_model="gemma")["current_model"] == "gemma"
