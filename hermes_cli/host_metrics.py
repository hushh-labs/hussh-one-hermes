"""Real host hardware + memory metrics, for hosts without ``/proc``.

Two Linux-shaped blind spots meet here:

* ``/api/system/stats`` names the OS, the arch and the core count from
  stdlib, but nothing that identifies the *machine*.  ``platform.processor()``
  returns the bare arch (``"arm64"``) on macOS, so an M4 Max and a 2020 M1
  Air render identically on the System page.  :func:`host_hardware` reads
  the values that actually distinguish them (``hw.model`` /
  ``machdep.cpu.brand_string``).
* :func:`gateway.lifecycle_ledger.sample_memory` — the source of every
  number in the ``/api/status`` memory rollup — is pure ``/proc``, so it
  returns ``{}`` on macOS and the whole block degrades to ``pressure:
  "unknown"`` with null numbers on an otherwise healthy 128 GB host.
  :func:`host_memory_sample` answers the same question with the same keys
  and the same units, so a caller can consume either without branching.

Everything here is best-effort and read-only: every function degrades to a
partial dict (or ``{}``) rather than raising, and no function shells out on
a platform where the command does not exist.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# These probes are single reads of kernel state — a host that cannot answer
# in three seconds is wedged, and the System page would rather render
# without the field than block on it.
_PROBE_TIMEOUT_S = 3

_BYTES_PER_GIB = 1024**3
_BYTES_PER_KIB = 1024
_KIB_PER_GIB = 1024 * 1024

# Overridable so the Linux path stays testable from a macOS dev host (and
# vice versa); nothing outside tests should reassign these.
_PROC_CPUINFO = "/proc/cpuinfo"
_PROC_MEMINFO = "/proc/meminfo"

# ``memory_pressure`` ends its report with a single summary line:
#   "System-wide memory free percentage: 88%"
_FREE_PERCENT_RE = re.compile(r"free percentage:\s*(\d+)\s*%", re.IGNORECASE)


def _run(cmd: List[str], timeout: int = _PROBE_TIMEOUT_S) -> str:
    """Stripped stdout of ``cmd``, or ``""`` for *any* failure.

    The ``shutil.which`` probe is the "never shell out where the command
    does not exist" guard — it also keeps a missing binary from costing a
    process spawn on every call.
    """
    if not cmd or not shutil.which(cmd[0]):
        return ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _sysctl(name: str) -> str:
    """``sysctl -n <name>``, empty off macOS.

    Linux ships a ``sysctl`` too, but none of the OIDs below exist there —
    the Linux answers come from ``/proc``, so gate rather than probe.
    """
    if sys.platform != "darwin":
        return ""
    return _run(["sysctl", "-n", name])


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (AttributeError, ValueError):
        return None


def _darwin_hardware() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    brand = _sysctl("hw.model")
    if brand:
        info["brand"] = brand
    processor = _sysctl("machdep.cpu.brand_string")
    if processor:
        info["processor"] = processor
    cores = _to_int(_sysctl("hw.ncpu"))
    if cores:
        info["cpu_cores"] = cores
    mem_bytes = _to_int(_sysctl("hw.memsize"))
    if mem_bytes:
        info["ram_total_gb"] = round(mem_bytes / _BYTES_PER_GIB, 1)
    return info


def _proc_field(path: str, prefix: str) -> str:
    """First ``<prefix>...: <value>`` line's value in a ``/proc`` file."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(prefix):
                    return line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        return ""
    return ""


def _linux_hardware() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    # No Linux equivalent of hw.model is reliably readable without root
    # (DMI is often absent in containers), so "brand" stays unset here
    # rather than being filled with a guess.
    processor = _proc_field(_PROC_CPUINFO, "model name")
    if processor:
        info["processor"] = processor
    # MemTotal's value carries its unit ("32768000 kB"); take the number.
    mem_total = _proc_field(_PROC_MEMINFO, "MemTotal").split()
    total_kib = _to_int(mem_total[0]) if mem_total else None
    if total_kib:
        info["ram_total_gb"] = round(total_kib / _KIB_PER_GIB, 1)
    return info


