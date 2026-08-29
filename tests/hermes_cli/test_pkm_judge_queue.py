# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The filesystem judge handoff.

A queue on disk can be gamed in ways an API call cannot: rows edited between
issue and ingest, hard rows skipped, controls spotted because they always sit
in the same place. Every test here is one of those.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.hussh_one_pkm import judge_queue as Q
from hermes_cli.hussh_one_pkm import judge as J


CASES = [
    {
        "utterance": "I stopped eating dairy in January.",
        "output": {
            "domain": "health",
            "scope_path": "health.diet.restrictions",
            "merge_patch": {"dairy": "avoided"},
            "summary": "Stopped eating dairy.",
        },
    },
    {
        "utterance": "Always book me an aisle seat.",
        "output": {
            "domain": "travel",
            "scope_path": "travel.preferences.seat",
            "merge_patch": {"seat": "aisle"},
            "summary": "Prefers an aisle seat.",
        },
    },
]


def _write(tmp_path: Path, **kwargs):
    return Q.write_queue(
        out_dir=tmp_path, cases=CASES, answerer_model="gemma-test", **kwargs
    )


def _rows(queued):
    return [json.loads(l) for l in queued.queue_path.read_text().splitlines() if l.strip()]


def _grade(queued, verdict_for):
    """Write verdicts using a callable that sees each row."""
    lines = []
    for row in _rows(queued):
        lines.append(json.dumps({"id": row["id"], **verdict_for(row)}))
    (queued.queue_path.parent / Q.VERDICTS_FILENAME).write_text("\n".join(lines) + "\n")


def _catches_controls(row):
    """A conscientious grader: flags the four planted failures, passes the rest."""
    out = json.dumps(row["output"])
    if "finance.accounts.primary" in out:
        return {"verdict": "wrong", "rule": "right-domain", "citation": "finance"}
    if "AA-4471182" in out:
        return {"verdict": "wrong", "rule": "no-invention", "citation": "AA-4471182"}
    if "call the dentist" in out:
        return {"verdict": "wrong", "rule": "durable-only", "citation": "call the dentist"}
    if "_parser_version" in out:
        return {"verdict": "wrong", "rule": "no-metadata", "citation": "_parser_version"}
    return {"verdict": "correct", "rule": "", "citation": ""}


class TestTheQueueHidesItsAnswers:
    def test_controls_are_indistinguishable_in_the_queue_file(self, tmp_path):
        queued = _write(tmp_path)
        raw = queued.queue_path.read_text()
        # No marking of any kind. A grader working from this file alone cannot
        # tell a planted failure from a real output.
        for marker in ("control", "must_catch", "planted", "is_control", "expected"):
            assert marker not in raw

    def test_the_answers_live_in_the_manifest_not_the_queue(self, tmp_path):
        queued = _write(tmp_path)
        manifest = json.loads(queued.manifest_path.read_text())
        planted = len(manifest["controls"]) + len(manifest["clean_controls"])
        assert planted == queued.control_count
        # Ingest reads them; a grader given only the queue path cannot.
        raw = queued.queue_path.read_text()
        assert "controls" not in raw
        assert "must_not_flag" not in raw

    def test_control_positions_differ_between_runs(self, tmp_path):
        # If traps always sat in the same slots, a grader would learn the slots
        # instead of reading the rows.
        a = Q.write_queue(
            out_dir=tmp_path / "a", cases=CASES, answerer_model="m", run_id="run-a"
        )
        b = Q.write_queue(
            out_dir=tmp_path / "b", cases=CASES, answerer_model="m", run_id="run-b"
        )
        controls_a = set(json.loads(a.manifest_path.read_text())["controls"])
        order_a = [r["id"] for r in _rows(a)]
        order_b = [r["id"] for r in _rows(b)]
        assert controls_a  # there are controls
        assert order_a != order_b or len(order_a) < 3

    def test_the_same_run_id_reshuffles_identically(self, tmp_path):
        # Reproducible from the manifest, so an old run can be re-graded.
        a = Q.write_queue(
            out_dir=tmp_path / "a", cases=CASES, answerer_model="m", run_id="fixed"
        )
        b = Q.write_queue(
            out_dir=tmp_path / "b", cases=CASES, answerer_model="m", run_id="fixed"
        )
        assert [r["id"] for r in _rows(a)] == [r["id"] for r in _rows(b)]


