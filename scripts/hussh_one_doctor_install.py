# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Install and migrate the deterministic Hussh One doctor without changing its job ID."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


DOCTOR_JOB_NAME = "Hussh One Self-Healing Doctor"
RUNTIME_SCRIPT_NAME = "hussh_one_doctor_heal.py"
SOURCE_SCRIPT_NAME = "hussh-one-doctor-heal.py"
DETERMINISTIC_PROMPT = (
    "Run the deterministic Hussh One doctor script. It controls delivery through stdout; "
    "do not invoke an agent or send an additional message."
)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def migrate_existing_doctor_job(repo_root: Path) -> str | None:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from cron.jobs import list_jobs, update_job

    for job in list_jobs():
        script = Path(str(job.get("script") or "")).name
        if job.get("name") != DOCTOR_JOB_NAME and script != RUNTIME_SCRIPT_NAME:
            continue
        updated = update_job(
            job["id"],
            {
                "script": RUNTIME_SCRIPT_NAME,
                "no_agent": True,
                "prompt": DETERMINISTIC_PROMPT,
            },
        )
        return str(updated["id"]) if updated else None
    return None


def install(repo_root: Path, hermes_home: Path, python_bin: str | None = None) -> str | None:
    source = repo_root / "scripts" / SOURCE_SCRIPT_NAME
    if not source.is_file():
        raise FileNotFoundError(f"managed doctor source is missing: {source}")
    _atomic_copy(source, hermes_home / "scripts" / RUNTIME_SCRIPT_NAME)
    _atomic_json(
        hermes_home / "hussh-one-runtime.json",
        {
            "schema": 1,
            "repo_root": str(repo_root.resolve()),
            "python_bin": python_bin or sys.executable,
        },
    )
    return migrate_existing_doctor_job(repo_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--python-bin", default=sys.executable)
    args = parser.parse_args()
    job_id = install(args.repo_root.resolve(), args.hermes_home.expanduser(), args.python_bin)
    print(f"Installed managed Hussh doctor; cron job {'updated: ' + job_id if job_id else 'not found'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
