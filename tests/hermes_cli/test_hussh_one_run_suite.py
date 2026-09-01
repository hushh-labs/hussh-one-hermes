# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The ladder driver, which had no test at all until three bugs were found in it.

Each class here corresponds to a defect that shipped because nothing imported
this module outside a scratch script.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_pkm import judge_queue as Q
from hermes_cli.hussh_one_routing import profile as P
from hermes_cli.hussh_one_routing import run_suite as R


class TestTheAuditorGuardActuallyImports:
    def test_a_local_judge_is_refused(self):
        # The original imported this from `integrity`, where it does not live,
        # so every call that passed a judge raised ImportError while the
        # docstring claimed the check was wired.
        from hermes_cli.hussh_one_pkm.judge import JudgeIsOnDevice

        with pytest.raises(JudgeIsOnDevice):
            R._assert_auditor_is_not_local("google/gemma-4-31b-qat")

    def test_a_remote_judge_passes(self):
        R._assert_auditor_is_not_local("claude-opus-5")

    def test_no_judge_is_not_an_error(self):
        R._assert_auditor_is_not_local(None)

    def test_the_provider_is_threaded_through(self):
        # The real signature checks the provider BEFORE guessing from the model
        # name, so a locally-served model with an innocuous name is still caught.
        from hermes_cli.hussh_one_pkm.judge import JudgeIsOnDevice

        with pytest.raises(JudgeIsOnDevice):
            R._assert_auditor_is_not_local("some-model", "lmstudio")

    def test_it_is_not_importable_from_integrity(self):
        # Pin the fact that produced the bug, so a future move is a failing test
        # rather than a silent ImportError behind a guard clause.
        import hermes_cli.hussh_one_pkm.integrity as integrity

        assert not hasattr(integrity, "assert_auditor_is_not_local")


class TestProbeModeReachesTheLedger:
    def _profile(self):
        prof = P.CapabilityProfile(schema_version=1, model="m")
        prof.recommended = {"reasoning_effort": "none", "max_tokens": 1200}
        return prof

    def test_to_dict_carries_probe_mode_when_told_the_suite(self):
        payload = self._profile().to_dict(suite_id="merge", output_protocol="region")
        assert payload["probe_mode"] == "merge/region/effort=none/max_tokens=1200"

    def test_to_dict_omits_it_rather_than_writing_none(self):
        # Writing None is what made compare_runs pass every pair: None != None
        # is False, so the check that exists to refuse unlike runs matched them.
        assert "probe_mode" not in self._profile().to_dict()

    def test_a_different_budget_is_a_different_probe_mode(self):
        a = self._profile()
        b = self._profile()
        b.recommended["max_tokens"] = 8000
        assert a.to_dict(suite_id="merge", output_protocol="region")[
            "probe_mode"
        ] != b.to_dict(suite_id="merge", output_protocol="region")["probe_mode"]


class TestCompareRunsRefusesWhatItCannotProve:
    def _row(self, probe_mode, accuracy=0.5):
        row = {
            "schema_version": Q.SCHEMA_VERSION,
            "at": 1,
            "answerer_model": "m",
            "judge": "claude-opus-5",
            "capability_profile": {},
            "host": {},
            "benchmark": {},
            "scoreboard": {"accuracy": accuracy, "graded": 10},
            "void": False,
            "void_reason": "",
        }
        if probe_mode is not None:
            row["capability_profile"]["probe_mode"] = probe_mode
        return row

    def _ledger(self, tmp_path, rows):
        path = tmp_path / "evolution-ledger.jsonl"
        path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
            encoding="utf-8",
        )
        return path

    def test_two_runs_with_no_probe_mode_are_not_comparable(self, tmp_path):
        # The exact shipped bug: both sides None, so the guard passed.
        path = self._ledger(tmp_path, [self._row(None), self._row(None)])
        verdict = Q.compare_runs(path)
        assert verdict["comparable"] is False
        assert "probe mode missing" in verdict["reason"]

    def test_one_missing_side_is_also_refused(self, tmp_path):
        path = self._ledger(tmp_path, [self._row("merge/region/a"), self._row(None)])
        assert Q.compare_runs(path)["comparable"] is False

    def test_matching_probe_modes_compare(self, tmp_path):
        path = self._ledger(
            tmp_path,
            [self._row("merge/region/a", 0.4), self._row("merge/region/a", 0.6)],
        )
        assert Q.compare_runs(path)["comparable"] is True

    def test_differing_probe_modes_are_refused(self, tmp_path):
        path = self._ledger(
            tmp_path, [self._row("merge/region/a"), self._row("merge/region/b")]
        )
        verdict = Q.compare_runs(path)
        assert verdict["comparable"] is False
        assert "probe mode changed" in verdict["reason"]


