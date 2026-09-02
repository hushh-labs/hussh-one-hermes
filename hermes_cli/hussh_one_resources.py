# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""What the owner of a Puppy One machine actually needs to see.

A resource monitor for an edge tier is not a CPU graph. The questions that
change what the owner does are narrow, and each one has been a real incident
on this machine:

* **Is it answering here?** The provider pin covers the main turn; the
  ``hussh_one.on_device_only`` gate is what stops an auxiliary task reaching a
  vendor. Reporting the model without the gate once let a PKM save think on
  Gemini while the config said otherwise.
* **Is there room?** Models are tens of gigabytes and eviction is deliberately
  conservative, so "resident 61 GB of 128 GB" is the number that predicts
  whether the next model load succeeds or drives the host into swap.
* **Will it survive tonight?** A laptop at 27% and discharging runs the same
  jobs as one on mains, and fails them by thermal throttling rather than by
  any error a reader could trace. Disk matters the same way: the vault, the
  encrypted replica and every session live on it.
* **Is the work landing?** The scheduled jobs are the product. Next run and
  the last day's outcomes say whether the agent is doing its job at all.

Every probe is bounded and independently fallible: a section that cannot be
answered is **omitted**, never guessed or zero-filled. A zero here would read
as a measurement, and the whole point is that a reading nobody took must be
distinguishable from one that came back bad.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Providers that answer on this machine. Everything else leaves it.
LOCAL_PROVIDERS = frozenset({"lmstudio", "lm-studio", "lm_studio", "ollama"})

_BYTES_PER_GB = 1024.0**3


def _safe(section: str, probe: Callable[[], Any], default: Any = None) -> Any:
    """Run one probe. A failure drops its section rather than the snapshot."""
    try:
        return probe()
    except Exception:  # noqa: BLE001 - a monitor must never be the outage
        logger.debug("resource probe failed: %s", section, exc_info=True)
        return default


def _round(value: Any, digits: int = 1) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def agent_section(config: Optional[dict] = None) -> Dict[str, Any]:
    """The model answering, where it runs, and whether the gate is closed."""
    from hermes_cli.config import load_config

    cfg = config if isinstance(config, dict) else load_config()
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider = str(model_cfg.get("provider") or "").strip()
    model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    hussh_one = cfg.get("hussh_one") if isinstance(cfg.get("hussh_one"), dict) else {}
    return {
        "model": model or None,
        "provider": provider or None,
        "on_device": provider.lower() in LOCAL_PROVIDERS,
        # The gate is the claim, not the pin. Report it plainly: "on-device"
        # with the gate off means only that this turn is local.
        "on_device_gate": bool(hussh_one.get("on_device_only")),
    }


def machine_section() -> Dict[str, Any]:
    """Host identity, memory pressure, disk headroom and power."""
    from hermes_cli import hussh_one_host_metrics as host

    out: Dict[str, Any] = {}
    hardware = _safe("hardware", host.host_hardware, {}) or {}
    for key in ("brand", "processor", "cpu_cores", "ram_total_gb"):
        if hardware.get(key) is not None:
            out[key] = hardware[key]

    sample = _safe("memory", host.host_memory_sample, {}) or {}
    total_kib = sample.get("mem_total_kib")
    available_kib = sample.get("mem_available_kib")
    if total_kib and available_kib is not None:
        total_gb = float(total_kib) / (1024.0**2)
        available_gb = float(available_kib) / (1024.0**2)
        out["ram_total_gb"] = _round(total_gb)
        out["ram_available_gb"] = _round(available_gb)
        if total_gb > 0:
            out["ram_used_pct"] = _round(100.0 * (1.0 - available_gb / total_gb))

    battery = _safe("battery", host.host_battery, {}) or {}
    if battery:
        # present=False is a real answer (a desktop), not a missing one.
        out["battery"] = battery

    def _disk() -> Dict[str, Any]:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        usage = shutil.disk_usage(home if home.exists() else Path.home())
        return {
            "disk_total_gb": _round(usage.total / _BYTES_PER_GB),
            "disk_free_gb": _round(usage.free / _BYTES_PER_GB),
            "disk_used_pct": _round(100.0 * (usage.total - usage.free) / usage.total),
        }

    out.update(_safe("disk", _disk, {}) or {})
    return out


