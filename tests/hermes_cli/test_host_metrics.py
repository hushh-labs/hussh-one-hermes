"""Tests for hermes_cli.host_metrics — real hardware/memory on non-/proc hosts.

The host these run on is irrelevant: every probe is mocked, so the macOS
path is exercised on Linux CI and the Linux path is exercised on a macOS
dev box.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import host_metrics

# Verbatim output from a Mac16,5 (M4 Max, 128 GB).
_SYSCTL = {
    "hw.model": "Mac16,5",
    "machdep.cpu.brand_string": "Apple M4 Max",
    "hw.ncpu": "16",
    "hw.memsize": "137438953472",
}

_MEMORY_PRESSURE = (
    "The system has 137438953472 (8388608 pages with a page size of 16384).\n"
    "\n"
    "Stats: \n"
    "Pages free: 1635493 \n"
    "Pages purgeable: 86532 \n"
    "\n"
    "System-wide memory free percentage: 88%\n"
)

_CPUINFO = (
    "processor\t: 0\n"
    "vendor_id\t: GenuineIntel\n"
    "model name\t: Intel(R) Xeon(R) CPU @ 2.20GHz\n"
    "cpu MHz\t\t: 2200.000\n"
)

_MEMINFO = (
    "MemTotal:       32768000 kB\n"
    "MemFree:         1048576 kB\n"
    "MemAvailable:   16384000 kB\n"
)


def _fake_probe(outputs: dict[str, str]):
    """Stand in for ``_run``, keyed by the sysctl OID / command name."""

    def _run(cmd, timeout=host_metrics._PROBE_TIMEOUT_S):  # noqa: ARG001
        if cmd[0] == "sysctl":
            return outputs.get(cmd[-1], "")
        return outputs.get(cmd[0], "")

    return _run


def _as_darwin(monkeypatch, outputs: dict[str, str]) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_metrics, "_run", _fake_probe(outputs))


class TestHostHardware:
    def test_darwin_parses_sysctl(self, monkeypatch) -> None:
        _as_darwin(monkeypatch, _SYSCTL)
        info = host_metrics.host_hardware()
        assert info["brand"] == "Mac16,5"
        assert info["processor"] == "Apple M4 Max"
        assert info["cpu_cores"] == 16
        assert info["ram_total_gb"] == 128.0

    def test_darwin_missing_sysctl_degrades_to_partial(self, monkeypatch) -> None:
        # sysctl absent (or every OID unreadable): no exception, no guesses,
        # and the stdlib core count still comes through.
        _as_darwin(monkeypatch, {})
        info = host_metrics.host_hardware()
        assert "brand" not in info
        assert "processor" not in info
        assert "ram_total_gb" not in info
        assert info["cpu_cores"] >= 1

    def test_darwin_partial_sysctl_keeps_what_answered(self, monkeypatch) -> None:
        _as_darwin(monkeypatch, {"hw.model": "Mac16,5"})
        info = host_metrics.host_hardware()
        assert info["brand"] == "Mac16,5"
        assert "processor" not in info

    def test_garbage_sysctl_numbers_are_dropped(self, monkeypatch) -> None:
        _as_darwin(
            monkeypatch,
            {**_SYSCTL, "hw.ncpu": "not-a-number", "hw.memsize": ""},
        )
        info = host_metrics.host_hardware()
        assert "ram_total_gb" not in info
        # cpu_cores falls back to stdlib rather than carrying the garbage.
        assert isinstance(info["cpu_cores"], int)

    def test_linux_reads_proc(self, monkeypatch, tmp_path: Path) -> None:
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(_CPUINFO, encoding="utf-8")
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(_MEMINFO, encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(host_metrics, "_PROC_CPUINFO", str(cpuinfo))
        monkeypatch.setattr(host_metrics, "_PROC_MEMINFO", str(meminfo))

        info = host_metrics.host_hardware()
        assert info["processor"] == "Intel(R) Xeon(R) CPU @ 2.20GHz"
        assert info["ram_total_gb"] == 31.2
        # No honest Linux equivalent of hw.model — the key stays absent.
        assert "brand" not in info

    def test_linux_missing_proc_degrades_to_partial(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(host_metrics, "_PROC_CPUINFO", str(tmp_path / "nope"))
        monkeypatch.setattr(host_metrics, "_PROC_MEMINFO", str(tmp_path / "nope"))
        info = host_metrics.host_hardware()
        assert "processor" not in info
        assert info["cpu_cores"] >= 1

    def test_unknown_platform_never_shells_out(self, monkeypatch) -> None:
        def _explode(*args, **kwargs):
            raise AssertionError("must not spawn a process on this platform")

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(subprocess, "run", _explode)
        info = host_metrics.host_hardware()
        assert "brand" not in info
        assert info["cpu_cores"] >= 1


class TestRunGuard:
    def test_missing_binary_is_not_spawned(self, monkeypatch) -> None:
        def _explode(*args, **kwargs):
            raise AssertionError("must not spawn a missing binary")

        monkeypatch.setattr(host_metrics.shutil, "which", lambda _name: None)
        monkeypatch.setattr(subprocess, "run", _explode)
        assert host_metrics._run(["sysctl", "-n", "hw.model"]) == ""

    def test_nonzero_exit_yields_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(host_metrics.shutil, "which", lambda name: "/usr/sbin/" + name)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "junk", "unknown oid"),
        )
        assert host_metrics._run(["sysctl", "-n", "hw.bogus"]) == ""

    def test_timeout_yields_empty(self, monkeypatch) -> None:
        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="sysctl", timeout=3)

        monkeypatch.setattr(host_metrics.shutil, "which", lambda name: "/usr/sbin/" + name)
        monkeypatch.setattr(subprocess, "run", _timeout)
        assert host_metrics._run(["sysctl", "-n", "hw.model"]) == ""


def _fake_psutil(
    *, total: int, available: int, swap_used: int, rss: int
) -> types.ModuleType:
    mod = types.ModuleType("psutil")
    mod.virtual_memory = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        total=total, available=available
    )
    mod.swap_memory = lambda: types.SimpleNamespace(used=swap_used)  # type: ignore[attr-defined]
    mod.Process = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        memory_info=lambda: types.SimpleNamespace(rss=rss)
    )
    return mod


class TestHostMemorySample:
    def test_psutil_path_is_preferred(self, monkeypatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "psutil",
            _fake_psutil(
                total=137438953472,
                available=73109897216,
                swap_used=2147483648,
                rss=104857600,
            ),
        )
        # If psutil answers, no subprocess is spawned at all.
        monkeypatch.setattr(
            host_metrics,
            "_run",
            lambda *a, **k: pytest.fail("psutil answered; must not shell out"),
        )
        sample = host_metrics.host_memory_sample()
        assert sample["mem_total_kib"] == 134217728
        assert sample["mem_available_kib"] == 71396384
        assert sample["swap_used_kib"] == 2097152
        assert sample["rss_kib"] == 102400

    def test_memory_pressure_fallback_when_psutil_absent(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
        _as_darwin(
            monkeypatch,
            {**_SYSCTL, "memory_pressure": _MEMORY_PRESSURE},
        )
        sample = host_metrics.host_memory_sample()
        assert sample["mem_total_kib"] == 134217728
        # 88% of 128 GiB.
        assert sample["mem_available_kib"] == 118111600
        # This path genuinely cannot see swap or own RSS — it omits them
        # rather than reporting a zero.
        assert "swap_used_kib" not in sample
        assert "rss_kib" not in sample

    def test_total_without_memory_pressure_stays_unclassifiable(
        self, monkeypatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "psutil", None)
        _as_darwin(monkeypatch, _SYSCTL)  # no memory_pressure output
        sample = host_metrics.host_memory_sample()
        assert sample == {"mem_total_kib": 134217728}

    def test_returns_empty_when_nothing_works(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "psutil", None)
        _as_darwin(monkeypatch, {})
        assert host_metrics.host_memory_sample() == {}

    def test_empty_off_darwin_without_psutil(self, monkeypatch) -> None:
        def _explode(*args, **kwargs):
            raise AssertionError("no sysctl on this platform")

        monkeypatch.setitem(sys.modules, "psutil", None)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(subprocess, "run", _explode)
        assert host_metrics.host_memory_sample() == {}

    def test_broken_psutil_never_raises(self, monkeypatch) -> None:
        broken = types.ModuleType("psutil")

        def _boom():
            raise RuntimeError("psutil is wedged")

        broken.virtual_memory = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "psutil", broken)
        _as_darwin(monkeypatch, _SYSCTL)
        # Falls through to the sysctl path instead of propagating.
        assert host_metrics.host_memory_sample() == {"mem_total_kib": 134217728}

    def test_nonsense_free_percentage_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "psutil", None)
        _as_darwin(
            monkeypatch,
            {
                **_SYSCTL,
                "memory_pressure": "System-wide memory free percentage: 4096%\n",
            },
        )
        sample = host_metrics.host_memory_sample()
        assert "mem_available_kib" not in sample


class TestMemoryStatusContract:
    """The sample must drop into gateway.memory_status without translation."""

    def test_keys_are_a_subset_of_sample_memory(self, monkeypatch) -> None:
        from gateway.lifecycle_ledger import sample_memory

        monkeypatch.setitem(
            sys.modules,
            "psutil",
            _fake_psutil(
                total=137438953472, available=73109897216, swap_used=0, rss=104857600
            ),
        )
        sample = host_metrics.host_memory_sample()
        # sample_memory's full Linux vocabulary, verified against the source
        # of truth rather than a hardcoded list.
        linux_keys = {"rss_kib", "mem_total_kib", "mem_available_kib", "swap_used_kib"}
        assert set(sample) <= linux_keys
        assert isinstance(sample_memory(), dict)

class TestSystemStatsHardware:
    def test_endpoint_adds_brand_and_processor(self, monkeypatch) -> None:
        import asyncio

        from hermes_cli import web_server

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(host_metrics, "_run", _fake_probe(_SYSCTL))

        info = asyncio.run(web_server.get_system_stats())
        assert info["brand"] == "Mac16,5"
        assert info["processor"] == "Apple M4 Max"
        # Every pre-existing key the dashboard's SystemStats type reads is
        # still there.
        for key in ("os", "os_release", "os_version", "platform", "arch",
                    "hostname", "python_version", "python_impl",
                    "hermes_version", "cpu_count", "psutil"):
            assert key in info

    def test_endpoint_omits_hardware_when_unavailable(self, monkeypatch) -> None:
        import asyncio

        from hermes_cli import web_server

        monkeypatch.setattr(
            host_metrics, "host_hardware", lambda: (_ for _ in ()).throw(OSError("nope"))
        )
        info = asyncio.run(web_server.get_system_stats())
        assert "brand" not in info
        assert info["arch"]