def host_hardware() -> Dict[str, Any]:
    """Static host identity: ``brand``, ``processor``, ``cpu_cores``, ``ram_total_gb``.

    Deliberately partial: a key is present only when *this* platform can
    answer it honestly (Linux exposes no hardware brand the way ``hw.model``
    does), and a probe that fails drops its key rather than guessing.  An
    unrecognised platform still gets ``cpu_cores`` from stdlib.
    """
    info: Dict[str, Any] = {}
    try:
        if sys.platform == "darwin":
            info.update(_darwin_hardware())
        elif sys.platform.startswith("linux"):
            info.update(_linux_hardware())
        if "cpu_cores" not in info:
            cores = os.cpu_count()
            if cores:
                info["cpu_cores"] = cores
    except Exception:
        logger.debug("host_hardware probe failed", exc_info=True)
    return info


def _psutil_memory_sample() -> Dict[str, Any]:
    """psutil's answer, in :func:`sample_memory`'s keys and KiB units."""
    try:
        import psutil  # type: ignore
    except Exception:
        return {}
    sample: Dict[str, Any] = {}
    try:
        vm = psutil.virtual_memory()
        sample["mem_total_kib"] = int(vm.total) // _BYTES_PER_KIB
        sample["mem_available_kib"] = int(vm.available) // _BYTES_PER_KIB
    except Exception:
        # Without a total/available pair there is nothing to classify, so
        # an own-RSS-only sample would be worse than none.
        return {}
    try:
        sample["swap_used_kib"] = int(psutil.swap_memory().used) // _BYTES_PER_KIB
    except Exception:
        pass
    try:
        sample["rss_kib"] = int(psutil.Process().memory_info().rss) // _BYTES_PER_KIB
    except Exception:
        pass
    return sample


def _memory_pressure_free_percent() -> Optional[int]:
    """Whole-percent free memory from ``memory_pressure``'s summary line."""
    match = _FREE_PERCENT_RE.search(_run(["memory_pressure"]))
    if not match:
        return None
    percent = _to_int(match.group(1))
    if percent is None or not 0 <= percent <= 100:
        return None
    return percent


def _sysctl_memory_sample() -> Dict[str, Any]:
    """Last-resort macOS sample: installed RAM plus the kernel's own
    free-percentage verdict.  Coarser than psutil (whole percent, no swap,
    no RSS) but it needs nothing installed."""
    total_bytes = _to_int(_sysctl("hw.memsize"))
    if not total_bytes:
        return {}
    total_kib = total_bytes // _BYTES_PER_KIB
    sample: Dict[str, Any] = {"mem_total_kib": total_kib}
    percent = _memory_pressure_free_percent()
    if percent is not None:
        sample["mem_available_kib"] = total_kib * percent // 100
    return sample


def host_memory_sample() -> Dict[str, Any]:
    """A live memory snapshot in :func:`gateway.lifecycle_ledger.sample_memory`'s shape.

    Same key names and the same KiB units — ``rss_kib`` (this process),
    ``mem_total_kib``, ``mem_available_kib``, ``swap_used_kib`` — so
    :mod:`gateway.memory_status` can consume either source through the same
    code path.  Keys the host cannot answer are omitted, exactly as
    ``sample_memory`` omits what ``/proc`` did not yield; ``{}`` when
    nothing works.
    """
    try:
        sample = _psutil_memory_sample()
        if sample:
            return sample
        return _sysctl_memory_sample()
    except Exception:
        logger.debug("host_memory_sample probe failed", exc_info=True)
        return {}


