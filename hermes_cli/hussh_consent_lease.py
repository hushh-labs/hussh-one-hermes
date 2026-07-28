# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""One-time, short-lived handoff for locally decrypted Hussh consent exports."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time
from typing import Any

from hermes_constants import get_hermes_home


LEASE_TTL_SECONDS = 10 * 60
MAX_INFORMATION_BYTES = 64 * 1024
_LEASE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _lease_directory() -> Path:
    hermes_home = get_hermes_home().resolve()
    directory = hermes_home / "tmp" / "consent"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("Consent lease directory must not be a symlink.")
    if not directory.resolve().is_relative_to(hermes_home):
        raise RuntimeError("Consent lease directory escaped the Hermes home.")
    os.chmod(directory, 0o700)
    return directory


def _lease_path(lease_id: str) -> Path:
    if not _LEASE_ID_RE.fullmatch(lease_id):
        raise ValueError("Invalid consent lease identifier.")
    return _lease_directory() / f"{lease_id}.json"


def cleanup_expired_leases(*, now: float | None = None) -> int:
    """Remove expired or malformed lease files without following symlinks."""
    current = time.time() if now is None else now
    removed = 0
    for path in _lease_directory().glob("*.json"):
        try:
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode):
                path.unlink()
                removed += 1
                continue
            if current - file_stat.st_mtime > LEASE_TTL_SECONDS:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def materialize_decrypted_export(
    information: dict[str, Any],
    *,
    expected_scope: str,
    granted_scope: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Persist authorized plaintext as a bounded, opaque, one-time lease."""
    current = time.time() if now is None else now
    cleanup_expired_leases(now=current)

    encoded_information = json.dumps(
        information,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded_information) > MAX_INFORMATION_BYTES:
        return {
            "status": "requires_narrower_scope",
            "message": "Authorized information exceeds the safe one-turn limit.",
            "information_bytes": len(encoded_information),
        }
    if not information:
        return {
            "status": "decrypted_export_empty",
            "message": "The approved export contained no information for this scope.",
            "information_bytes": 0,
        }

    lease_id = secrets.token_hex(16)
    payload = {
        "version": 1,
        "created_at": int(current),
        "expires_at": int(current + LEASE_TTL_SECONDS),
        "expected_scope": expected_scope,
        "granted_scope": granted_scope,
        "information": information,
    }
    directory = _lease_directory()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{lease_id}-",
        suffix=".tmp",
        dir=directory,
    )
    temporary_path = Path(temporary_name)
    final_path = _lease_path(lease_id)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
        os.chmod(final_path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)

    return {
        "status": "decrypted_export_ready",
        "lease_id": lease_id,
        "expires_at": payload["expires_at"],
        "information_bytes": len(encoded_information),
        "consume_command": (
            f"{shlex.quote(sys.executable)} -m hermes_cli.hussh_consent_lease "
            f"consume {lease_id}"
        ),
    }


def consume_lease(lease_id: str, *, now: float | None = None) -> dict[str, Any]:
    """Load and delete one lease before returning its authorized information."""
    current = time.time() if now is None else now
    path = _lease_path(lease_id)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise PermissionError("Consent lease permissions are invalid.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(MAX_INFORMATION_BYTES + 4096)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Consent lease payload is invalid.")
    if int(payload.get("expires_at") or 0) < int(current):
        raise TimeoutError("Consent lease expired.")
    information = payload.get("information")
    if not isinstance(information, dict):
        raise ValueError("Consent lease information is invalid.")
    return {
        "status": "consumed",
        "expected_scope": str(payload.get("expected_scope") or ""),
        "granted_scope": str(payload.get("granted_scope") or ""),
        "information": information,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume a one-time Hussh consent lease.")
    parser.add_argument("action", choices=("consume", "cleanup"))
    parser.add_argument("lease_id", nargs="?")
    args = parser.parse_args(argv)

    try:
        if args.action == "cleanup":
            cleanup_expired_leases()
            return 0
        if not args.lease_id:
            parser.error("consume requires a lease identifier")
        result = consume_lease(args.lease_id)
    except (
        OSError,
        PermissionError,
        UnicodeDecodeError,
        ValueError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
