#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Reconcile this machine's Puppy One cron jobs with the versioned manifest.

Until 2026-09-02 every daily job (its schedule, prompt, toolsets and the
script behind it) lived only in one machine's ``~/.hermes``: a product a
fleet could not receive and a change nobody could review. This script is the
other half of the daily updater: after the fork fast-forwards, the scripts
under ``scripts/hussh-one-cron`` are installed into ``$HERMES_HOME/scripts``
and each job named in ``jobs.manifest.json`` is created or brought back into
line, by name.

What it never does: touch ``deliver`` on an existing job (that is the owner's
own chat, per device), touch ``model`` or ``provider`` (the device default
applies), or remove or re-enable a job the manifest does not name (the founder
disables jobs on purpose).

    hussh-one-cron-sync.py --check    # print drift, change nothing (default)
    hussh-one-cron-sync.py --apply    # install scripts, create/update jobs
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
MANIFEST = HERE / "jobs.manifest.json"

# Fields the manifest owns on an existing job. Everything else is the device's.
MANAGED_FIELDS = ("schedule", "script", "no_agent", "enabled_toolsets", "skills", "prompt")
SCRIPT_SUFFIXES = (".py", ".sh")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def load_manifest(path: Path = MANIFEST) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs") or []
    for job in jobs:
        if job.get("prompt_file"):
            job["prompt"] = (Path(path).parent / job["prompt_file"]).read_text(encoding="utf-8").rstrip() + "\n"
    return jobs


# --------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------


def install_scripts(source: Path, target: Path, *, apply: bool) -> list[str]:
    """Copy every job script (and helper package) whose content differs."""
    changed: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name in ("prompts", "__pycache__") or entry.name.startswith("."):
            continue
        if entry.name.endswith((".json", ".md")) or entry.name == Path(__file__).name:
            continue
        destination = target / entry.name
        if entry.is_dir():
            if not destination.exists() or _dir_differs(entry, destination):
                changed.append(entry.name + "/")
                if apply:
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(entry, destination, ignore=shutil.ignore_patterns("__pycache__"))
            continue
        if not entry.name.endswith(SCRIPT_SUFFIXES):
            continue
        if not destination.exists() or not filecmp.cmp(entry, destination, shallow=False):
            changed.append(entry.name)
            if apply:
                shutil.copy2(entry, destination)
                os.chmod(destination, 0o755)
    return changed


def _dir_differs(source: Path, destination: Path) -> bool:
    for path in source.rglob("*"):
        if "__pycache__" in path.parts or path.is_dir():
            continue
        other = destination / path.relative_to(source)
        if not other.exists() or not filecmp.cmp(path, other, shallow=False):
            return True
    return False


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def desired_fields(entry: dict) -> dict:
    """The manifest's view of a job, in the cron store's field names."""
    fields: dict = {
        "schedule": entry["schedule"],
        "script": entry.get("script"),
        "no_agent": bool(entry.get("no_agent", False)),
    }
    if not fields["no_agent"]:
        fields["enabled_toolsets"] = list(entry.get("enabled_toolsets") or [])
        fields["skills"] = list(entry.get("skills") or [])
        fields["prompt"] = entry.get("prompt") or ""
    return fields


def drift(existing: dict, desired: dict) -> dict:
    """The managed fields whose stored value differs from the manifest."""
    out: dict = {}
    for key, value in desired.items():
        if key == "schedule":
            stored = (existing.get("schedule") or {})
            stored_expr = stored.get("expr") or stored.get("display") or existing.get("schedule_display")
            if str(stored_expr or "").strip() != str(value).strip():
                out[key] = {"have": stored_expr, "want": value}
        elif key == "prompt":
            if (existing.get("prompt") or "").strip() != str(value).strip():
                out[key] = {"have": f"{len(existing.get('prompt') or '')} chars", "want": f"{len(value)} chars"}
        elif key in ("enabled_toolsets", "skills"):
            if list(existing.get(key) or []) != list(value):
                out[key] = {"have": existing.get(key), "want": value}
        else:
            if existing.get(key) != value:
                out[key] = {"have": existing.get(key), "want": value}
    return out


def reconcile(
    manifest_jobs: list[dict],
    *,
    load: Callable[[], list[dict]],
    create: Callable[..., dict],
    update: Callable[[str, dict], Optional[dict]],
    apply: bool,
) -> dict:
    """Create missing jobs, update drifted managed fields, report everything."""
    existing = {str(j.get("name") or ""): j for j in load()}
    report: dict = {"created": [], "updated": {}, "unchanged": [], "errors": {}}
    for entry in manifest_jobs:
        name = entry["name"]
        desired = desired_fields(entry)
        current = existing.get(name)
        if current is None:
            report["created"].append(name)
            if apply:
                try:
                    create(
                        prompt=desired.get("prompt") or None,
                        schedule=desired["schedule"],
                        name=name,
                        deliver=entry.get("default_deliver") or "local",
                        script=desired.get("script"),
                        no_agent=desired["no_agent"],
                        enabled_toolsets=desired.get("enabled_toolsets") or None,
                        skills=desired.get("skills") or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    report["errors"][name] = f"create failed: {exc}"
            continue
        changes = drift(current, desired)
        if not changes:
            report["unchanged"].append(name)
            continue
        report["updated"][name] = changes
        if apply:
            updates = {key: desired[key] for key in changes}
            try:
                update(str(current["id"]), updates)
            except Exception as exc:  # noqa: BLE001
                report["errors"][name] = f"update failed: {exc}"
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="install scripts and reconcile jobs")
    parser.add_argument("--check", action="store_true", help="report drift only (default)")
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args(argv)
    apply = bool(args.apply and not args.check)

    manifest_jobs = load_manifest(Path(args.manifest))
    scripts_changed = install_scripts(HERE, hermes_home() / "scripts", apply=apply)

    sys.path.insert(0, str(REPO_ROOT))
    from cron import jobs as store  # noqa: E402 - the repo's own cron store

    report = reconcile(
        manifest_jobs,
        load=lambda: store.load_jobs(),
        create=store.create_job,
        update=store.update_job,
        apply=apply,
    )
    verb = "applied" if apply else "would apply"
    print(f"scripts {verb}: {', '.join(scripts_changed) or 'none differ'}")
    print(f"jobs created ({verb}): {', '.join(report['created']) or 'none'}")
    for name, changes in report["updated"].items():
        print(f"job updated ({verb}): {name}: " + "; ".join(
            f"{k} {v['have']!r} -> {v['want']!r}" for k, v in changes.items()))
    print(f"jobs unchanged: {len(report['unchanged'])}")
    for name, error in report["errors"].items():
        print(f"ERROR {name}: {error}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
