# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The judge has god-mode, so every edit must at least be visible.

The judge lane holds Bash. It can read the manifest naming the planted rows,
rewrite the queue it is grading, append verdicts around the validating writer,
revise verdicts it already gave, and edit the rules it is judged against. None
of that is preventable at this layer.

What these tests establish is that each of those produces a VOID run rather than
a number. The difference between "the judge can cheat invisibly" and "the judge
can cheat and the result is discarded" is the entire value here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.hussh_one_pkm import integrity as I
from hermes_cli.hussh_one_pkm import judge_queue as Q


ROWS = [
    {"id": "c000", "utterance": "I stopped eating dairy.", "output": {"domain": "health"}},
    {"id": "c001", "utterance": "Aisle seat please.", "output": {"domain": "travel"}},
]
CONTROLS = ["c001"]


def _seal(rows=None, controls=None, package_dir=None):
    return I.seal_run(
        run_id="r",
        rows=rows or ROWS,
        control_ids=controls or CONTROLS,
        package_dir=package_dir,
        salt="fixed-salt",
    )


def _verify(seal, rows=None, controls=None, verdicts=(), **kw):
    return I.verify(
        seal=seal,
        rows=rows if rows is not None else ROWS,
        control_ids=controls if controls is not None else CONTROLS,
        verdicts=verdicts,
        **kw,
    )


class TestAnUnsealedRunIsNotTrusted:
    def test_no_seal_is_the_loudest_finding_not_the_quietest(self):
        # An unsealed run is one where tampering is undetectable by
        # construction. It must not score as if it were verified.
        violations = _verify(None)
        assert [v.kind for v in violations] == ["no-seal"]

    def test_an_unreadable_seal_reads_as_absent(self, tmp_path):
        (tmp_path / "bad.seal.json").write_text("not json")
        assert I.read_seal(tmp_path / "bad.seal.json") is None


class TestEvidenceTampering:
    def test_editing_a_row_after_issue_is_caught(self):
        edited = [dict(ROWS[0], output={"domain": "finance"}), ROWS[1]]
        kinds = [v.kind for v in _verify(_seal(), rows=edited)]
        assert "row-altered" in kinds

    def test_adding_a_row_is_caught(self):
        extra = ROWS + [{"id": "c999", "utterance": "x", "output": {}}]
        assert "row-added" in [v.kind for v in _verify(_seal(), rows=extra)]

    def test_removing_a_row_is_caught(self):
        assert "row-removed" in [v.kind for v in _verify(_seal(), rows=ROWS[:1])]

    def test_reordering_rows_is_not_tampering(self):
        # Rows are keyed by id, so ordinary handling must not void a run.
        assert _verify(_seal(), rows=list(reversed(ROWS))) == []


class TestTheAnswerKeyIsNotInTheSeal:
    def test_the_seal_does_not_say_which_rows_are_planted(self):
        # It lists every row id, because it fingerprints every row. What it must
        # never do is distinguish the planted ones: a plain list would make the
        # seal an answer key, and anyone finding it would know what to catch.
        data = _seal().to_dict()
        assert "control_commitment" in data
        assert "controls" not in data
        assert "control_ids" not in data
        # Both a control and a non-control appear, and nothing tells them apart.
        assert set(data["row_hashes"]) == {"c000", "c001"}

    def test_the_commitment_reveals_nothing_about_its_members(self):
        # Two different control sets over the same rows produce unrelated
        # commitments, and neither contains a row id.
        one = I.control_commitment(["c000"], "salt")
        two = I.control_commitment(["c001"], "salt")
        assert one != two
        assert "c000" not in one and "c001" not in two

    def test_changing_which_rows_are_planted_is_caught(self):
        assert "controls-altered" in [
            v.kind for v in _verify(_seal(), controls=["c000"])
        ]


class TestHarnessTampering:
    def test_rewriting_the_rules_voids_the_run(self, tmp_path):
        # A judge that edits judge.py is not grading against the contract any
        # more, it is grading against whatever it just wrote.
        fake = tmp_path / "pkg"
        fake.mkdir()
        for name in I.SEALED_SOURCES:
            (fake / name).write_text("original")
        seal = _seal(package_dir=fake)

        (fake / "judge.py").write_text("rewritten by the judge")
        kinds = [v.kind for v in _verify(seal, package_dir=fake)]
        assert "harness-altered" in kinds

    def test_deleting_a_harness_file_voids_the_run(self, tmp_path):
        fake = tmp_path / "pkg"
        fake.mkdir()
        for name in I.SEALED_SOURCES:
            (fake / name).write_text("original")
        seal = _seal(package_dir=fake)

        (fake / "integrity.py").unlink()
        assert "harness-altered" in [
            v.kind for v in _verify(seal, package_dir=fake)
        ]

    def test_an_untouched_harness_passes(self, tmp_path):
        fake = tmp_path / "pkg"
        fake.mkdir()
        for name in I.SEALED_SOURCES:
            (fake / name).write_text("original")
        assert _verify(_seal(package_dir=fake), package_dir=fake) == []


