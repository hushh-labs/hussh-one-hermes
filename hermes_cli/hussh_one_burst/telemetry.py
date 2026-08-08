# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Live device measurement — the I/O boundary for placement.

Everything that reads the real machine lives here, and nothing else in this
package does any I/O.  That split is deliberate: ``placement.py`` stays pure and
testable, and this module is the single place where a probe can be slow, absent,
or wrong.

**No probe may raise.**  A machine without ``nvidia-smi``, a Linux kernel with no
thermal zones, a locked-down sandbox where ``sysctl`` is missing — each of those
is a normal machine, not an error.  Every probe returns ``None`` on failure and
the caller degrades to a conservative decision.

**Nothing here touches the network.**  Reachability is inferred from local
interface state, never by contacting a host, because a burst decision must not
itself leak that the person is about to run something.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal, Optional

from .types import DeviceProfile

#: Probes shell out at most this long.  A slow probe is a failed probe — the
#: point of local placement is that it costs nothing to decide.
_PROBE_TIMEOUT_S = 2.0

_GB = 1024.0**3

#: How accelerator memory relates to host memory on this machine.
#:
#: ``unified`` — Apple Silicon: one pool, GPU and CPU share it.
#: ``discrete`` — a separate card with its own VRAM.
#: ``none`` — no usable accelerator found.
MemoryModel = Literal["unified", "discrete", "none"]


def _run(args: list[str]) -> Optional[str]:
    """Run a probe, returning stdout, or ``None`` for any failure whatsoever."""
    if not args or shutil.which(args[0]) is None:
        return None
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


@dataclass(frozen=True)
class GpuInfo:
    """A measured accelerator.  ``vram_gb`` is ``None`` when it could not be read."""

    name: str
    vram_gb: Optional[float]
    memory_model: MemoryModel
    cores: int = 0


@dataclass(frozen=True)
class MeasuredDevice:
    """What this machine actually looks like right now.

    Distinct from :class:`~.types.DeviceProfile`, which is a *catalog* entry.
    This is measured; that is assumed.  ``to_profile`` converts.
    """

    label: str
    cpu_cores: int
    cpu_load_pct: Optional[float]
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float
    online: bool
    gpu: Optional[GpuInfo] = None
    battery_pct: Optional[int] = None
    on_ac_power: Optional[bool] = None
    cpu_freq_ratio: Optional[float] = None
    """Current CPU frequency over maximum.  Well under 1.0 suggests throttling."""

    max_temp_c: Optional[float] = None

    @property
    def throttled(self) -> Optional[bool]:
        """Whether the machine appears to be running below its rated speed.

        ``None`` when neither frequency nor temperature could be read — unknown
        is not the same as healthy, and callers must be able to tell them apart.
        """
        if self.cpu_freq_ratio is None and self.max_temp_c is None:
            return None
        if self.cpu_freq_ratio is not None and self.cpu_freq_ratio < 0.75:
            return True
        if self.max_temp_c is not None and self.max_temp_c >= 90.0:
            return True
        return False

    @property
    def on_battery(self) -> bool:
        """True only when we positively know the machine is unplugged."""
        return self.on_ac_power is False

    def to_profile(self) -> DeviceProfile:
        """Convert to the catalog shape the pure placement engine consumes.

        Two deliberate choices:

        * **Available RAM, not total.**  Placement asks "does this fit *now*",
          and the browser tabs already open are not headroom.
        * **VRAM is carried separately** when the accelerator has its own pool,
          so the engine can gate accelerator need on VRAM and host need on RAM
          instead of conflating them.  On unified memory both come from the one
          pool and ``vram_gb`` stays ``None``, which is exactly the original
          Apple-Silicon behaviour.
        """
        vram_gb: Optional[float] = None
        if self.gpu is not None and self.gpu.memory_model == "discrete":
            vram_gb = self.gpu.vram_gb
        return DeviceProfile(
            id="measured",
            label=self.label,
            cpu_cores=self.cpu_cores,
            gpu_cores=self.gpu.cores if self.gpu else 0,
            unified_memory_gb=round(self.ram_available_gb, 1),
            disk_free_gb=round(self.disk_free_gb, 1),
            network_mbps=0.0,
            online=self.online,
            vram_gb=round(vram_gb, 1) if vram_gb is not None else None,
        )


def _detect_nvidia() -> Optional[GpuInfo]:
    """Read the first NVIDIA card via ``nvidia-smi``, if one is present."""
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not out:
        return None
    first = out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2:
        return None
    try:
        # nvidia-smi reports MiB with --nounits.
        vram_gb = float(parts[1]) / 1024.0
    except ValueError:
        return None
    return GpuInfo(name=parts[0], vram_gb=round(vram_gb, 1), memory_model="discrete")


