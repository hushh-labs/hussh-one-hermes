# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Answer one question about this machine: what leaves it.

Pinning `model.provider: lmstudio` covers the main turn and nothing else.
Every auxiliary task carries its own provider, and the default is `auto`, which
resolves through a fallback chain ending at a paid cloud model. A config that
looks on-device can therefore route most of its work off the machine, and the
only visible symptom is a bill.

This audits every configured auxiliary task and reports, per task, whether it
stays here. It runs the real resolver rather than reimplementing the rules,
because a second copy of the routing logic would drift from the first and this
tool would confidently describe a machine that does not exist.

Simulation is the point. `--simulate on` shows what the gate WOULD change
without touching config, so the owner can see the cost before paying it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Providers that answer without leaving this machine.
LOCAL_PROVIDERS = frozenset({"lmstudio", "lm-studio", "lm_studio", "ollama"})

# Keys under `auxiliary` that configure a task rather than being one.
_NON_TASK_KEYS = frozenset(
    {
        "transient_retries",
        "free_only",
        "openrouter_model",
        "stream_only_base_urls",
    }
)

STAYS = "stays"
LEAVES = "leaves"
REFUSED = "refused"
UNKNOWN = "unknown"


def _is_local(provider: Optional[str]) -> bool:
    return str(provider or "").strip().casefold() in LOCAL_PROVIDERS


def _resolve_effective(provider: str, model: str, task: str) -> tuple[str, str]:
    """Ask the real router where a task lands. Returns (provider, model).

    Calls `resolve_provider_client` rather than reimplementing its rules. A
    second copy of the routing logic would drift from the first, and this tool
    would then confidently describe a machine that does not exist. That is not
    hypothetical: the first version of this file assumed `auto` fell straight
    through to a cloud provider and reported 19 leaking tasks, when in fact
    step 1 of auto-route uses the MAIN provider, which is local here. It
    overstated the problem by eighteen tasks.
    """
    from agent.auxiliary_client import _resolve_auto_route, resolve_provider_client

    if (provider or "auto") == "auto":
        _client, resolved_model, effective = _resolve_auto_route(
            main_runtime=None, task=task
        )
        return str(effective or ""), str(resolved_model or "")

    client, resolved_model = resolve_provider_client(
        provider=provider, model=model or None, task=task
    )
    # A refusal returns (None, None): the gate declined rather than routing.
    if client is None:
        return "", ""
    return provider, str(resolved_model or model or "")


