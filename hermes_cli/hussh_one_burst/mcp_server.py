# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Local MCP surface for Xtreme Burst Compute.

This is how burst becomes reachable — by a person through Hermes, and by an
agent runtime that lives somewhere else entirely.  It follows the same shape as
``hussh_one_pkm.mcp_server``: a local FastMCP server, registered in
``mcp_config.py``, offering read-only judgement tools plus one gated action.

Two properties this surface must never lose:

* **The decision stays local.**  ``decide`` and ``plan`` measure this machine and
  answer from numbers.  No workload contents, filenames, or prompts cross this
  boundary — the tools do not accept them, so they cannot leak them.
* **Nothing runs without an explicit yes.**  ``run`` elicits approval through
  Hermes' own MCP elicitation, showing the cost and the destination project
  before a single instance is provisioned.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from typing import Any, Optional

from pydantic import BaseModel, Field

from .devices import WORKLOAD_PRESETS, find_workload_preset
from .hardware import benchmark_hardware, recommend_hardware
from .placement import decide_placement
from .telemetry import measure_device
from .types import AcceleratorKind, WorkloadEstimate

try:
    from mcp.server.fastmcp import Context
except ImportError:  # pragma: no cover - optional MCP extra
    Context = Any  # type: ignore[misc,assignment]

_logger = logging.getLogger(__name__)


class BurstApproval(BaseModel):
    """Schema rendered by Hermes' existing MCP form-elicitation handler."""

    confirm: bool = Field(
        default=True,
        description="Approve provisioning this hardware in your own cloud project.",
    )


def _estimate_from(
    preset_id: Optional[str],
    vram_gb: Optional[float],
    memory_gb: Optional[float],
    disk_gb: Optional[float],
    minutes: Optional[float],
) -> tuple[WorkloadEstimate, AcceleratorKind, int, float, str]:
    """Resolve a preset id or explicit numbers into a workload estimate.

    Returns ``(estimate, kind, parallel_chips, runtime_min, label)``.
    """
    if preset_id:
        preset = find_workload_preset(preset_id)
        if preset is None:
            known = ", ".join(p.id for p in WORKLOAD_PRESETS)
            raise ValueError(f"Unknown preset '{preset_id}'. Known presets: {known}.")
        return (
            preset.estimate,
            preset.accelerator_kind,
            preset.parallel_chips,
            preset.matched_runtime_min,
            preset.title,
        )
    if vram_gb is None:
        raise ValueError("Provide either a preset_id or an accelerator memory estimate.")
    estimate = WorkloadEstimate(
        vram_gb=float(vram_gb),
        unified_memory_gb=float(memory_gb if memory_gb is not None else vram_gb),
        vcpus=0.0,
        disk_gb=float(disk_gb or 0.0),
        estimated_minutes=float(minutes or 0.0),
    )
    return estimate, "gpu", 1, float(minutes or 0.0), "this workload"