class TestInventedRules:
    def test_a_rule_the_contract_does_not_define_is_caught(self):
        # Improvising a rule produces a failure that looks fully compliant --
        # cited, well-formed -- while grading against a standard nobody agreed
        # to.
        verdicts = [{"id": "c000", "verdict": "wrong", "rule": "vibes", "citation": "x"}]
        assert "invented-rule" in [
            v.kind for v in _verify(_seal(), verdicts=verdicts)
        ]

    def test_every_contract_rule_is_accepted(self):
        for rule in I.KNOWN_RULES:
            verdicts = [
                {"id": "c000", "verdict": "wrong", "rule": rule, "citation": "x"}
            ]
            assert _verify(_seal(), verdicts=verdicts) == []

    def test_a_correct_verdict_needs_no_rule(self):
        verdicts = [{"id": "c000", "verdict": "correct", "rule": "", "citation": ""}]
        assert _verify(_seal(), verdicts=verdicts) == []


class TestTheSealMustLiveOutsideTheRun:
    def test_a_seal_inside_the_run_directory_is_itself_a_violation(self, tmp_path):
        # Inside, it is one more file the judge can regenerate, so it protects
        # nothing and must not be mistaken for protection.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        inside = run_dir / "r.seal.json"
        kinds = [
            v.kind
            for v in _verify(_seal(), run_dir=run_dir, seal_path=inside)
        ]
        assert "seal-inside-run" in kinds

    def test_a_seal_beside_the_run_directory_is_accepted(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        outside = tmp_path / "r.seal.json"
        assert _verify(_seal(), run_dir=run_dir, seal_path=outside) == []

    def test_write_queue_puts_the_seal_outside_by_default(self, tmp_path):
        run_dir = tmp_path / "run"
        queued = Q.write_queue(
            out_dir=run_dir,
            cases=[{"utterance": "u", "output": {"domain": "d"}}],
            answerer_model="m",
            run_id="r",
        )
        assert queued.seal_path is not None
        assert run_dir not in queued.seal_path.parents

    def test_the_instructions_never_mention_the_seal(self, tmp_path):
        # Handing the grader its path defeats the whole mechanism.
        queued = Q.write_queue(
            out_dir=tmp_path / "run",
            cases=[{"utterance": "u", "output": {"domain": "d"}}],
            answerer_model="m",
            run_id="r",
        )
        text = queued.instructions()
        assert "seal" not in text.lower()
        assert str(queued.seal_path) not in text


class TestVerdictChain:
    def test_rewriting_an_earlier_verdict_changes_the_chain(self):
        # A judge cannot revise a verdict it already gave and leave the file
        # looking untouched.
        original = [
            {"id": "a", "verdict": "wrong", "rule": "no-invention", "citation": "q"},
            {"id": "b", "verdict": "correct"},
        ]
        revised = [dict(original[0], verdict="correct"), original[1]]
        assert I.verdict_chain(original, "s") != I.verdict_chain(revised, "s")

    def test_reordering_verdicts_changes_the_chain(self):
        entries = [{"id": "a", "verdict": "correct"}, {"id": "b", "verdict": "unsure"}]
        assert I.verdict_chain(entries, "s") != I.verdict_chain(
            list(reversed(entries)), "s"
        )

    def test_an_identical_sequence_reproduces_the_chain(self):
        entries = [{"id": "a", "verdict": "correct"}]
        assert I.verdict_chain(entries, "s") == I.verdict_chain(entries, "s")


class TestIngestVoidsOnTampering:
    def _run(self, tmp_path):
        return Q.write_queue(
            out_dir=tmp_path / "run",
            cases=[{"utterance": "I stopped eating dairy.", "output": {"domain": "health"}}],
            answerer_model="m",
            run_id="r",
        )

    def _grade_all(self, queued, verdict="correct", **extra):
        lines = []
        for line in queued.queue_path.read_text().splitlines():
            row = json.loads(line)
            lines.append(json.dumps({"id": row["id"], "verdict": verdict, **extra}))
        (queued.queue_path.parent / Q.VERDICTS_FILENAME).write_text(
            "\n".join(lines) + "\n"
        )

    def test_an_edited_queue_voids_before_any_scoring(self, tmp_path):
        queued = self._run(tmp_path)
        self._grade_all(queued)
        rows = [json.loads(l) for l in queued.queue_path.read_text().splitlines()]
        rows[0]["output"] = {"domain": "rewritten"}
        queued.queue_path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
        )

        report = Q.ingest(out_dir=tmp_path / "run")
        assert report.void is True
        # Two checks cover this: the in-queue hash, which is cheap but which the
        # judge could recompute, and the seal, which it cannot without finding a
        # file it was never given. Either firing is correct; publishing a number
        # is not.
        assert "changed between issue and ingest" in report.void_reason or (
            "integrity check failed" in report.void_reason
        )
        assert report.scoreboard()["accuracy"] is None

    def test_a_full_in_run_tamper_is_still_caught_by_the_seal(self, tmp_path):
        # Everything inside the run directory is writable by the judge: the
        # queue, its per-row hash, AND the manifest holding the expected hashes.
        # A judge that updates all three defeats every in-run check. The seal
        # lives outside and is the only thing left standing.
        queued = self._run(tmp_path)
        self._grade_all(queued)

        rows = [json.loads(l) for l in queued.queue_path.read_text().splitlines()]
        rows[0]["output"] = {"domain": "rewritten"}
        rows[0]["hash"] = Q._row_hash(rows[0])
        queued.queue_path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
        )
        manifest = json.loads(queued.manifest_path.read_text())
        manifest["hashes"][rows[0]["id"]] = rows[0]["hash"]
        queued.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

        report = Q.ingest(out_dir=tmp_path / "run")
        assert report.void is True
        assert "integrity check failed" in report.void_reason
        assert "row-altered" in report.void_reason

    def test_a_deleted_seal_voids_the_run(self, tmp_path):
        queued = self._run(tmp_path)
        self._grade_all(queued)
        queued.seal_path.unlink()

        report = Q.ingest(out_dir=tmp_path / "run")
        assert report.void is True
        assert "no-seal" in report.void_reason

    def test_an_untampered_run_reaches_scoring(self, tmp_path):
        queued = self._run(tmp_path)
        self._grade_all(queued)
        report = Q.ingest(out_dir=tmp_path / "run")
        # It gets past integrity and fails on the controls instead, which is
        # the next gate rather than this one.
        assert "integrity check failed" not in report.void_reason