class TestEvidenceCannotChangeUnderneath:
    def test_an_edited_row_voids_the_run(self, tmp_path):
        queued = _write(tmp_path)
        _grade(queued, _catches_controls)

        # Someone edits the output after it was issued, so the verdict no
        # longer describes what was actually produced.
        rows = _rows(queued)
        rows[0]["output"] = {"domain": "rewritten", "scope_path": "x"}
        queued.queue_path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
        )

        report = Q.ingest(out_dir=tmp_path)
        assert report.void is True
        assert "changed between issue and ingest" in report.void_reason

    def test_reordering_the_queue_is_not_tampering(self, tmp_path):
        # The hash covers what is graded, not where it sits, so a reordered
        # file still ingests. Otherwise routine handling would void runs.
        queued = _write(tmp_path)
        _grade(queued, _catches_controls)
        rows = list(reversed(_rows(queued)))
        queued.queue_path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
        )
        assert Q.ingest(out_dir=tmp_path).void is False


class TestPartialGradingIsNotAPass:
    def test_skipping_rows_voids_the_run(self, tmp_path):
        # Otherwise a grader raises accuracy by skipping whatever it found hard.
        queued = _write(tmp_path)
        rows = _rows(queued)
        (tmp_path / Q.VERDICTS_FILENAME).write_text(
            json.dumps({"id": rows[0]["id"], "verdict": "correct"}) + "\n"
        )
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is True
        assert "were not graded" in report.void_reason

    def test_no_verdicts_at_all_voids_rather_than_scoring_zero(self, tmp_path):
        queued = _write(tmp_path)
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is True
        assert report.scoreboard()["accuracy"] is None


class TestControlsStillGateTheRun:
    def test_a_grader_that_passes_everything_voids_the_run(self, tmp_path):
        queued = _write(tmp_path)
        _grade(queued, lambda _row: {"verdict": "correct"})
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is True
        assert "planted failures" in report.void_reason

    def test_a_conscientious_grader_produces_a_valid_run(self, tmp_path):
        queued = _write(tmp_path)
        _grade(queued, _catches_controls)
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is False
        board = report.scoreboard()
        assert board["graded"] == len(CASES)
        assert board["accuracy"] == 1.0

    def test_uncited_failures_are_still_discarded(self, tmp_path):
        queued = _write(tmp_path)

        def _grader(row):
            base = _catches_controls(row)
            if base["verdict"] == "correct":
                return {
                    "verdict": "wrong",
                    "rule": "no-invention",
                    "citation": "text that is nowhere in the output",
                }
            return base

        _grade(queued, _grader)
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is False
        assert report.scoreboard()["discarded_uncited"] == len(CASES)


class TestEvolutionLedger:
    def test_a_void_run_is_still_recorded(self, tmp_path):
        # Dropping void runs would make the ledger an unbroken record of
        # successes, which is the opposite of what it is for.
        report = J.JudgeReport(judge_model="claude-code", answerer_model="m")
        report.void = True
        report.void_reason = "grader missed a control"
        Q.append_to_ledger(ledger_path=tmp_path / "l.jsonl", report=report, timestamp=1)
        rows = Q.read_ledger(tmp_path / "l.jsonl")
        assert len(rows) == 1
        assert rows[0]["void"] is True

    def test_runs_with_different_probe_modes_are_not_compared(self, tmp_path):
        # A model tested through tool calling and one tested through JSON mode
        # were not asked the same question; a delta between them is invented.
        ledger = tmp_path / "l.jsonl"
        for mode, accuracy, when in [("tools", 0.5, 1), ("json", 0.9, 2)]:
            report = J.JudgeReport(judge_model="claude-code", answerer_model="m")
            report.cases = [
                J.GradedCase(
                    case_id="a",
                    model="m",
                    verdict=J.VERDICT_CORRECT if accuracy > 0.7 else J.VERDICT_WRONG,
                )
            ]
            Q.append_to_ledger(
                ledger_path=ledger,
                report=report,
                capability_profile={"probe_mode": mode},
                timestamp=when,
            )
        result = Q.compare_runs(ledger)
        assert result["comparable"] is False
        assert "probe mode changed" in result["reason"]

    def test_same_probe_mode_runs_compare(self, tmp_path):
        ledger = tmp_path / "l.jsonl"
        for correct, when in [(False, 1), (True, 2)]:
            report = J.JudgeReport(judge_model="claude-code", answerer_model="m")
            report.cases = [
                J.GradedCase(
                    case_id="a",
                    model="m",
                    verdict=J.VERDICT_CORRECT if correct else J.VERDICT_WRONG,
                )
            ]
            Q.append_to_ledger(
                ledger_path=ledger,
                report=report,
                capability_profile={"probe_mode": "tools"},
                timestamp=when,
            )
        result = Q.compare_runs(ledger)
        assert result["comparable"] is True
        assert result["delta"] == 1.0

    def test_a_missing_ledger_is_an_empty_history_not_an_error(self, tmp_path):
        assert Q.read_ledger(tmp_path / "nope.jsonl") == []
        assert Q.compare_runs(tmp_path / "nope.jsonl")["comparable"] is False


