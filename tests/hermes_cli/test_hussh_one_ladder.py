# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Walking the model ladder so the rungs are comparable.

The failure these prevent is subtle and would be invisible in the output: a
rung measured while the previous model's weights are still resident runs on a
different machine than its neighbours, and the latency difference lands in the
routing table as a property of the model.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing import ladder as L
from hermes_cli.hussh_one_routing.request import Turn


class _Host:
    """A fake LM Studio whose residency can be scripted."""

    def __init__(self, resident=(), stubborn=()):
        self._resident = [{"identifier": m} for m in resident]
        self._stubborn = set(stubborn)
        self.unload_calls = []

    def resident(self):
        return list(self._resident)

    def unload(self, identifier):
        self.unload_calls.append(identifier)
        if identifier in self._stubborn:
            return False
        self._resident = [
            e for e in self._resident if e.get("identifier") != identifier
        ]
        return True


class TestDrainIsVerifiedNotAssumed:
    def test_it_unloads_everything_and_confirms_empty(self):
        host = _Host(resident=["a", "b"])
        result = L.drain(unload=host.unload, resident=host.resident)
        assert result["empty"] is True
        assert sorted(result["unloaded"]) == ["a", "b"]

    def test_a_model_that_refuses_to_unload_is_reported_not_ignored(self):
        # unload_model returning True means the request was accepted, not that
        # the weights are gone. Trusting it is how a rung gets measured on a
        # machine still holding the previous model.
        host = _Host(resident=["stuck"], stubborn=["stuck"])
        result = L.drain(unload=host.unload, resident=host.resident)
        assert result["empty"] is False
        assert result["still_resident"] == ["stuck"]

    def test_an_already_empty_host_needs_no_unloads(self):
        host = _Host(resident=[])
        result = L.drain(unload=host.unload, resident=host.resident)
        assert result["empty"] is True
        assert host.unload_calls == []

    def test_a_raising_unload_does_not_abort_the_drain(self):
        def _explode(_identifier):
            raise RuntimeError("lms is unhappy")

        host = _Host(resident=["a"])
        result = L.drain(unload=_explode, resident=host.resident)
        assert result["empty"] is False


class TestARungIsNeverMeasuredOnADirtyMachine:
    def test_a_failed_drain_skips_the_rung_rather_than_running_it(self):
        host = _Host(resident=["stuck"], stubborn=["stuck"])
        ran = []
        result = L.walk(
            models=["m1"],
            suite_id="code",
            cases=["c1"],
            run_case=lambda m, c: ran.append((m, c)) or Turn(model=m, ok=True),
            unload=host.unload,
            resident=host.resident,
        )
        assert ran == []
        rung = result["rungs"][0]
        assert "could not drain" in rung["load_error"]
        assert rung["turns"] == []

    def test_a_clean_drain_lets_the_rung_run(self):
        host = _Host(resident=["old-model"])
        ran = []
        L.walk(
            models=["m1"],
            suite_id="code",
            cases=["c1", "c2"],
            run_case=lambda m, c: ran.append(c) or Turn(model=m, ok=True),
            unload=host.unload,
            resident=host.resident,
        )
        assert ran == ["c1", "c2"]
        # The model that was resident at the start got no warm head start.
        assert "old-model" in host.unload_calls


class TestOrderIsCounterbalanced:
    def test_each_rep_rotates_the_order(self):
        models = ["a", "b", "c"]
        assert L.counterbalanced_order(models, 0) == ["a", "b", "c"]
        assert L.counterbalanced_order(models, 1) == ["b", "c", "a"]
        assert L.counterbalanced_order(models, 2) == ["c", "a", "b"]

    def test_rotation_wraps_past_the_model_count(self):
        assert L.counterbalanced_order(["a", "b"], 5) == ["b", "a"]

    def test_an_empty_ladder_does_not_crash(self):
        assert L.counterbalanced_order([], 3) == []

    def test_no_model_is_always_measured_first(self):
        # A fixed order means one model is always measured cold and another
        # always hot, which is indistinguishable from a model difference.
        models = ["a", "b", "c"]
        firsts = {L.counterbalanced_order(models, rep)[0] for rep in range(3)}
        assert firsts == set(models)


