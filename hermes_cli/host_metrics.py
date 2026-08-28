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
