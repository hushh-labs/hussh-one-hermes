# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Agent sync.

Two things must never happen: a sync that deletes something the person owns,
and a swap to a payload that was never validated. Everything else is recoverable
by running it again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import hussh_one_agent_sync as sync


def _desired(**kw):
    base = {"name": "agent-one", "source": "git@example:one.git", "ref": "abc123"}
    base.update(kw)
    return sync.DesiredAgent(**base)


def _installed(**kw):
    base = {"name": "agent-one", "source": "git@example:one.git", "applied_ref": "abc123"}
    base.update(kw)
    return sync.InstalledAgent(**base)


class TestPlanIsPure:
    def test_a_missing_agent_is_installed(self):
        actions = sync.plan([_desired()], [])
        assert [a.action for a in actions] == [sync.ACTION_INSTALL]

    def test_a_matching_ref_is_a_noop(self):
        actions = sync.plan([_desired()], [_installed()])
        assert actions[0].action == sync.ACTION_NOOP

    def test_a_changed_ref_is_an_update(self):
        actions = sync.plan([_desired(ref="def456")], [_installed()])
        assert actions[0].action == sync.ACTION_UPDATE
        assert "abc123 -> def456" in actions[0].reason

    def test_an_install_with_no_recorded_ref_is_updated(self):
        # A machine that cannot say what it runs cannot be trusted to say it is
        # up to date either. This is the provenance gap the design closes, and
        # it holds even when the fleet pins no ref of its own.
        actions = sync.plan([_desired(ref="")], [_installed(applied_ref="")])
        assert actions[0].action == sync.ACTION_UPDATE
        assert "without a recorded ref" in actions[0].reason

    def test_a_ref_mismatch_against_an_unknown_installed_ref_still_updates(self):
        actions = sync.plan([_desired(ref="abc123")], [_installed(applied_ref="")])
        assert actions[0].action == sync.ACTION_UPDATE


class TestUserOwnedPathsAreRefusedAtPlanTime:
    @pytest.mark.parametrize(
        "path", ["memories", "sessions/2026.jsonl", ".env", "auth.json", "state.db"]
    )
    def test_a_distribution_claiming_them_is_blocked(self, path):
        # Refused when PLANNING, not when applying. A sync that discovered this
        # halfway through would already have deleted something.
        actions = sync.plan(
            [_desired()], [], owned_paths={"agent-one": ["SOUL.md", path]}
        )
        assert actions[0].action == sync.ACTION_BLOCKED
        assert path in actions[0].blocked_paths

    def test_ordinary_distribution_paths_are_allowed(self):
        actions = sync.plan(
            [_desired()],
            [],
            owned_paths={"agent-one": ["SOUL.md", "config.yaml", "skills/a.md"]},
        )
        assert actions[0].action == sync.ACTION_INSTALL

    def test_a_blocked_agent_is_never_reconciled(self):
        staged = []
        actions = sync.plan([_desired()], [], owned_paths={"agent-one": [".env"]})
        result = sync.reconcile(
            actions,
            stage=lambda a: staged.append(a) or Path("/tmp/x"),
            validate=lambda a, p: True,
            swap=lambda a, p: None,
            discard=lambda a, p: None,
        )
        assert staged == []
        assert result["applied"] == []


class TestPruneIsOptIn:
    def test_an_unlisted_agent_is_left_alone_by_default(self):
        # Usually a fleet manifest that has not been updated, not an agent to
        # delete, and deleting is the one action re-running cannot undo.
        actions = sync.plan([], [_installed(name="orphan")])
        assert actions == []

    def test_prune_removes_it_when_asked(self):
        actions = sync.plan([], [_installed(name="orphan")], prune=True)
        assert [a.action for a in actions] == [sync.ACTION_REMOVE]


class TestNothingSwapsUnvalidated:
    def test_a_failed_validation_discards_instead_of_swapping(self):
        swapped, discarded = [], []
        actions = sync.plan([_desired()], [])
        result = sync.reconcile(
            actions,
            stage=lambda a: Path("/tmp/staged"),
            validate=lambda a, p: False,
            swap=lambda a, p: swapped.append(a.name),
            discard=lambda a, p: discarded.append(a.name),
        )
        assert swapped == []
        assert discarded == ["agent-one"]
        assert result["failed"][0]["reason"] == "staged payload failed validation"

    def test_a_raising_stage_discards_nothing_and_records_the_failure(self):
        def _stage(_a):
            raise OSError("network died")

        result = sync.reconcile(
            sync.plan([_desired()], []),
            stage=_stage,
            validate=lambda a, p: True,
            swap=lambda a, p: None,
            discard=lambda a, p: None,
        )
        assert result["applied"] == []
        assert "OSError" in result["failed"][0]["reason"]

    def test_one_failure_does_not_abandon_the_other_agents(self):
        # A half-synced fleet where the failure stopped everything after it is
        # harder to reason about than one where each agent's state is its own.
        desired = [_desired(name="good"), _desired(name="bad"), _desired(name="also-good")]

        def _validate(action, _path):
            return action.name != "bad"

        result = sync.reconcile(
            sync.plan(desired, []),
            stage=lambda a: Path("/tmp/s"),
            validate=_validate,
            swap=lambda a, p: None,
            discard=lambda a, p: None,
        )
        assert {r["name"] for r in result["applied"]} == {"good", "also-good"}
        assert [r["name"] for r in result["failed"]] == ["bad"]


