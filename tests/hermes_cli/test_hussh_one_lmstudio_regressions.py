# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Regressions in the LM Studio manager, found by adversarial review.

Each of these is a way the eviction planner could destroy a warm model the
owner still wanted. Model weights are expensive to reload on an edge device,
so an eviction that buys nothing is not a cosmetic bug.
"""

from __future__ import annotations

import hermes_cli.hussh_one_lmstudio as lm


def test_ensure_capacity_never_evicts_when_the_fit_is_impossible(monkeypatch):
    # plan_eviction deliberately returns the whole evictable set when even
    # shedding all of it cannot close the gap, so a caller can see the ceiling.
    # Acting on that plan is the worst outcome available: every warm model
    # unloaded AND still no room.
    unloaded: list[str] = []
    monkeypatch.setattr(lm, "host_memory", lambda: {"available_gb": 10.0})
    monkeypatch.setattr(
        lm,
        "loaded_models",
        lambda **_kw: [
            {"identifier": "warm-a", "status": "IDLE", "size_gb": 8.0},
            {"identifier": "warm-b", "status": "IDLE", "size_gb": 8.0},
        ],
    )

    def _record(identifier, **_kw):
        unloaded.append(identifier)
        return True

    monkeypatch.setattr(lm, "unload_model", _record)

    # 300 GB can never fit: 10 free plus 16 evictable is nowhere near.
    result = lm.ensure_capacity(need_gb=300.0)

    assert unloaded == [], "must not destroy warm models it cannot benefit from"
    assert result["evicted"] == []
    assert result["fit"] is False


def test_ensure_capacity_still_evicts_when_that_does_achieve_the_fit(monkeypatch):
    # The guard above must not turn into "never evict".
    unloaded: list[str] = []
    monkeypatch.setattr(lm, "host_memory", lambda: {"available_gb": 2.0})
    monkeypatch.setattr(
        lm,
        "loaded_models",
        lambda **_kw: [{"identifier": "idle-one", "status": "IDLE", "size_gb": 20.0}],
    )

    def _record(identifier, **_kw):
        unloaded.append(identifier)
        return True

    monkeypatch.setattr(lm, "unload_model", _record)

    result = lm.ensure_capacity(need_gb=15.0)

    assert unloaded == ["idle-one"]
    assert result["evicted"] == ["idle-one"]


def test_protect_is_case_insensitive():
    # A protect list is a safety instruction, not a string-matching puzzle: the
    # failure mode is unloading the model serving the active session.
    plan = lm.plan_eviction(
        need_gb=20.0,
        loaded=[{"identifier": "Serving-Model", "status": "IDLE", "size_gb": 30.0}],
        available_gb=1.0,
        protect=["serving-model"],
    )
    assert plan == []


def test_prefers_the_cheapest_single_eviction_not_the_first():
    # With a small gap and both a 5 GB and a 40 GB idle resident, every
    # single-model plan "fits". Taking the first in LRU order throws away 40 GB
    # of warm weights to free 5.
    plan = lm.plan_eviction(
        need_gb=5.0,
        loaded=[
            {"identifier": "huge", "status": "IDLE", "size_gb": 40.0},
            {"identifier": "small", "status": "IDLE", "size_gb": 5.0},
        ],
        available_gb=1.0,
    )
    assert plan == ["small"]