class TestContextIsPinnedNotInherited:
    def test_the_rung_is_loaded_at_the_pinned_context(self):
        loaded = []

        def _load(model, ctx):
            loaded.append((model, ctx))
            return ctx

        L.walk(
            models=["m1", "m2"],
            suite_id="merge",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
            load=_load,
            context_length=131072,
        )
        assert loaded == [("m1", 131072), ("m2", 131072)]

    def test_a_server_that_clamps_the_context_fails_the_rung(self):
        # The real hazard: the walk asks for 131072, the server quietly loads at
        # 16384 because that is what fits, and the manifest still records the
        # number that was requested.
        result = L.walk(
            models=["m1"],
            suite_id="merge",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
            load=lambda m, ctx: 16384,
            context_length=131072,
        )
        rung = result["rungs"][0]
        assert "server loaded at 16384" in rung["load_error"]
        assert rung["turns"] == []

    def test_a_rung_that_cannot_load_is_skipped_not_measured(self):
        def _explode(model, ctx):
            raise RuntimeError("out of memory")

        result = L.walk(
            models=["m1"],
            suite_id="merge",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
            load=_explode,
            context_length=131072,
        )
        assert "could not load at 131072" in result["rungs"][0]["load_error"]

    def test_without_a_loader_the_walk_still_runs(self):
        # Falls back to just-in-time loading. Permitted, but it is how a ladder
        # ends up comparing 262144 against 16384, so nothing records a context.
        result = L.walk(
            models=["m1"],
            suite_id="merge",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
        )
        assert result["rungs"][0]["context_length"] is None
        assert result["rungs"][0]["turns"]


class TestComparabilityIsReportedNotCorrected:
    def _ctx_rungs(self, *pairs):
        return [
            L.RungResult(
                model=f"m{i}", suite="s",
                available_gb_before_load=gb, context_length=ctx,
            )
            for i, (gb, ctx) in enumerate(pairs)
        ]

    def test_mixed_context_lengths_are_not_comparable(self):
        # The measured mistake: MoE at 262144 against a dense model at 16384.
        # Nothing in the output would have shown it.
        verdict = L.comparability(
            self._ctx_rungs((63.0, 262144), (62.0, 16384))
        )
        assert verdict["comparable"] is False
        assert "different context lengths" in verdict["reason"]
        assert verdict["context_lengths"] == [16384, 262144]

    def test_context_is_checked_before_memory(self):
        # A 16x KV cache difference dwarfs a few GB of drift, so reporting the
        # memory spread instead would name the smaller problem.
        verdict = L.comparability(
            self._ctx_rungs((63.0, 131072), (20.0, 16384))
        )
        assert "context" in verdict["reason"]

    def test_matched_contexts_fall_through_to_the_memory_check(self):
        verdict = L.comparability(
            self._ctx_rungs((63.0, 131072), (62.5, 131072))
        )
        assert verdict["comparable"] is True
        assert verdict["context_lengths"] == [131072]

    def test_rungs_with_no_context_recorded_do_not_trip_the_check(self):
        verdict = L.comparability(
            self._ctx_rungs((63.0, None), (62.5, None))
        )
        assert verdict["comparable"] is True
    def _rungs(self, *readings):
        return [
            L.RungResult(model=f"m{i}", suite="s", available_gb_before_load=r)
            for i, r in enumerate(readings)
        ]

    def test_similar_memory_across_rungs_is_comparable(self):
        assert L.comparability(self._rungs(63.0, 62.5, 63.4))["comparable"] is True

    def test_a_large_memory_spread_is_flagged(self):
        verdict = L.comparability(self._rungs(63.0, 40.0))
        assert verdict["comparable"] is False
        assert "memory artifacts" in verdict["reason"]
        assert verdict["spread_gb"] == 23.0

    def test_too_few_readings_is_not_silently_comparable(self):
        assert L.comparability(self._rungs(63.0))["comparable"] is False

    def test_a_missing_reading_does_not_crash_the_check(self):
        assert L.comparability(self._rungs(63.0, None, 62.0))["comparable"] is True


class TestCircuitBreakerStopsARung:
    def test_three_timeouts_abandon_the_rung_and_stop_calling(self):
        calls = []

        def _timeout(model, case):
            calls.append(case)
            return Turn(model=model, ok=False, timed_out=True)

        result = L.walk(
            models=["m1"],
            suite_id="code",
            cases=list(range(10)),
            run_case=_timeout,
        )
        rung = result["rungs"][0]
        assert rung["abandoned"] is True
        # Stopped at the limit rather than grinding through all ten.
        assert len(calls) == 3

    def test_an_abandoned_rung_is_not_usable(self):
        rung = L.RungResult(model="m", suite="s", abandoned=True)
        assert rung.usable is False

    def test_a_rung_with_no_turns_is_not_usable(self):
        assert L.RungResult(model="m", suite="s").usable is False

    def test_a_completed_rung_is_usable(self):
        rung = L.RungResult(model="m", suite="s", turns=[Turn(model="m", ok=True)])
        assert rung.usable is True


class TestWalkShape:
    def test_every_model_and_rep_produces_a_rung(self):
        result = L.walk(
            models=["a", "b"],
            suite_id="pkm",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
            reps=2,
        )
        assert len(result["rungs"]) == 4
        assert result["suite"] == "pkm"

    def test_each_rung_records_when_it_ran(self):
        # Thermal state tracks with elapsed time, so the offset has to be
        # recoverable after the fact.
        result = L.walk(
            models=["a", "b"],
            suite_id="pkm",
            cases=["c"],
            run_case=lambda m, c: Turn(model=m, ok=True),
        )
        offsets = [r["wall_clock_offset_s"] for r in result["rungs"]]
        assert all(o is not None for o in offsets)