def models_section(current_model: Optional[str] = None) -> Dict[str, Any]:
    """Resident models and the headroom left for another one.

    ``available_gb`` comes from the same conservative source eviction uses, so
    the number the owner reads is the number that decides whether a load fits.
    """
    from hermes_cli import hussh_one_lmstudio as lms

    resident: List[dict] = _safe("resident", lms.loaded_models, []) or []
    rows = []
    total_gb = 0.0
    for entry in resident:
        size = _round(entry.get("size_gb")) or 0.0
        total_gb += float(size or 0.0)
        rows.append(
            {
                "id": entry.get("identifier") or entry.get("model"),
                "size_gb": size,
                "status": entry.get("status") or None,
                "context": entry.get("context") or None,
                "is_current": bool(
                    current_model and str(entry.get("identifier") or "") == str(current_model)
                ),
            }
        )
    section: Dict[str, Any] = {"resident": rows, "resident_gb": _round(total_gb)}
    memory = _safe("lms_memory", lms.host_memory, {}) or {}
    if memory.get("available_gb") is not None:
        section["available_gb"] = _round(memory["available_gb"])
    return section


def jobs_section(
    *,
    jobs_path: Optional[Path] = None,
    executions_db: Optional[Path] = None,
    now: Optional[float] = None,
    window_hours: int = 24,
) -> Dict[str, Any]:
    """The scheduled work: what runs next, and how the last day went.

    Disabled jobs are counted separately rather than dropped. The owner
    disables jobs on purpose, and a monitor that hides them cannot explain why
    a job it once showed no longer runs.
    """
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    jobs_file = Path(jobs_path) if jobs_path else home / "cron" / "jobs.json"
    db_file = Path(executions_db) if executions_db else home / "cron" / "executions.db"
    moment = now if now is not None else time.time()

    def _jobs() -> Dict[str, Any]:
        payload = json.loads(jobs_file.read_text(encoding="utf-8"))
        rows = payload.get("jobs") if isinstance(payload, dict) else payload
        enabled = [j for j in (rows or []) if j.get("state") != "paused" and j.get("enabled", True)]
        disabled = len(rows or []) - len(enabled)
        upcoming = [
            (str(j.get("next_run_at") or ""), str(j.get("name") or j.get("id") or ""))
            for j in enabled
            if j.get("next_run_at")
        ]
        upcoming.sort()
        out: Dict[str, Any] = {"enabled": len(enabled), "disabled": max(0, disabled)}
        if upcoming:
            out["next"] = {"at": upcoming[0][0], "name": upcoming[0][1]}
        return out

    section = _safe("jobs", _jobs, {}) or {}

    def _recent() -> Dict[str, int]:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(moment - window_hours * 3600)
            )
            counts = {"completed": 0, "failed": 0, "other": 0}
            for status, count in connection.execute(
                "select status, count(*) from executions where claimed_at >= ? group by status",
                (cutoff,),
            ):
                key = str(status or "").lower()
                if key in ("completed", "success"):
                    counts["completed"] += int(count)
                elif key in ("failed", "error"):
                    counts["failed"] += int(count)
                else:
                    counts["other"] += int(count)
            return counts
        finally:
            connection.close()

    recent = _safe("executions", _recent, None)
    if recent is not None:
        section[f"last_{window_hours}h"] = recent
    return section


def collect_resources(
    *,
    config: Optional[dict] = None,
    active_agents: int = 0,
    busy: bool = False,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """One bounded snapshot of this Puppy One machine."""
    agent = _safe("agent", lambda: agent_section(config), {}) or {}
    agent["active_agents"] = max(0, int(active_agents or 0))
    agent["busy"] = bool(busy)
    if version:
        agent["version"] = version
    return {
        "generated_at": int(time.time() * 1000),
        "agent": agent,
        "machine": _safe("machine", machine_section, {}) or {},
        "models": _safe("models", lambda: models_section(agent.get("model")), {}) or {},
        "jobs": _safe("jobs_section", jobs_section, {}) or {},
    }