class TestProvenanceIsCapturedBeforeTheSwap:
    def test_the_resolved_ref_and_digest_are_recorded(self):
        # Captured while the staged source still exists. Afterwards it is gone,
        # which is exactly how the previous implementation lost it.
        order = []
        result = sync.reconcile(
            sync.plan([_desired()], []),
            stage=lambda a: Path("/tmp/s"),
            validate=lambda a, p: True,
            swap=lambda a, p: order.append("swap"),
            discard=lambda a, p: None,
            resolve_ref=lambda a, p: order.append("ref") or "resolved-sha",
            digest=lambda a, p: "digest123",
        )
        assert order == ["ref", "swap"]
        assert result["applied"][0]["applied_ref"] == "resolved-sha"
        assert result["applied"][0]["applied_digest"] == "digest123"


class TestRestartIsOnlyForRealChanges:
    def test_a_noop_sync_does_not_require_a_restart(self):
        # Restarting on a no-op interrupts turns for nothing, which is how a
        # safety mechanism trains people to disable it.
        assert sync.summarize(sync.plan([_desired()], [_installed()]))[
            "restart_required"
        ] is False

    def test_a_real_change_does(self):
        assert sync.summarize(sync.plan([_desired()], []))["restart_required"] is True

    def test_reconcile_reports_restart_only_when_something_landed(self):
        result = sync.reconcile(
            sync.plan([_desired()], []),
            stage=lambda a: Path("/tmp/s"),
            validate=lambda a, p: False,
            swap=lambda a, p: None,
            discard=lambda a, p: None,
        )
        assert result["restart_required"] is False


class TestDigest:
    def test_the_same_tree_in_two_locations_hashes_the_same(self, tmp_path):
        # Absolute paths would give one distribution a different digest per
        # install location, so every staged-vs-live comparison would report
        # drift and every sync would replace everything.
        a, b = tmp_path / "a", tmp_path / "b"
        for root in (a, b):
            (root / "skills").mkdir(parents=True)
            (root / "SOUL.md").write_text("same")
            (root / "skills" / "s.md").write_text("also same")
        assert sync.content_digest(
            [a / "SOUL.md", a / "skills" / "s.md"], root=a
        ) == sync.content_digest([b / "SOUL.md", b / "skills" / "s.md"], root=b)

    def test_a_rename_changes_the_digest(self, tmp_path):
        (tmp_path / "one.md").write_text("x")
        (tmp_path / "two.md").write_text("x")
        assert sync.content_digest(
            [tmp_path / "one.md"], root=tmp_path
        ) != sync.content_digest([tmp_path / "two.md"], root=tmp_path)

    def test_changed_content_changes_the_digest(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("one")
        first = sync.content_digest([f], root=tmp_path)
        f.write_text("two")
        assert sync.content_digest([f], root=tmp_path) != first

    def test_an_unreadable_file_does_not_hash_as_absent(self, tmp_path):
        # Otherwise a broken install would look like a clean one.
        missing = tmp_path / "gone.txt"
        assert sync.content_digest([missing]) != sync.content_digest([])


class TestState:
    def test_state_is_written_atomically_and_readable(self, tmp_path):
        path = tmp_path / "state.json"
        sync.write_state(
            path,
            result={"applied": [{"name": "a", "applied_ref": "r"}], "failed": []},
            timestamp=1,
        )
        assert json.loads(path.read_text())["agents"]["a"]["applied_ref"] == "r"

    def test_a_missing_state_file_reads_as_empty(self, tmp_path):
        assert sync.read_state(tmp_path / "nope.json")["agents"] == {}


class TestFleetParsing:
    def test_an_entry_without_a_name_is_rejected(self):
        with pytest.raises(ValueError):
            sync.DesiredAgent.from_dict({"source": "x"})

    def test_a_non_mapping_entry_is_rejected(self):
        with pytest.raises(ValueError):
            sync.DesiredAgent.from_dict(["not", "a", "mapping"])