class TestRulesAreScopedToTheSuite:
    """A flat rule set voids every non-PKM run on its first real finding.

    A merge judge citing `kept-wrong-side` would be recorded as having invented
    a rule, `verify` would raise `invented-rule`, and `ingest` treats any
    violation as void. The vocabulary is a property of what is being graded.
    """

    def _verdict(self, rule):
        return [{"id": "c000", "verdict": "wrong", "rule": rule, "citation": "x"}]

    def test_a_merge_rule_is_valid_in_the_merge_suite(self):
        assert (
            _verify(_seal(), verdicts=self._verdict("kept-wrong-side"), suite="merge")
            == []
        )

    def test_a_code_edit_rule_is_valid_in_the_code_edit_suite(self):
        assert (
            _verify(
                _seal(),
                verdicts=self._verdict("collateral-change"),
                suite="code_edit",
            )
            == []
        )

    def test_a_merge_rule_is_rejected_inside_the_pkm_suite(self):
        # Scoping cuts both ways, or it is not scoping.
        kinds = [
            v.kind
            for v in _verify(
                _seal(), verdicts=self._verdict("kept-wrong-side"), suite="pkm"
            )
        ]
        assert "invented-rule" in kinds

    def test_a_genuinely_invented_rule_is_still_caught_in_every_suite(self):
        for suite in ("pkm", "merge", "code_edit"):
            kinds = [
                v.kind
                for v in _verify(_seal(), verdicts=self._verdict("vibes"), suite=suite)
            ]
            assert "invented-rule" in kinds, suite

    def test_an_unknown_suite_accepts_any_defined_rule_rather_than_voiding(self):
        # Permissive on purpose: voiding a run for citing a real rule that
        # belongs to a suite this code has not heard of punishes the run for
        # the harness being out of date.
        assert (
            _verify(
                _seal(),
                verdicts=self._verdict("kept-wrong-side"),
                suite="something-new",
            )
            == []
        )

    def test_the_suite_is_recorded_at_issue_time(self, tmp_path):
        # Recorded in the manifest so ingest cannot later be argued into a more
        # permissive vocabulary than the run was issued under.
        queued = Q.write_queue(
            out_dir=tmp_path / "run",
            cases=[{"utterance": "u", "output": {"domain": "d"}}],
            answerer_model="m",
            run_id="r",
            suite="merge",
        )
        manifest = json.loads(queued.manifest_path.read_text())
        assert manifest["suite"] == "merge"

    def test_pkm_remains_the_default_for_existing_callers(self, tmp_path):
        queued = Q.write_queue(
            out_dir=tmp_path / "run",
            cases=[{"utterance": "u", "output": {"domain": "d"}}],
            answerer_model="m",
            run_id="r",
        )
        assert json.loads(queued.manifest_path.read_text())["suite"] == "pkm"


class TestGradingLogicOutsideThePackageIsSealed:
    def test_the_write_guard_and_routing_modules_are_hashed(self):
        # An oracle a judge could edit undetected is a rule it can rewrite
        # mid-run, which is what sealing exists to prevent.
        hashes = I.source_hashes()
        for rel in I.SEALED_REPO_SOURCES:
            assert rel in hashes, rel
            assert hashes[rel] != "<missing>", rel

    def test_a_package_dir_override_does_not_fold_in_real_repo_files(self, tmp_path):
        # `package_dir` means "hash this directory". A test pointing at a temp
        # dir must not have its seal depend on files it never wrote.
        for name in I.SEALED_SOURCES:
            (tmp_path / name).write_text("x")
        hashes = I.source_hashes(tmp_path)
        assert set(hashes) == set(I.SEALED_SOURCES)
