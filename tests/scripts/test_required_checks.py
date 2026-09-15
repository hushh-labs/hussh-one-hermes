# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""A skipped or cancelled selected lane cannot authorize a merge."""
import pytest

from scripts.ci.evaluate_required_checks import LANES, evaluate


def results():
    outputs = dict.fromkeys(set(LANES.values()) | {"npm_lock", "scan", "deps", "ci_review", "mcp_catalog"}, "false")
    needs = {name: {"result": "skipped"} for name in LANES}
    needs.update({name: {"result": "skipped"} for name in ("history-check", "lockfile-diff", "supply-chain", "review-labels")})
    needs.update({name: {"result": "success"} for name in ("infographic-check", "osv-scanner")})
    needs["detect"] = {"result": "success", "outputs": outputs}
    return needs


def test_excluded_lanes_can_skip():
    assert evaluate(results(), "merge_group") == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_selected_lane_must_run(result):
    needs = results()
    needs["detect"]["outputs"]["python"] = "true"
    for name in ("tests", "tests-os", "lint", "contributor-check"):
        needs[name]["result"] = "success"
    needs["tests"]["result"] = result
    assert any("tests:" in failure for failure in evaluate(needs, "merge_group"))


def test_missing_classifier_is_not_exclusion():
    needs = results()
    del needs["detect"]["outputs"]["python"]
    assert evaluate(needs, "push")


def test_failed_classifier_blocks_even_with_successful_children():
    needs = results()
    needs["detect"]["result"] = "cancelled"
    assert evaluate(needs, "push")


def test_unknown_dependency_blocks():
    needs = results()
    needs["forgotten-lane"] = {"result": "skipped"}
    assert evaluate(needs, "merge_group")
