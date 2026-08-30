# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Recovering merge ground truth from history.

These build real git repositories rather than mocking git. The whole value of
the corpus is that the conflict is the one git actually produced and the
resolution is the one a human actually shipped, and a mock would assert that
this code reproduces its own assumptions.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli.hussh_one_routing import corpus_build as C


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{args}: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    return root


def commit(repo, name, files, *, parent_of=None):
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD").strip()


def conflicting_merge(repo, base_text, ours_text, theirs_text, path="mod.py"):
    """Build a real conflicted merge and resolve it by taking theirs."""
    commit(repo, "base", {path: base_text})
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "theirs", {path: theirs_text})
    git(repo, "checkout", "-q", "main")
    commit(repo, "ours", {path: ours_text})
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-commit", "side"],
        capture_output=True, text=True, check=False,
    )
    assert merge.returncode != 0, "fixture did not actually conflict"
    (repo / path).write_text(theirs_text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "merge: take theirs")
    return git(repo, "rev-parse", "HEAD").strip()


BASE = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
OURS = "def f():\n    return 100\n\n\ndef g():\n    return 2\n"
THEIRS = "def f():\n    return 999\n\n\ndef g():\n    return 2\n"


class TestFindingMergesWorthLearningFrom:
    def test_a_conflicted_merge_is_found_with_its_paths(self, repo):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        found = C.conflicted_merges(repo, limit=20)
        assert len(found) == 1
        assert found[0][1] == ["mod.py"]

    def test_a_clean_merge_is_skipped(self, repo):
        # git resolved it, so it says nothing about whether a model could.
        commit(repo, "base", {"a.py": "x = 1\n"})
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, "theirs", {"b.py": "y = 2\n"})
        git(repo, "checkout", "-q", "main")
        commit(repo, "ours", {"c.py": "z = 3\n"})
        git(repo, "merge", "-q", "--no-ff", "-m", "clean", "side")
        assert C.conflicted_merges(repo, limit=20) == []

    def test_a_repo_with_no_merges_yields_nothing(self, repo):
        commit(repo, "only", {"a.py": "x = 1\n"})
        assert C.conflicted_merges(repo, limit=20) == []


class TestReplayReproducesGitsOwnConflict:
    def test_the_rebuilt_file_carries_real_markers(self, repo):
        merge = conflicting_merge(repo, BASE, OURS, THEIRS)
        rebuilt = C.rebuild_conflict(repo, merge, "mod.py")
        assert "<<<<<<<" in rebuilt and ">>>>>>>" in rebuilt
        assert "return 100" in rebuilt and "return 999" in rebuilt

    def test_both_sides_land_on_the_correct_side_of_the_marker(self, repo):
        merge = conflicting_merge(repo, BASE, OURS, THEIRS)
        cases = C.build(repo, limit=20, max_entries=5).entries
        entry = cases[0]
        assert "return 100" in entry.ours
        assert "return 999" in entry.theirs

    def test_a_file_added_on_only_one_side_is_not_a_content_conflict(self, repo):
        merge = conflicting_merge(repo, BASE, OURS, THEIRS)
        assert C.rebuild_conflict(repo, merge, "never-existed.py") is None

    def test_replay_leaves_the_working_tree_untouched(self, repo):
        # This runs on a machine whose tree is serving live traffic. A replay
        # that checks anything out would take the bridge down.
        merge = conflicting_merge(repo, BASE, OURS, THEIRS)
        before = git(repo, "status", "--porcelain")
        head_before = git(repo, "rev-parse", "HEAD")
        C.build(repo, limit=20, max_entries=5)
        assert git(repo, "status", "--porcelain") == before
        assert git(repo, "rev-parse", "HEAD") == head_before


class TestReferenceOnlyWhenItCanBeAttributed:
    def test_a_single_hunk_file_gets_the_shipped_side(self, repo):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        entries = C.build(repo, limit=20, max_entries=5).entries
        assert entries[0].hunks_in_file == 1
        assert entries[0].reference_side == "theirs"
        assert entries[0].has_reference is True

    def test_a_multi_hunk_file_is_judge_only(self, repo):
        # The merge commit shows the resolved result but not which hunk each
        # line came from. Attributing them would be the guesswork this corpus
        # exists to avoid.
        base = "a = 0\n\n\nb = 0\n"
        ours = "a = 1\n\n\nb = 1\n"
        theirs = "a = 2\n\n\nb = 2\n"
        conflicting_merge(repo, base, ours, theirs)
        entries = C.build(repo, limit=20, max_entries=5).entries
        assert entries, "no cases extracted"
        assert entries[0].hunks_in_file == 2
        assert entries[0].reference_side == ""
        assert entries[0].has_reference is False