def _detect_apple_gpu(ram_total_gb: float) -> Optional[GpuInfo]:
    """Read the Apple Silicon integrated GPU.  Its memory *is* system memory."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    name = "Apple Silicon GPU"
    cores = 0
    raw = _run(["system_profiler", "-json", "SPDisplaysDataType"])
    if raw:
        try:
            blocks = json.loads(raw).get("SPDisplaysDataType") or []
            if blocks:
                name = blocks[0].get("_name") or name
                cores = int(blocks[0].get("sppci_cores") or 0)
        except (ValueError, TypeError, AttributeError):
            pass
    return GpuInfo(
        name=name,
        vram_gb=round(ram_total_gb, 1),
        memory_model="unified",
        cores=cores,
    )


def _detect_gpu(ram_total_gb: float) -> Optional[GpuInfo]:
    """Best available accelerator, preferring a discrete card over integrated."""
    return _detect_nvidia() or _detect_apple_gpu(ram_total_gb)


def _detect_power() -> tuple[Optional[int], Optional[bool]]:
    """Charge percent and AC state, reusing Hermes' own battery reader."""
    try:
        from agent.battery import read_battery  # type: ignore[import-not-found]

        status = read_battery()
        return getattr(status, "percent", None), getattr(status, "plugged", None)
    except Exception:
        pass
    try:
        import psutil

        reader = getattr(psutil, "sensors_battery", None)
        batt = reader() if reader else None
        if batt is None:
            # A desktop with no battery is permanently on AC, not unknown.
            return None, True
        pct = getattr(batt, "percent", None)
        return (
            int(round(pct)) if pct is not None else None,
            getattr(batt, "power_plugged", None),
        )
    except Exception:
        return None, None


def _detect_thermal() -> tuple[Optional[float], Optional[float]]:
    """Return ``(cpu_freq_ratio, max_temp_c)``; either may be ``None``."""
    ratio: Optional[float] = None
    max_temp: Optional[float] = None
    try:
        import psutil

        try:
            freq = psutil.cpu_freq()
            if freq and getattr(freq, "max", 0):
                ratio = round(float(freq.current) / float(freq.max), 3)
        except Exception:
            pass
        try:
            reader = getattr(psutil, "sensors_temperatures", None)
            temps = reader() if reader else None
            readings = [
                entry.current
                for entries in (temps or {}).values()
                for entry in entries
                if getattr(entry, "current", None) is not None
            ]
            if readings:
                max_temp = round(max(readings), 1)
        except Exception:
            pass
    except Exception:
        return None, None
    return ratio, max_temp


def _detect_online() -> bool:
    """Infer reachability from local interface state — never by contacting a host.

    A burst decision must not announce itself.  This reads link state only, so a
    machine on a captive portal reads as online; that is the right trade, because
    the alternative leaks intent to decide.
    """
    try:
        import psutil

        stats = psutil.net_if_stats()
    except Exception:
        return True  # Unknown link state: assume online, let the burst fail loudly.
    for name, st in stats.items():
        lowered = name.lower()
        if lowered.startswith(("lo", "loopback")):
            continue
        if getattr(st, "isup", False):
            return True
    return False


def measure_device(disk_path: str = "/") -> MeasuredDevice:
    """Measure this machine.  Never raises; degrades to conservative numbers.

    When ``psutil`` is somehow unavailable the result reports zero memory and
    zero disk, which the placement engine reads as "nothing fits" and bursts —
    the safe direction to fail in.
    """
    label = f"{platform.system()} {platform.machine()}".strip() or "this device"
    cpu_cores = 0
    cpu_load: Optional[float] = None
    ram_total = ram_avail = disk_free = 0.0

    try:
        import psutil

        cpu_cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 0
        try:
            # Non-blocking: the value since the previous call.  A blocking
            # sample would make placement cost a second of wall-clock.
            cpu_load = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_load = None
        mem = psutil.virtual_memory()
        ram_total = mem.total / _GB
        ram_avail = mem.available / _GB
        try:
            disk_free = psutil.disk_usage(disk_path).free / _GB
        except Exception:
            disk_free = 0.0
    except Exception:
        pass

    battery_pct, on_ac = _detect_power()
    freq_ratio, max_temp = _detect_thermal()

    return MeasuredDevice(
        label=label,
        cpu_cores=cpu_cores,
        cpu_load_pct=cpu_load,
        ram_total_gb=round(ram_total, 2),
        ram_available_gb=round(ram_avail, 2),
        disk_free_gb=round(disk_free, 2),
        online=_detect_online(),
        gpu=_detect_gpu(ram_total),
        battery_pct=battery_pct,
        on_ac_power=on_ac,
        cpu_freq_ratio=freq_ratio,
        max_temp_c=max_temp,
    )