class simulated_gate:
    """Force the gate on or off for the duration of a block.

    The router reads the gate live from config, so a flag that merely LABELS a
    report as "gate on" would print the gate-off behaviour under a gate-on
    heading. That is worse than not simulating at all: it would show the gate
    changing nothing and invite the owner to conclude it does nothing.

    So the simulation actually replaces the gate read. It touches no config, and
    it restores the original function even if the audit raises.
    """

    def __init__(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._original: Any = None

    def __enter__(self) -> "simulated_gate":
        from agent import auxiliary_client

        self._original = auxiliary_client._on_device_only_enabled
        auxiliary_client._on_device_only_enabled = lambda: self._enabled
        return self

    def __exit__(self, *_exc: Any) -> bool:
        from agent import auxiliary_client

        if self._original is not None:
            auxiliary_client._on_device_only_enabled = self._original
        return False


def audit_tasks(config: dict[str, Any], *, gate_on: bool) -> list[dict[str, Any]]:
    """Classify every auxiliary task by asking the router where it lands.

    Wrap the call in `simulated_gate(gate_on)` to audit a state the process is
    not actually in; `build_report` does this for you.
    """
    auxiliary = config.get("auxiliary") or {}
    rows: list[dict[str, Any]] = []
    for name, settings in sorted(auxiliary.items()):
        if name in _NON_TASK_KEYS or not isinstance(settings, dict):
            continue
        configured = str(settings.get("provider") or "").strip() or "auto"
        model = str(settings.get("model") or "").strip()

        try:
            effective, effective_model = _resolve_effective(configured, model, name)
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "task": name,
                    "configured": configured,
                    "effective": "",
                    "model": "",
                    "verdict": UNKNOWN,
                    "detail": f"router raised {type(exc).__name__}",
                }
            )
            continue

        if not effective:
            verdict = REFUSED if gate_on else UNKNOWN
            detail = (
                "gate refused this route"
                if gate_on
                else "router declined; no provider available"
            )
        elif _is_local(effective):
            verdict, detail = STAYS, f"resolves to {effective}"
        else:
            verdict, detail = LEAVES, f"resolves to {effective}"

        rows.append(
            {
                "task": name,
                "configured": configured,
                "effective": effective,
                "model": effective_model,
                "verdict": verdict,
                "detail": detail,
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {STAYS: 0, LEAVES: 0, REFUSED: 0, UNKNOWN: 0}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return counts


def main_turn(config: dict[str, Any]) -> dict[str, Any]:
    """The main turn, audited on the same terms as the side tasks."""
    model_cfg = config.get("model") or {}
    provider = str(model_cfg.get("provider") or "").strip()
    return {
        "task": "main_turn",
        "provider": provider or "auto",
        "model": str(model_cfg.get("default") or ""),
        "verdict": STAYS if _is_local(provider) else LEAVES,
        "detail": "pinned to a local provider"
        if _is_local(provider)
        else f"provider {provider or 'auto'} is not local",
    }


def build_report(config: dict[str, Any], *, gate_on: bool) -> dict[str, Any]:
    # Actually enter the state being reported, so the heading and the rows
    # describe the same machine.
    with simulated_gate(gate_on):
        rows = audit_tasks(config, gate_on=gate_on)
    main = main_turn(config)
    return {
        "gate_on": gate_on,
        "main_turn": main,
        "auxiliary": rows,
        "counts": summarize(rows),
        # The number that matters: how many distinct kinds of work can reach a
        # vendor right now. Reported for the side tasks and the main turn
        # together, because the owner does not experience them separately.
        "leaves_total": summarize(rows)[LEAVES] + (0 if main["verdict"] == STAYS else 1),
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    state = "ON" if report["gate_on"] else "OFF"
    lines.append(f"on_device_only: {state}")
    lines.append("")
    main = report["main_turn"]
    lines.append(f"  {'main_turn':24} {main['verdict']:8} {main['detail']}")
    for row in report["auxiliary"]:
        # Show configured and effective together: the gap between them is the
        # whole reason a config can look on-device and not be.
        configured = row.get("configured", "")
        suffix = (
            f"  (configured {configured})"
            if configured and configured != row.get("effective")
            else ""
        )
        lines.append(
            f"  {row['task']:24} {row['verdict']:8} {row['detail']}{suffix}"
        )
    lines.append("")
    counts = report["counts"]
    lines.append(
        f"  stays={counts[STAYS]}  leaves={counts[LEAVES]}  refused={counts[REFUSED]}"
    )
    if report["leaves_total"]:
        lines.append(
            f"  *** {report['leaves_total']} kinds of work can reach a model vendor ***"
        )
    else:
        lines.append("  nothing reaches a model vendor")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulate",
        choices=["on", "off", "both"],
        default="both",
        help="audit under a hypothetical gate state without changing config",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    from hermes_cli.config import cfg_get, load_config_readonly

    config = load_config_readonly()
    live_gate = bool(cfg_get(config, "hussh_one", "on_device_only"))

    states = [True, False] if args.simulate == "both" else [args.simulate == "on"]
    reports = [build_report(config, gate_on=state) for state in states]

    if args.json:
        print(json.dumps({"live_gate_on": live_gate, "reports": reports}, indent=2))
        return 0

    print(f"live config has on_device_only = {live_gate}")
    print()
    for report in reports:
        print(render(report))
        print()

    # Exit non-zero when the LIVE state leaks, so this can gate a check rather
    # than only inform a reader.
    live = next((r for r in reports if r["gate_on"] == live_gate), reports[0])
    return 1 if live["leaves_total"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