def _device_view() -> dict[str, Any]:
    device = measure_device()
    return {
        "label": device.label,
        "cpu_cores": device.cpu_cores,
        "cpu_load_pct": device.cpu_load_pct,
        "ram_total_gb": device.ram_total_gb,
        "ram_available_gb": device.ram_available_gb,
        "disk_free_gb": device.disk_free_gb,
        "accelerator": (
            {
                "name": device.gpu.name,
                "vram_gb": device.gpu.vram_gb,
                "memory_model": device.gpu.memory_model,
            }
            if device.gpu
            else None
        ),
        "online": device.online,
        "battery_pct": device.battery_pct,
        "on_ac_power": device.on_ac_power,
        "throttled": device.throttled,
    }


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise ImportError("Xtreme Burst requires the Hermes MCP extra.") from exc

    mcp = FastMCP(
        "hussh-one-burst",
        instructions=(
            "Use this local bridge to decide whether a compute workload should run "
            "on this device or burst to the person's own cloud. Placement is decided "
            "from measured resource numbers only — never send workload contents, "
            "file paths, prompts, or record data through these tools. Always show "
            "the person the plan and its cost before calling hussh_burst_run."
        ),
    )

    @mcp.tool(
        name="hussh_burst_device_status",
        description=(
            "Measure this machine right now: cores, available memory, free disk, "
            "accelerator and its memory, power state, and whether it appears "
            "thermally throttled. Numbers only — no workload information is read."
        ),
    )
    def device_status() -> dict[str, Any]:
        return _device_view()

    @mcp.tool(
        name="hussh_burst_list_presets",
        description=(
            "List the ready-made workloads a person can ask for, with the "
            "accelerator memory each needs."
        ),
    )
    def list_presets() -> dict[str, Any]:
        return {
            "presets": [
                {
                    "id": p.id,
                    "title": p.title,
                    "subtitle": p.subtitle,
                    "accelerator_kind": p.accelerator_kind,
                    "vram_gb": p.estimate.vram_gb,
                    "parallel_chips": p.parallel_chips,
                }
                for p in WORKLOAD_PRESETS
            ]
        }

    @mcp.tool(
        name="hussh_burst_decide",
        description=(
            "Decide whether a workload runs on this device or bursts to the "
            "person's cloud, measuring the machine first. Pass a preset_id, or "
            "vram_gb for a custom workload. Returns the decision and the reason "
            "in the person's own terms."
        ),
    )
    def decide(
        preset_id: Optional[str] = None,
        vram_gb: Optional[float] = None,
        memory_gb: Optional[float] = None,
        disk_gb: Optional[float] = None,
        minutes: Optional[float] = None,
    ) -> dict[str, Any]:
        estimate, kind, _chips, _runtime, label = _estimate_from(
            preset_id, vram_gb, memory_gb, disk_gb, minutes
        )
        device = measure_device()
        decision = decide_placement(estimate, device.to_profile(), kind)
        result: dict[str, Any] = {
            "workload": label,
            "target": decision.target,
            "reason": decision.reason,
            "fits_locally": decision.fits_locally,
            "measured_device": _device_view(),
        }
        if decision.headroom is not None:
            result["headroom"] = dataclasses.asdict(decision.headroom)
        if device.on_battery and decision.target == "device":
            result["advisory"] = (
                "This machine is on battery. Running locally will drain it — "
                "offer the cloud as an alternative."
            )
        if device.throttled and decision.target == "device":
            result["advisory"] = (
                "This machine appears thermally throttled, so a local run may take "
                "considerably longer than the estimate."
            )
        return result

    @mcp.tool(
        name="hussh_burst_plan",
        description=(
            "Produce the full plan a person sees before anything runs: the "
            "placement decision, the matched accelerator, the hourly and total "
            "cost, and a comparison against undersized and oversized hardware."
        ),
    )
    def plan(
        preset_id: Optional[str] = None,
        vram_gb: Optional[float] = None,
        memory_gb: Optional[float] = None,
        disk_gb: Optional[float] = None,
        minutes: Optional[float] = None,
    ) -> dict[str, Any]:
        estimate, kind, chips, runtime_min, label = _estimate_from(
            preset_id, vram_gb, memory_gb, disk_gb, minutes
        )
        device = measure_device()
        decision = decide_placement(estimate, device.to_profile(), kind)
        rec = recommend_hardware(estimate.vram_gb, kind, chips)
        rows = benchmark_hardware(estimate.vram_gb, kind, chips, runtime_min)
        total = round(rec.usd_per_hour * (runtime_min / 60.0), 2) if runtime_min else None
        return {
            "workload": label,
            "target": decision.target,
            "reason": decision.reason,
            "measured_device": _device_view(),
            "recommended": {
                "accelerator": rec.accel.label,
                "count": rec.count,
                "usd_per_hour": rec.usd_per_hour,
                "fits": rec.fits,
                "rationale": rec.rationale,
            },
            "estimated_total_usd": total,
            "estimated_minutes": runtime_min or None,
            "comparison": [dataclasses.asdict(r) for r in rows],
            "cost_basis": (
                "Modeled on-demand us-central1 rates. Treat as an estimate, not a quote."
            ),
        }

    @mcp.tool(
        name="hussh_burst_run",
        description=(
            "Provision the recommended hardware in the person's own cloud project "
            "and run the workload. Shows cost, hardware and destination project, "
            "then proceeds only if the person accepts. Teardown is guaranteed."
        ),
    )
    async def run(
        ctx: Context,
        preset_id: Optional[str] = None,
        vram_gb: Optional[float] = None,
        minutes: Optional[float] = None,
        project: Optional[str] = None,
        provider: str = "mock",
    ) -> dict[str, Any]:
        from .execution import BurstRequest, run_burst
        from .providers import resolve_provider

        estimate, kind, chips, runtime_min, label = _estimate_from(
            preset_id, vram_gb, None, None, minutes
        )
        rec = recommend_hardware(estimate.vram_gb, kind, chips)
        total = round(rec.usd_per_hour * (runtime_min / 60.0), 2) if runtime_min else None
        backend = resolve_provider(provider, project=project)

        message = (
            f"Run “{label}” in your own cloud?\n"
            f"Hardware: {rec.count}× {rec.accel.label}\n"
            f"Rate: ${rec.usd_per_hour:.2f}/hour\n"
            f"Estimated: {runtime_min:g} min"
            + (f" (~${total:.2f})" if total is not None else "")
            + "\n"
            f"Destination: {backend.describe_destination()}\n"
            "The instance is torn down when the job finishes, fails, or hits its "
            "deadline. You are billed by your own cloud provider."
        )
        decision = await ctx.elicit(message=message, schema=BurstApproval)
        if decision.action != "accept":
            return {"success": False, "status": "declined", "workload": label}
        content = getattr(decision, "data", None) or getattr(decision, "content", None)
        if content is not None and getattr(content, "confirm", True) is False:
            return {"success": False, "status": "declined", "workload": label}

        receipt = run_burst(
            BurstRequest(
                label=label,
                accelerator_id=rec.accel.id,
                chip_count=rec.count,
                usd_per_hour=rec.usd_per_hour,
                deadline_minutes=max(runtime_min * 2, 5.0) if runtime_min else 60.0,
            ),
            backend,
        )
        return receipt.as_dict()

    return mcp


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")
    try:
        _build_server().run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        _logger.exception("Xtreme Burst MCP server stopped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