class TestContextSeparationIsRecordedNotClaimed:
    def test_it_reports_that_it_cannot_enforce(self, tmp_path):
        # A session that wrote the queue remembers where the controls went.
        # There is no way to ask "are you the same context", so the honest
        # move is to record the limitation, not to pretend it was checked.
        queued = _write(tmp_path)
        fact = Q.assert_fresh_context(queued.manifest_path)
        assert fact["enforced"] is False
        assert "discipline" in fact["note"]


class TestInstructions:
    def test_they_do_not_reveal_the_controls(self, tmp_path):
        queued = _write(tmp_path)
        text = queued.instructions()
        assert "planted failures" in text  # says they exist
        for control in J.NEGATIVE_CONTROLS:
            # ...but never which rows, nor what they contain.
            assert control["must_catch"] not in text
            assert control["utterance"] not in text

    def test_they_require_a_rule_and_a_citation(self, tmp_path):
        text = _write(tmp_path).instructions()
        assert "REQUIRES a rule and a citation" in text

    def test_they_point_at_the_sanctioned_writer_not_a_shell_redirect(self, tmp_path):
        # The judge lane has no Write tool. A raw redirect would be an
        # unvalidated write laundered past a read-only declaration.
        text = _write(tmp_path).instructions()
        assert "verdict_cli" in text
        # No redirect into the verdicts file anywhere in the instructions.
        assert f"> {Q.VERDICTS_FILENAME}" not in text
        assert f">{Q.VERDICTS_FILENAME}" not in text
        assert "Write one JSON object per row to" not in text


class TestFalsePositivesAreCaught:
    """Negative controls alone catch a rubber-stamper and nothing else.

    A judge told to hunt for planted failures can flag every correct row and
    sail through a negative-only gate, its noise reading as diligence. The
    design had no false-positive rate at all until these rows existed.
    """

    def test_a_judge_that_flags_everything_voids_the_run(self, tmp_path):
        queued = _write(tmp_path)
        _grade(
            queued,
            lambda row: {
                "verdict": "wrong",
                "rule": "no-invention",
                # Cite something genuinely present so the citation check passes
                # and only the false-positive gate can catch this.
                "citation": str(row["output"].get("domain", "")),
            },
        )
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is True
        assert "flagged known-good outputs" in report.void_reason

    def test_clean_controls_never_enter_the_score(self, tmp_path):
        queued = _write(tmp_path)
        _grade(queued, _catches_controls)
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is False
        # Only real cases are graded; both kinds of control sit outside.
        assert report.scoreboard()["graded"] == len(CASES)

    def test_rubber_stamping_is_named_before_over_flagging(self, tmp_path):
        # Passing a planted failure is the more fundamental break, so it wins.
        queued = _write(tmp_path)
        _grade(queued, lambda _row: {"verdict": "correct"})
        assert "planted failures" in Q.ingest(out_dir=tmp_path).void_reason


class TestOmissionFailuresCanBeCited:
    def test_a_citation_from_the_utterance_is_accepted(self, tmp_path):
        # An omission has nothing to quote in the output by definition: the
        # complaint IS that it is absent. Output-only checking would force these
        # to `unsure`, which counts against accuracy, training a judge away from
        # the one failure class that loses the owner's data.
        queued = _write(tmp_path)

        def _grader(row):
            base = _catches_controls(row)
            if base["verdict"] == "correct" and "dairy" in row["utterance"]:
                # "January" is in the utterance and absent from the output --
                # exactly the shape of an omission complaint.
                return {
                    "verdict": "wrong",
                    "rule": "minimal-patch",
                    "citation": "January",
                }
            return base

        _grade(queued, _grader)
        report = Q.ingest(out_dir=tmp_path)
        assert report.void is False
        assert report.scoreboard()["discarded_uncited"] == 0

    def test_a_citation_in_neither_place_is_still_discarded(self, tmp_path):
        queued = _write(tmp_path)

        def _grader(row):
            base = _catches_controls(row)
            if base["verdict"] == "correct":
                return {"verdict": "wrong", "rule": "x", "citation": "nowhere at all"}
            return base

        _grade(queued, _grader)
        assert Q.ingest(out_dir=tmp_path).scoreboard()["discarded_uncited"] == len(CASES)


class TestRowsAreVersioned:
    def test_every_row_carries_a_schema_version(self, tmp_path):
        # The row shape is what grows: multimodal input, agentic trajectories, a
        # second judge. A reader meeting an unknown version should say so rather
        # than misparse it as v1.
        for row in _rows(_write(tmp_path)):
            assert row["v"] == Q.SCHEMA_VERSION