class TestWhatIsPassedOverIsCounted:
    def test_a_non_source_file_is_skipped_by_name(self, repo):
        conflicting_merge(repo, BASE, OURS, THEIRS, path="thing.bin")
        report = C.build(repo, limit=20, max_entries=5)
        assert report.entries == []
        assert report.skipped["not-a-text-source"] == 1

    def test_an_enormous_hunk_is_skipped_as_a_rewrite(self, repo):
        # Grading a whole-file rewrite measures context length, not merge
        # reasoning.
        base = "".join(f"x{i} = 0\n" for i in range(200))
        ours = "".join(f"x{i} = 1\n" for i in range(200))
        theirs = "".join(f"x{i} = 2\n" for i in range(200))
        conflicting_merge(repo, base, ours, theirs)
        report = C.build(repo, limit=20, max_entries=5)
        assert report.skipped.get("hunk-too-large", 0) >= 1
        assert report.entries == []

    def test_the_scan_stops_at_max_entries(self, repo):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        assert len(C.build(repo, limit=20, max_entries=1).entries) <= 1


class TestTheCorpusReportsWhatItCannotMeasure:
    def _entry(self, side):
        return C.CorpusEntry(
            case_id="x", path="a.py", merge_sha="deadbeef",
            conflicted_text="", reference_text="", pre="", ours="", theirs="",
            post="", reference_side=side,
        )

    def test_an_absent_keep_ours_case_is_named_as_a_gap(self):
        # The real corpus has zero of these: merges here are a branch landing
        # on main, where the resolution takes the branch. A model that silently
        # discards fork behaviour would score clean, and for an upstream sync
        # that is the expensive failure.
        gaps = C.coverage_gaps([self._entry("theirs")] * 40)
        assert any(g["missing_reference_side"] == "ours" for g in gaps)
        assert not any(g["missing_reference_side"] == "theirs" for g in gaps)

    def test_a_thin_reference_set_is_flagged_even_when_both_sides_appear(self):
        entries = [self._entry("ours"), self._entry("theirs")]
        gaps = C.coverage_gaps(entries)
        assert any("too few" in g["consequence"] for g in gaps)

    def test_a_balanced_and_large_corpus_reports_no_gaps(self):
        entries = [self._entry("ours")] * 20 + [self._entry("theirs")] * 20
        assert C.coverage_gaps(entries) == []

    def test_the_gaps_ride_along_in_the_frozen_manifest(self, repo, tmp_path):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        report = C.build(repo, limit=20, max_entries=5)
        manifest = C.freeze(report, tmp_path / "corpus.json")
        assert "coverage_gaps" in manifest
        assert any(
            g["missing_reference_side"] == "ours" for g in manifest["coverage_gaps"]
        )


class TestFreezingStatesItsOwnLimits:
    def test_the_manifest_separates_reference_from_judge_only(self, repo, tmp_path):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        report = C.build(repo, limit=20, max_entries=5)
        manifest = C.freeze(report, tmp_path / "corpus.json")
        assert manifest["with_reference"] + manifest["judge_only"] == manifest["cases"]
        # Reporting judge-only cases inside a deterministic rate would claim a
        # ground truth the corpus does not have.
        assert "judge-only" in manifest["caveat"]

    def test_a_frozen_corpus_reloads_identically(self, repo, tmp_path):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        report = C.build(repo, limit=20, max_entries=5)
        path = tmp_path / "corpus.json"
        C.freeze(report, path)
        entries, manifest = C.load(path)
        assert len(entries) == len(report.entries)
        assert entries[0].ours == report.entries[0].ours
        assert entries[0].reference_side == report.entries[0].reference_side
        assert manifest["cases"] == len(entries)

    def test_frozen_cases_grade_without_the_repo(self, repo, tmp_path):
        # The corpus has to outlive the worktree it came from.
        conflicting_merge(repo, BASE, OURS, THEIRS)
        report = C.build(repo, limit=20, max_entries=5)
        path = tmp_path / "corpus.json"
        C.freeze(report, path)
        entries, _ = C.load(path)
        cases = C.to_cases(entries)
        from hermes_cli.hussh_one_routing.suites import merge_conflict as M

        verdict = M.grade(
            entries[0].theirs, cases[0], entries[0].conflicted_text
        )
        assert verdict.deterministically_ok is True
        assert verdict.reference_match is True

    def test_the_manifest_is_json_serialisable(self, repo, tmp_path):
        conflicting_merge(repo, BASE, OURS, THEIRS)
        report = C.build(repo, limit=20, max_entries=5)
        path = tmp_path / "corpus.json"
        C.freeze(report, path)
        json.loads(path.read_text())
