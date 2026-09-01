# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The sanctioned verdict writer.

It exists because the judge lane has no Write tool, so the only way to produce
verdicts was a raw shell redirect: unvalidated, able to clobber the file or the
queue being graded. These tests are about the constraints that make this a
narrow append instead of arbitrary filesystem access.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_pkm import judge_queue as Q
from hermes_cli.hussh_one_pkm import verdict_cli as V


CASES = [
    {
        "utterance": "I stopped eating dairy in January.",
        "output": {
            "domain": "health",
            "scope_path": "health.diet.restrictions",
            "merge_patch": {"dairy": "avoided"},
            "summary": "Stopped eating dairy.",
        },
    }
]


@pytest.fixture
def run_dir(tmp_path):
    Q.write_queue(out_dir=tmp_path, cases=CASES, answerer_model="m", run_id="r")
    return tmp_path


def _first_real_id(run_dir):
    """The id of a row we know the content of."""
    for line in (run_dir / Q.QUEUE_FILENAME).read_text().splitlines():
        row = json.loads(line)
        if "health.diet.restrictions" in json.dumps(row["output"]):
            return row["id"]
    raise AssertionError("fixture row not found")


class TestItValidatesAtWriteTime:
    def test_a_wrong_verdict_without_a_citation_is_rejected(self, run_dir):
        # Rejected where the judge can still fix it, rather than silently
        # discarded at ingest.
        with pytest.raises(V.VerdictRejected, match="quote the offending value"):
            V.record(
                run_dir=run_dir,
                row_id=_first_real_id(run_dir),
                verdict="wrong",
                rule="no-invention",
            )

    def test_a_wrong_verdict_without_a_rule_is_rejected(self, run_dir):
        with pytest.raises(V.VerdictRejected, match="name the rule"):
            V.record(
                run_dir=run_dir,
                row_id=_first_real_id(run_dir),
                verdict="wrong",
                citation="dairy",
            )

    def test_a_citation_that_is_really_present_is_accepted(self, run_dir):
        entry = V.record(
            run_dir=run_dir,
            row_id=_first_real_id(run_dir),
            verdict="wrong",
            rule="right-domain",
            citation="health.diet.restrictions",
        )
        assert entry["verdict"] == "wrong"

    def test_an_utterance_citation_is_accepted_for_an_omission(self, run_dir):
        # An omission has nothing to quote in the output by definition.
        entry = V.record(
            run_dir=run_dir,
            row_id=_first_real_id(run_dir),
            verdict="wrong",
            rule="minimal-patch",
            citation="January",
        )
        assert entry["citation"] == "January"

    def test_an_unknown_verdict_word_is_rejected(self, run_dir):
        with pytest.raises(V.VerdictRejected, match="verdict must be one of"):
            V.record(
                run_dir=run_dir, row_id=_first_real_id(run_dir), verdict="excellent"
            )


class TestItCannotGradeRowsThatDoNotExist:
    def test_a_hallucinated_id_is_rejected(self, run_dir):
        # Otherwise it becomes an ungraded row at ingest, voiding the run for a
        # reason that points at the wrong thing.
        with pytest.raises(V.VerdictRejected, match="not in the queue"):
            V.record(run_dir=run_dir, row_id="c999", verdict="correct")

    def test_a_missing_queue_is_rejected_rather_than_written_around(self, tmp_path):
        with pytest.raises(V.VerdictRejected, match="no queue"):
            V.record(run_dir=tmp_path, row_id="c000", verdict="correct")


class TestItIsAppendOnly:
    def test_a_second_verdict_for_one_row_is_refused(self, run_dir):
        row = _first_real_id(run_dir)
        V.record(run_dir=run_dir, row_id=row, verdict="correct")
        # A second verdict is either a mistake or an overwrite, and both should
        # be seen rather than resolved silently.
        with pytest.raises(V.VerdictRejected, match="already has a verdict"):
            V.record(run_dir=run_dir, row_id=row, verdict="wrong", rule="x",
                     citation="dairy")

    def test_earlier_verdicts_survive_later_ones(self, run_dir):
        rows = [
            json.loads(l)["id"]
            for l in (run_dir / Q.QUEUE_FILENAME).read_text().splitlines()
        ]
        V.record(run_dir=run_dir, row_id=rows[0], verdict="correct")
        V.record(run_dir=run_dir, row_id=rows[1], verdict="unsure")
        written = (run_dir / Q.VERDICTS_FILENAME).read_text().splitlines()
        assert len(written) == 2

    def test_it_writes_only_to_the_verdicts_file(self, run_dir):
        before = (run_dir / Q.QUEUE_FILENAME).read_text()
        manifest_before = (run_dir / Q.MANIFEST_FILENAME).read_text()
        V.record(run_dir=run_dir, row_id=_first_real_id(run_dir), verdict="correct")
        # It cannot touch the queue it is grading or the manifest holding the
        # answers.
        assert (run_dir / Q.QUEUE_FILENAME).read_text() == before
        assert (run_dir / Q.MANIFEST_FILENAME).read_text() == manifest_before


class TestProgress:
    def test_it_reports_what_is_still_ungraded(self, run_dir):
        state = V.progress(run_dir)
        assert state["complete"] is False
        assert state["graded"] == 0
        assert len(state["remaining"]) == state["total"]

    def test_it_reports_complete_once_every_row_is_graded(self, run_dir):
        for line in (run_dir / Q.QUEUE_FILENAME).read_text().splitlines():
            V.record(run_dir=run_dir, row_id=json.loads(line)["id"], verdict="unsure")
        assert V.progress(run_dir)["complete"] is True


class TestCli:
    def test_a_rejection_exits_non_zero(self, run_dir, capsys):
        code = V.main(
            ["--run-dir", str(run_dir), "record", "--id", "c999", "--verdict", "correct"]
        )
        # A judge that cannot tell a rejection from a success moves on
        # believing the row is graded.
        assert code == 2
        assert "rejected" in capsys.readouterr().err

    def test_a_good_record_exits_zero(self, run_dir):
        code = V.main(
            [
                "--run-dir",
                str(run_dir),
                "record",
                "--id",
                _first_real_id(run_dir),
                "--verdict",
                "correct",
            ]
        )
        assert code == 0

    def test_progress_exits_non_zero_while_rows_remain(self, run_dir):
        assert V.main(["--run-dir", str(run_dir), "progress"]) == 1
