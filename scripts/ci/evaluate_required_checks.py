# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Fail closed when an expected CI lane did not execute successfully."""
from __future__ import annotations

import json
import os

LANES = {
    "tests": "python", "tests-os": "python", "lint": "python",
    "js-tests": "frontend", "installer-tests": "installer",
    "rust-tests": "rust", "docs-site": "site", "contributor-check": "python",
    "uv-lockfile": "uv_lock", "docker-lint": "docker_meta",
}


def evaluate(needs: dict, event: str) -> list[str]:
    failures = []
    detect = needs.get("detect", {})
    outputs = detect.get("outputs", {})
    if detect.get("result") != "success":
        failures.append("detect: classification did not succeed")
    expected = {name: True for name in ("detect", "infographic-check", "osv-scanner")}
    for name, flag in LANES.items():
        value = outputs.get(flag)
        if value not in ("true", "false"):
            failures.append(f"{name}: missing or invalid classification {flag}")
        expected[name] = value != "false"
    pr = event == "pull_request"
    for flag in ("npm_lock", "scan", "deps", "ci_review", "mcp_catalog"):
        if outputs.get(flag) not in ("true", "false"):
            failures.append(f"detect: missing or invalid classification {flag}")
    expected["history-check"] = pr
    expected["lockfile-diff"] = pr and outputs.get("npm_lock") == "true"
    expected["supply-chain"] = pr and any(outputs.get(k) == "true" for k in ("scan", "deps"))
    critical = needs.get("supply-chain", {}).get("outputs", {}).get("critical_findings")
    expected["review-labels"] = pr and (
        any(outputs.get(k) == "true" for k in ("ci_review", "mcp_catalog")) or critical == "true"
    )
    for name, required in expected.items():
        result = needs.get(name, {}).get("result")
        if result != "success" and not (result == "skipped" and not required):
            failures.append(f"{name}: {result or 'missing'} (required={required})")
    for name in needs.keys() - expected.keys():
        failures.append(f"{name}: unclassified dependency")
    return failures


def main() -> int:
    needs = json.loads(os.environ["NEEDS"])
    compact = {name: info.get("result") for name, info in needs.items()}
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"needs-json={json.dumps(compact)}\n")
    failures = evaluate(needs, os.environ["EVENT_NAME"])
    for failure in failures:
        print(f"::error::{failure}")
    if not failures:
        print("All expected checks executed successfully; excluded lanes accounted for.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