# `pmset -g batt` prints one row per battery:
#   -InternalBattery-0 (id=7602275)	27%; discharging; 1:35 remaining present: true
_BATT_PERCENT_RE = re.compile(r"(\d{1,3})%")
_BATT_TIME_RE = re.compile(r"(\d+):(\d{2})\s+remaining")
# "AC attached" shows while plugged in but not taking a charge (already full, or
# held at a charge limit). That is not "charging", and reporting it as charging
# would tell the owner power is coming in when it is not.
_BATT_STATES = ("finishing charge", "discharging", "charging", "charged", "AC attached")
_BATT_CHARGING_STATES = frozenset({"charging", "finishing charge"})


def host_battery() -> Dict[str, Any]:
    """Battery state, or an explicit "no battery" for a desktop.

    A Mac Studio has no battery. Reporting ``0%`` for one would be a false
    reading rather than a missing one, and nothing downstream could tell those
    apart afterwards. So absence is ``{"present": False}`` with no percentage at
    all, and callers must handle that rather than defaulting it to a number.

    This is more than a nicety for an edge tier. A 31B model on a laptop at 27%
    and discharging is a materially different machine from the same laptop on
    mains, and the difference surfaces as thermal throttling and a dead session
    rather than as any error a reader could trace back.
    """
    try:
        if sys.platform == "darwin":
            darwin = _darwin_battery()
            if darwin:
                return darwin
        return _psutil_battery()
    except Exception:
        logger.debug("battery probe failed", exc_info=True)
        return {}


def _darwin_battery() -> Dict[str, Any]:
    output = _run(["pmset", "-g", "batt"])
    if not output:
        return {}
    # A desktop prints the AC line and no battery row at all.
    if "InternalBattery" not in output and "present: true" not in output:
        return {"present": False}

    battery: Dict[str, Any] = {"present": True}
    percent_match = _BATT_PERCENT_RE.search(output)
    if percent_match:
        percent = int(percent_match.group(1))
        if 0 <= percent <= 100:
            battery["percent"] = percent

    # Parse the state as a field, never as a substring of the whole output.
    # `pmset` prints `80%; AC attached; not charging`, and searching for
    # "charging" anywhere finds it inside "not charging" -- reporting a machine
    # held at a charge limit as actively charging.
    state = ""
    for line in output.splitlines():
        if "%" not in line:
            continue
        fields = [part.strip() for part in line.split(";")]
        if len(fields) >= 2 and fields[1]:
            candidate = fields[1].casefold()
            for known in _BATT_STATES:
                if candidate == known.casefold():
                    state = known
                    break
            if not state:
                state = fields[1]
        break
    if state:
        battery["state"] = state
    # Both flags derive from the one state string, so a single snapshot can
    # never report itself as charging and discharging at once.
    battery["charging"] = state in _BATT_CHARGING_STATES
    battery["on_ac"] = bool(state) and state != "discharging"

    time_match = _BATT_TIME_RE.search(output)
    if time_match:
        minutes = int(time_match.group(1)) * 60 + int(time_match.group(2))
        # macOS prints 0:00 while still calculating an estimate. Reporting
        # "0 minutes remaining" would read as an imminent shutdown.
        if minutes > 0:
            battery["minutes_remaining"] = minutes
    return battery


def _psutil_battery() -> Dict[str, Any]:
    try:
        import psutil
    except Exception:
        return {}
    sensors = getattr(psutil, "sensors_battery", None)
    if sensors is None:
        return {}
    reading = sensors()
    if reading is None:
        # psutil returns None for a machine with no battery.
        return {"present": False}

    plugged = bool(getattr(reading, "power_plugged", False))
    battery: Dict[str, Any] = {
        "present": True,
        "charging": plugged,
        "on_ac": plugged,
        "state": "charging" if plugged else "discharging",
    }
    percent = getattr(reading, "percent", None)
    if isinstance(percent, (int, float)) and 0 <= percent <= 100:
        battery["percent"] = round(float(percent))
    seconds = getattr(reading, "secsleft", None)
    # psutil uses negative sentinels for "unlimited" and "unknown".
    if isinstance(seconds, int) and seconds > 0:
        battery["minutes_remaining"] = seconds // 60
    return battery
