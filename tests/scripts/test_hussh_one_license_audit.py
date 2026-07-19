# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts/hussh-one-license-audit.py"
SPEC = importlib.util.spec_from_file_location("hussh_one_license_audit", AUDIT_PATH)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_repository(tmp_path: Path) -> None:
    (tmp_path / "LICENSES").mkdir()
    (tmp_path / "LICENSE").write_text("                                 Apache License\n", encoding="utf-8")
    (tmp_path / "NOTICE").write_text(
        "Hussh One Hermes\nCopyright 2026 Hushh Labs\nNous Research\nMIT\n", encoding="utf-8"
    )
    (tmp_path / "LICENSES/UPSTREAM-MIT.txt").write_text(
        "MIT License\nCopyright (c) 2025 Nous Research\nPermission is hereby granted\n", encoding="utf-8"
    )
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text("nested terms override\n", encoding="utf-8")
    (tmp_path / "LICENSES/attribution.toml").write_text(
        """version = 1
[distribution]
spdx_expression = "Apache-2.0 AND MIT"
[provenance]
upstream_base_commit = "base"
upstream_retained = "MIT"
upstream_modified = "MIT AND Apache-2.0"
hussh_added = "Apache-2.0"
source_extensions = [".py"]
[spdx_headers]
copyright = "SPDX-FileCopyrightText: 2026 Hushh Labs"
license = "SPDX-License-Identifier: Apache-2.0"
[[nested_component]]
path = "nested"
license = "MIT"
required_files = ["LICENSE"]
""",
        encoding="utf-8",
    )
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/LICENSE").write_text("MIT\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """[project]
license = "Apache-2.0 AND MIT"
license-files = ["LICENSE", "NOTICE", "LICENSES/*", "THIRD_PARTY_NOTICES.md"]
""",
        encoding="utf-8",
    )
    for manifest in audit.PACKAGE_MANIFESTS:
        _write_json(tmp_path / manifest, {"license": audit.SPDX_EXPRESSION})
    root_manifest = _write_json(
        tmp_path / "package.json",
        {"license": audit.SPDX_EXPRESSION, "files": ["LICENSE", "NOTICE", "LICENSES/", "THIRD_PARTY_NOTICES.md"]},
    )
    assert root_manifest is None
    root_packages = {path.as_posix(): {"license": audit.SPDX_EXPRESSION} for path in audit.ROOT_WORKSPACE_LOCK_PATHS}
    _write_json(tmp_path / "package-lock.json", {"packages": root_packages})
    _write_json(
        tmp_path / "scripts/whatsapp-bridge/package-lock.json",
        {"packages": {"": {"license": audit.SPDX_EXPRESSION}}},
    )


def test_license_audit_passes_current_distribution():
    assert audit.audit_repository(ROOT) == []


def test_license_audit_rejects_apache_only_python_metadata(tmp_path):
    _make_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nlicense = \"Apache-2.0\"\nlicense-files = []\n", encoding="utf-8"
    )

    errors = audit.audit_repository(tmp_path, changes=[])

    assert any("project.license" in error for error in errors)
    assert any("license-files" in error for error in errors)


def test_license_audit_rejects_missing_nested_artifact(tmp_path):
    _make_repository(tmp_path)
    (tmp_path / "nested/LICENSE").unlink()

    errors = audit.audit_repository(tmp_path, changes=[])

    assert "nested: missing required nested license artifact 'LICENSE'" in errors


def test_added_hussh_source_needs_spdx_headers(tmp_path):
    _make_repository(tmp_path)
    source = tmp_path / "hussh_added.py"
    source.write_text("print('missing headers')\n", encoding="utf-8")

    errors = audit.validate_changed_source_headers(
        tmp_path,
        audit._read_toml(tmp_path / "LICENSES/attribution.toml"),
        [("A", Path("hussh_added.py"))],
    )

    assert errors == ["hussh_added.py: Hussh-added source requires Apache-2.0 SPDX copyright and license headers"]


def test_path_provenance_classification_preserves_upstream_and_nested_terms(tmp_path):
    _make_repository(tmp_path)
    attribution = audit._read_toml(tmp_path / "LICENSES/attribution.toml")

    assert audit.classify_change("M", Path("run_agent.py"), attribution) == "MIT AND Apache-2.0"
    assert audit.classify_change("A", Path("hussh_added.py"), attribution) == "Apache-2.0"
    assert audit.classify_change("A", Path("nested/tool.py"), attribution) == "MIT"