class TestTheLedgerHasAHome:
    def test_a_default_path_exists(self, monkeypatch, tmp_path):
        # No default existed, so every would-be caller had to invent a location
        # and none did. That is most of why nothing ever wrote to the ledger.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert Q.default_ledger_path().name == Q.LEDGER_FILENAME

    def test_append_without_a_path_uses_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        report = Q.JudgeReport(
            judge_model="claude-opus-5", answerer_model="m", cases=[]
        )
        Q.append_to_ledger(report=report, timestamp=1)
        assert Q.default_ledger_path().exists()

    def test_an_explicit_path_still_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        explicit = tmp_path / "nested" / "custom.jsonl"
        report = Q.JudgeReport(
            judge_model="claude-opus-5", answerer_model="m", cases=[]
        )
        Q.append_to_ledger(ledger_path=explicit, report=report, timestamp=1)
        assert explicit.exists()
        assert not (tmp_path / Q.LEDGER_FILENAME).exists()


class TestTheShipGate:
    def _summary(self, *, graded=40, ok=40, offered=40, indeterminate=0):
        return {
            "graded": graded, "deterministically_ok": ok,
            "offered": offered, "indeterminate": indeterminate,
        }

    def test_a_clean_high_scorer_ships(self):
        assert R.ship_gate(self._summary())["ship"] is True

    def test_below_threshold_does_not_ship(self):
        gate = R.ship_gate(self._summary(ok=30))
        assert gate["ship"] is False
        assert "below the" in gate["reason"]

    def test_too_few_cases_is_its_own_refusal(self):
        # A model that answered four cases correctly is not a 100% model.
        gate = R.ship_gate(self._summary(graded=4, ok=4, offered=4))
        assert gate["ship"] is False
        assert "small-sample" in gate["reason"]

    def test_indeterminate_turns_block_before_any_verdict(self):
        # A budget problem is not a statement about the model, so the gate must
        # not let a high score on the answerable third stand in for the whole.
        gate = R.ship_gate(self._summary(graded=40, ok=40, offered=60,
                                         indeterminate=20))
        assert gate["ship"] is False
        assert "budget" in gate["reason"]

    def test_the_rate_is_over_offered_not_graded(self):
        # 40 of 40 graded but 20 unanswered is not 100%.
        gate = R.ship_gate(self._summary(graded=40, ok=40, offered=60,
                                         indeterminate=20))
        assert gate["rate"] < 1.0

    def test_todays_best_measured_model_would_not_ship(self):
        # The merge ladder's best: 12 structurally valid of 20 real conflicts.
        # The gate has to say no to that, or it is decoration.
        gate = R.ship_gate(
            {"graded": 40, "deterministically_ok": 24, "offered": 40,
             "indeterminate": 0}
        )
        assert gate["ship"] is False


class TestRankingNeverLetsLatencyBuyAWin:
    def test_validity_outranks_speed(self):
        rows = R.rank(
            {
                "fast-broken": {
                    "graded": 10, "deterministically_ok": 2,
                    "indeterminate": 0, "reference_match": 1,
                    "latency_s": {"median": 3},
                },
                "slow-correct": {
                    "graded": 10, "deterministically_ok": 9,
                    "indeterminate": 0, "reference_match": 7,
                    "latency_s": {"median": 90},
                },
            }
        )
        assert rows[0]["model"] == "slow-correct"

    def test_latency_breaks_a_validity_tie(self):
        rows = R.rank(
            {
                "slower": {
                    "graded": 10, "deterministically_ok": 5,
                    "indeterminate": 0, "reference_match": 3,
                    "latency_s": {"median": 90},
                },
                "faster": {
                    "graded": 10, "deterministically_ok": 5,
                    "indeterminate": 0, "reference_match": 3,
                    "latency_s": {"median": 9},
                },
            }
        )
        assert rows[0]["model"] == "faster"

    def test_a_model_that_graded_nothing_does_not_rank_first(self):
        # 0/0 must not become a perfect score.
        rows = R.rank(
            {
                "nothing-graded": {
                    "graded": 0, "deterministically_ok": 0,
                    "indeterminate": 20, "reference_match": 0,
                },
                "real": {
                    "graded": 10, "deterministically_ok": 4,
                    "indeterminate": 0, "reference_match": 2,
                    "latency_s": {"median": 30},
                },
            }
        )
        assert rows[0]["model"] == "real"
