#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Verify Hussh One's Apache-2.0 and MIT mixed-license distribution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTION_PATH = Path("LICENSES/attribution.toml")
SPDX_EXPRESSION = "Apache-2.0 AND MIT"
PACKAGE_MANIFESTS = (
    Path("package.json"),
    Path("apps/bootstrap-installer/package.json"),
    Path("apps/desktop/package.json"),
    Path("apps/shared/package.json"),
    Path("plugins/platforms/photon/sidecar/package.json"),
    Path("scripts/whatsapp-bridge/package.json"),
    Path("ui-tui/package.json"),
    Path("ui-tui/packages/hermes-ink/package.json"),
    Path("web/package.json"),
    Path("website/package.json"),
)
ROOT_WORKSPACE_LOCK_PATHS = (
    Path(""),
    Path("apps/bootstrap-installer"),
    Path("apps/desktop"),
    Path("apps/shared"),
    Path("ui-tui"),
    Path("ui-tui/packages/hermes-ink"),
    Path("web"),
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("must contain a JSON object")
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("must contain a TOML table")
    return value


def _contains_all(text: str, required: Iterable[str]) -> bool:
    return all(value in text for value in required)


def _path_in_component(path: Path, component: str) -> bool:
    normalized = path.as_posix()
    return normalized == component or normalized.startswith(f"{component}/")


def _git_changes(root: Path, base_commit: str) -> list[tuple[str, Path]]:
    command = ["git", "diff", "--name-status", "--find-renames=100%", base_commit, "--"]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        reason = result.stderr.strip() or "unknown git error"
        raise ValueError(f"cannot compare provenance base {base_commit}: {reason}")

    changes: list[tuple[str, Path]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0][0]
        path = Path(fields[-1])
        changes.append((status, path))
    return changes


def classify_change(status: str, path: Path, attribution: dict[str, Any]) -> str | None:
    """Return the declared SPDX expression for one changed repository path."""
    provenance = attribution.get("provenance", {})
    nested_components = attribution.get("nested_component", [])
    if not isinstance(provenance, dict) or not isinstance(nested_components, list):
        return None

    for component in nested_components:
        if isinstance(component, dict) and _path_in_component(path, str(component.get("path", ""))):
            return str(component.get("license", "")) or None
    if status == "A":
        return str(provenance.get("hussh_added", "")) or None
    if status in {"M", "R", "C"}:
        return str(provenance.get("upstream_modified", "")) or None
    if status == " ":
        return str(provenance.get("upstream_retained", "")) or None
    return None


def validate_metadata(root: Path, attribution: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    distribution = attribution.get("distribution", {})
    if not isinstance(distribution, dict) or distribution.get("spdx_expression") != SPDX_EXPRESSION:
        errors.append(f"{ATTRIBUTION_PATH}: distribution SPDX expression must be {SPDX_EXPRESSION!r}")

    license_path = root / "LICENSE"
    if not license_path.is_file() or not license_path.read_text(encoding="utf-8").startswith("                                 Apache License\n"):
        errors.append("LICENSE: must contain the Apache License 2.0 text")

    upstream_mit = root / "LICENSES/UPSTREAM-MIT.txt"
    upstream_mit_required = ("MIT License", "Copyright (c) 2025 Nous Research", "Permission is hereby granted")
    if not upstream_mit.is_file() or not _contains_all(upstream_mit.read_text(encoding="utf-8"), upstream_mit_required):
        errors.append("LICENSES/UPSTREAM-MIT.txt: missing or does not preserve the Nous Research MIT grant")

    notice = root / "NOTICE"
    notice_required = ("Hussh One Hermes", "Copyright 2026 Hushh Labs", "Nous Research", "MIT")
    if not notice.is_file() or not _contains_all(notice.read_text(encoding="utf-8"), notice_required):
        errors.append("NOTICE: missing Hussh One, Hushh Labs, or upstream MIT attribution")

    notices = root / "THIRD_PARTY_NOTICES.md"
    if not notices.is_file() or "nested terms override" not in notices.read_text(encoding="utf-8"):
        errors.append("THIRD_PARTY_NOTICES.md: missing nested-license attribution index")

    try:
        project = _read_toml(root / "pyproject.toml").get("project", {})
    except ValueError as exc:
        errors.append(f"pyproject.toml: {exc}")
    else:
        if project.get("license") != SPDX_EXPRESSION:
            errors.append(f"pyproject.toml: project.license must be {SPDX_EXPRESSION!r}")
        expected_files = {"LICENSE", "NOTICE", "LICENSES/*", "THIRD_PARTY_NOTICES.md"}
        if not expected_files.issubset(set(project.get("license-files", []))):
            errors.append("pyproject.toml: license-files must ship LICENSE, NOTICE, LICENSES/*, and THIRD_PARTY_NOTICES.md")

    for manifest_path in PACKAGE_MANIFESTS:
        try:
            manifest = _read_json(root / manifest_path)
        except ValueError as exc:
            errors.append(f"{manifest_path}: {exc}")
            continue
        if manifest.get("license") != SPDX_EXPRESSION:
            errors.append(f"{manifest_path}: license must be {SPDX_EXPRESSION!r}")

    try:
        root_manifest = _read_json(root / "package.json")
    except ValueError:
        root_manifest = {}
    required_package_files = {"LICENSE", "NOTICE", "LICENSES/", "THIRD_PARTY_NOTICES.md"}
    if not required_package_files.issubset(set(root_manifest.get("files", []))):
        errors.append("package.json: files must include the required legal artifacts")

    try:
        root_lock = _read_json(root / "package-lock.json")
    except ValueError as exc:
        errors.append(f"package-lock.json: {exc}")
    else:
        packages = root_lock.get("packages", {})
        for workspace_path in ROOT_WORKSPACE_LOCK_PATHS:
            key = "" if workspace_path == Path(".") else workspace_path.as_posix()
            package = packages.get(key)
            if not isinstance(package, dict) or package.get("license") != SPDX_EXPRESSION:
                label = key or "root"
                errors.append(f"package-lock.json: {label} license must be {SPDX_EXPRESSION!r}")

    try:
        bridge_lock = _read_json(root / "scripts/whatsapp-bridge/package-lock.json")
    except ValueError as exc:
        errors.append(f"scripts/whatsapp-bridge/package-lock.json: {exc}")
    else:
        bridge_root = bridge_lock.get("packages", {}).get("", {})
        if bridge_root.get("license") != SPDX_EXPRESSION:
            errors.append(f"scripts/whatsapp-bridge/package-lock.json: root license must be {SPDX_EXPRESSION!r}")

    return errors


def validate_nested_components(root: Path, attribution: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    components = attribution.get("nested_component", [])
    if not isinstance(components, list):
        return [f"{ATTRIBUTION_PATH}: nested_component must be an array"]

    for component in components:
        if not isinstance(component, dict):
            errors.append(f"{ATTRIBUTION_PATH}: nested component must be a table")
            continue
        component_path = component.get("path")
        required_files = component.get("required_files")
        if not isinstance(component_path, str) or not isinstance(required_files, list):
            errors.append(f"{ATTRIBUTION_PATH}: nested component needs path and required_files")
            continue
        for required_file in required_files:
            if not isinstance(required_file, str) or not (root / component_path / required_file).is_file():
                errors.append(f"{component_path}: missing required nested license artifact {required_file!r}")
    return errors


def validate_changed_source_headers(
    root: Path,
    attribution: dict[str, Any],
    changes: Iterable[tuple[str, Path]],
) -> list[str]:
    errors: list[str] = []
    provenance = attribution.get("provenance", {})
    header_policy = attribution.get("spdx_headers", {})
    if not isinstance(provenance, dict) or not isinstance(header_policy, dict):
        return [f"{ATTRIBUTION_PATH}: provenance and spdx_headers tables are required"]
    extensions = provenance.get("source_extensions", [])
    if not isinstance(extensions, list):
        return [f"{ATTRIBUTION_PATH}: source_extensions must be an array"]
    source_extensions = frozenset(str(extension) for extension in extensions)
    copyright_marker = str(header_policy.get("copyright", ""))
    license_marker = str(header_policy.get("license", ""))

    for status, path in changes:
        # A deleted upstream path is not part of this distribution, so it has
        # no distributable license classification or SPDX-header obligation.
        if status == "D":
            continue
        if path.suffix not in source_extensions:
            continue
        classification = classify_change(status, path, attribution)
        if classification is None:
            errors.append(f"{path}: unclassified Hussh source change ({status})")
            continue
        if status != "A" or classification != "Apache-2.0":
            continue
        try:
            opening = (root / path).read_text(encoding="utf-8")[:1024]
        except OSError as exc:
            errors.append(f"{path}: cannot read added source file: {exc}")
            continue
        if copyright_marker not in opening or license_marker not in opening:
            errors.append(f"{path}: Hussh-added source requires Apache-2.0 SPDX copyright and license headers")
    return errors


def audit_repository(root: Path = REPOSITORY_ROOT, changes: list[tuple[str, Path]] | None = None) -> list[str]:
    """Return every policy violation. ``changes`` exists for isolated unit tests."""
    try:
        attribution = _read_toml(root / ATTRIBUTION_PATH)
    except ValueError as exc:
        return [f"{ATTRIBUTION_PATH}: {exc}"]

    errors = validate_metadata(root, attribution)
    errors.extend(validate_nested_components(root, attribution))
    if changes is None:
        base = attribution.get("provenance", {}).get("upstream_base_commit")
        if not isinstance(base, str) or not base:
            errors.append(f"{ATTRIBUTION_PATH}: provenance.upstream_base_commit is required")
        else:
            try:
                changes = _git_changes(root, base)
            except ValueError as exc:
                errors.append(str(exc))
    if changes is not None:
        errors.extend(validate_changed_source_headers(root, attribution, changes))
    return errors


def main() -> int:
    errors = audit_repository()
    if errors:
        for error in errors:
            print(f"license audit: {error}", file=sys.stderr)
        return 1
    print("Hussh One license audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
