# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Residency + eviction policy for LM Studio models.

The eviction tests are the load-bearing ones: a wrong plan here unloads the
model answering the user's current question. They run against the pure
:func:`plan_eviction`, so nothing below touches a real LM Studio server.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import types
import urllib.error

import pytest

from hermes_cli import hussh_one_lmstudio as lmstudio_manager


# ---------------------------------------------------------------------------
# Fixtures: a real `lms ps` table, captured from LM Studio on macOS.
#
# Row 1 is verbatim from the host (note the TTL column has no cell at all —
# the line simply ends after DEVICE). Rows 2 and 3 add the cases that row 1
# cannot show: an MB-sized model, a populated TTL, and a non-IDLE status.
# ---------------------------------------------------------------------------

LMS_PS_OUTPUT = (
    "IDENTIFIER                    MODEL                         "
    "STATUS    SIZE        CONTEXT    PARALLEL    DEVICE    TTL\n"
    "google/gemma-4-26b-a4b-qat    google/gemma-4-26b-a4b-qat    "
    "IDLE      15.64 GB    262144     4           Local\n"
    "text-embedding-nomic-v1.5     nomic-ai/nomic-embed-text     "
    "IDLE      532.00 MB   2048       1           Local     3600s\n"
    "google/gemma-4-31b-qat        google/gemma-4-31b-qat        "
    "LOADING   18.85 GB    262144     1           Local\n"
)

LMS_PS_SINGLE_ROW = (
    "IDENTIFIER                    MODEL                         "
    "STATUS    SIZE        CONTEXT    PARALLEL    DEVICE    TTL\n"
    "google/gemma-4-26b-a4b-qat    google/gemma-4-26b-a4b-qat    "
    "IDLE      15.64 GB    262144     4           Local\n"
)


def _resident(identifier, size_gb, status="IDLE"):
    return {
        "identifier": identifier,
        "model": identifier,
        "status": status,
        "size_gb": size_gb,
        "context": 262144,
        "ttl": "",
    }


class _Response:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _http_error(code, payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return urllib.error.HTTPError(
        "http://127.0.0.1:1234/api/v1/models/unload",
        code,
        "error",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def _completed(argv, returncode=0, stdout=""):
    return subprocess.CompletedProcess(list(argv), returncode, stdout, "")


# ---------------------------------------------------------------------------
# plan_eviction — pure policy
# ---------------------------------------------------------------------------


def test_no_eviction_when_the_model_already_fits():
    plan = lmstudio_manager.plan_eviction(
        need_gb=12.0,
        loaded=[_resident("a", 15.64), _resident("b", 8.0)],
        available_gb=40.0,
    )

    assert plan == []


def test_evicts_one_idle_model_to_cover_the_shortfall():
    plan = lmstudio_manager.plan_eviction(
        need_gb=20.0,
        loaded=[_resident("a", 15.64), _resident("b", 8.0)],
        available_gb=6.0,
    )

    assert plan == ["a"]


def test_prefers_the_smallest_set_that_fits():
    # Two 4 GB residents would also cover a 9 GB gap, but that is two
    # evictions where one suffices.
    plan = lmstudio_manager.plan_eviction(
        need_gb=10.0,
        loaded=[_resident("small-a", 4.0), _resident("small-b", 4.0), _resident("big", 12.0)],
        available_gb=1.0,
    )

    assert plan == ["big"]


def test_takes_two_only_when_no_single_model_covers_the_gap():
    plan = lmstudio_manager.plan_eviction(
        need_gb=14.0,
        loaded=[_resident("small-a", 4.0), _resident("small-b", 4.0), _resident("big", 12.0)],
        available_gb=1.0,
    )

    assert plan == ["small-a", "big"]


def test_equal_sized_candidates_break_toward_the_least_recently_used():
    plan = lmstudio_manager.plan_eviction(
        need_gb=10.0,
        loaded=[_resident("oldest", 8.0), _resident("newest", 8.0)],
        available_gb=3.0,
    )

    assert plan == ["oldest"]


def test_protected_model_is_never_evicted():
    plan = lmstudio_manager.plan_eviction(
        need_gb=20.0,
        loaded=[_resident("serving-the-session", 15.64), _resident("idle", 8.0)],
        available_gb=13.0,
        protect=("serving-the-session",),
    )

    assert plan == ["idle"]


def test_protected_model_is_not_evicted_even_when_it_is_the_only_fit():
    plan = lmstudio_manager.plan_eviction(
        need_gb=30.0,
        loaded=[_resident("serving-the-session", 15.64)],
        available_gb=1.0,
        protect=["serving-the-session"],
    )

    assert plan == []


@pytest.mark.parametrize("status", ["LOADING", "GENERATING", "", "idle-ish"])
def test_a_model_that_is_not_idle_is_never_evicted(status):
    plan = lmstudio_manager.plan_eviction(
        need_gb=20.0,
        loaded=[_resident("busy", 15.64, status=status)],
        available_gb=1.0,
    )

    assert plan == []


def test_status_match_is_case_insensitive():
    plan = lmstudio_manager.plan_eviction(
        need_gb=20.0,
        loaded=[_resident("a", 15.64, status="idle")],
        available_gb=6.0,
    )

    assert plan == ["a"]


def test_returns_the_full_evictable_set_when_it_still_cannot_fit():
    plan = lmstudio_manager.plan_eviction(
        need_gb=80.0,
        loaded=[
            _resident("idle-a", 8.0),
            _resident("busy", 30.0, status="LOADING"),
            _resident("protected", 30.0),
            _resident("idle-b", 4.0),
        ],
        available_gb=1.0,
        protect=("protected",),
    )

    assert plan == ["idle-a", "idle-b"]


def test_nothing_evictable_returns_an_empty_plan():
    plan = lmstudio_manager.plan_eviction(
        need_gb=40.0,
        loaded=[_resident("busy", 30.0, status="LOADING")],
        available_gb=1.0,
    )

    assert plan == []


def test_an_unparsed_size_is_never_chosen_to_close_a_gap_it_may_not_close():
    plan = lmstudio_manager.plan_eviction(
        need_gb=10.0,
        loaded=[_resident("unknown-size", 0.0), _resident("known", 9.0)],
        available_gb=1.0,
    )

    assert plan == ["known"]


def test_an_unparsed_size_still_goes_when_everything_evictable_has_to():
    plan = lmstudio_manager.plan_eviction(
        need_gb=100.0,
        loaded=[_resident("unknown-size", 0.0), _resident("known", 9.0)],
        available_gb=1.0,
    )

    assert plan == ["unknown-size", "known"]


def test_beyond_the_exhaustive_cap_the_plan_falls_back_to_lru_first():
    loaded = [_resident(f"m{index}", 4.0) for index in range(20)]

    plan = lmstudio_manager.plan_eviction(need_gb=12.0, loaded=loaded, available_gb=0.0)

    assert plan == ["m0", "m1", "m2"]


def test_malformed_entries_are_skipped_not_fatal():
    plan = lmstudio_manager.plan_eviction(
        need_gb=10.0,
        loaded=["not-a-dict", None, {"status": "IDLE", "size_gb": 12.0}, _resident("real", 12.0)],
        available_gb=1.0,
    )

    assert plan == ["real"]


# ---------------------------------------------------------------------------
# `lms ps` parsing
# ---------------------------------------------------------------------------


def test_parses_the_captured_single_row_table():
    records = lmstudio_manager.parse_lms_ps(LMS_PS_SINGLE_ROW)

    assert records == [
        {
            "identifier": "google/gemma-4-26b-a4b-qat",
            "model": "google/gemma-4-26b-a4b-qat",
            "status": "IDLE",
            "size_gb": 15.64,
            "context": 262144,
            "ttl": "",
        }
    ]


def test_parses_mb_sizes_and_a_populated_ttl():
    records = lmstudio_manager.parse_lms_ps(LMS_PS_OUTPUT)
    by_id = {record["identifier"]: record for record in records}

    assert len(records) == 3
    assert by_id["text-embedding-nomic-v1.5"]["size_gb"] == 0.532
    assert by_id["text-embedding-nomic-v1.5"]["ttl"] == "3600s"
    assert by_id["text-embedding-nomic-v1.5"]["context"] == 2048
    assert by_id["google/gemma-4-26b-a4b-qat"]["ttl"] == ""
    assert by_id["google/gemma-4-31b-qat"]["status"] == "LOADING"


def test_the_size_column_survives_its_embedded_space():
    # "15.64 GB" is one cell holding two whitespace-separated tokens, so a
    # naive split() lines the remaining columns up one position off.
    records = lmstudio_manager.parse_lms_ps(LMS_PS_OUTPUT)

    assert [record["context"] for record in records] == [262144, 2048, 262144]
    assert [record["status"] for record in records] == ["IDLE", "IDLE", "LOADING"]


@pytest.mark.parametrize(
    "output",
    ["", "   \n", "No models are currently loaded.\n", None],
)
def test_output_without_a_table_parses_to_nothing(output):
    assert lmstudio_manager.parse_lms_ps(output) == []


def test_separator_and_blank_lines_are_ignored():
    output = LMS_PS_SINGLE_ROW.replace(
        "\ngoogle/gemma", "\n---------------------------\n\ngoogle/gemma"
    )

    records = lmstudio_manager.parse_lms_ps(output)

    assert [record["identifier"] for record in records] == ["google/gemma-4-26b-a4b-qat"]


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("15.64 GB", 15.64),
        ("532.00 MB", 0.532),
        ("1.5 TB", 1500.0),
        ("14.56 GiB", 15.6337),
        ("", 0.0),
        ("unknown", 0.0),
        ("12 QB", 0.0),
    ],
)
def test_size_cell_conversion(cell, expected):
    assert lmstudio_manager._parse_size_gb(cell) == expected


# ---------------------------------------------------------------------------
# loaded_models
# ---------------------------------------------------------------------------


def test_loaded_models_parses_the_cli_table(monkeypatch):
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: "/fake/lms")
    monkeypatch.setattr(
        lmstudio_manager,
        "bounded_probe_run",
        lambda argv, **_kwargs: _completed(argv, stdout=LMS_PS_SINGLE_ROW),
    )

    records = lmstudio_manager.loaded_models()

    assert [record["identifier"] for record in records] == ["google/gemma-4-26b-a4b-qat"]


def test_loaded_models_is_empty_without_the_cli(monkeypatch):
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: None)

    assert lmstudio_manager.loaded_models() == []


@pytest.mark.parametrize("outcome", ["timeout", "nonzero"])
def test_loaded_models_degrades_on_a_failed_probe(monkeypatch, outcome):
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: "/fake/lms")
    monkeypatch.setattr(
        lmstudio_manager,
        "bounded_probe_run",
        lambda argv, **_kwargs: None if outcome == "timeout" else _completed(argv, 1),
    )

    assert lmstudio_manager.loaded_models() == []


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_reads_the_v0_state_field(monkeypatch):
    payload = {
        "data": [
            {
                "id": "google/gemma-4-26b-a4b-qat",
                "type": "vlm",
                "state": "loaded",
                "max_context_length": 262144,
            },
            {"id": "google/gemma-4-12b", "type": "vlm", "state": "not-loaded"},
            {"no": "id"},
            "not-a-dict",
        ]
    }
    captured = []

    def fake_open(request, *, timeout):
        captured.append((request.full_url, timeout))
        return _Response(payload)

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)

    models = lmstudio_manager.list_models("http://127.0.0.1:1234/v1")

    assert captured[0][0] == "http://127.0.0.1:1234/api/v0/models"

    # Assert the fields this test is about, not the whole dict: list_models has
    # since grown publisher/quantization/capabilities, and an exact-equality
    # assertion turned every additive change into a failure. The residency
    # contract is what matters here — the malformed rows are dropped, and each
    # surviving row carries the v0 `state` verbatim.
    fields = ("id", "state", "type", "max_context_length")
    assert [{k: m[k] for k in fields} for m in models] == [
        {
            "id": "google/gemma-4-26b-a4b-qat",
            "state": "loaded",
            "type": "vlm",
            "max_context_length": 262144,
        },
        {
            "id": "google/gemma-4-12b",
            "state": "not-loaded",
            "type": "vlm",
            "max_context_length": None,
        },
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:1234",
        "http://127.0.0.1:1234/",
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:1234/api/v1",
        "http://127.0.0.1:1234/api/v0/",
    ],
)
def test_every_pasted_base_url_form_reaches_the_same_endpoint(monkeypatch, base_url):
    captured = []

    def fake_open(request, *, timeout):
        captured.append(request.full_url)
        return _Response({"data": []})

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)

    lmstudio_manager.list_models(base_url)

    assert captured == ["http://127.0.0.1:1234/api/v0/models"]


def test_list_models_is_empty_when_the_server_is_unreachable(monkeypatch):
    def fake_open(_request, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)

    assert lmstudio_manager.list_models() == []


def test_list_models_is_empty_on_a_malformed_payload(monkeypatch):
    monkeypatch.setattr(
        lmstudio_manager,
        "open_credentialed_url",
        lambda _request, *, timeout: _Response({"models": []}),
    )

    assert lmstudio_manager.list_models() == []


# ---------------------------------------------------------------------------
# host_memory
# ---------------------------------------------------------------------------


def test_host_memory_prefers_psutil(monkeypatch):
    fake = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(
            total=137_438_953_472, available=120_000_000_000
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert lmstudio_manager.host_memory() == {
        "total_gb": 137.44,
        "available_gb": 120.0,
        "free_pct": 87.3,
    }


def test_host_memory_falls_back_to_macos_system_tools(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_probe(argv, **_kwargs):
        if argv[0] == "sysctl":
            return _completed(argv, stdout="137438953472\n")
        return _completed(
            argv, stdout="Pageouts: 430438 \n\nSystem-wide memory free percentage: 88%\n"
        )

    monkeypatch.setattr(lmstudio_manager, "bounded_probe_run", fake_probe)

    assert lmstudio_manager.host_memory() == {
        "total_gb": 137.44,
        "available_gb": 120.95,
        "free_pct": 88.0,
    }


def test_host_memory_is_empty_when_the_free_percentage_is_unreadable(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_probe(argv, **_kwargs):
        if argv[0] == "sysctl":
            return _completed(argv, stdout="137438953472\n")
        return None

    monkeypatch.setattr(lmstudio_manager, "bounded_probe_run", fake_probe)

    assert lmstudio_manager.host_memory() == {}


def test_host_memory_is_empty_when_nothing_can_answer(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(sys, "platform", "linux")

    assert lmstudio_manager.host_memory() == {}


# ---------------------------------------------------------------------------
# unload_model
# ---------------------------------------------------------------------------


def test_unload_uses_the_rest_route_and_sends_an_instance_id(monkeypatch):
    captured = []

    def fake_open(request, *, timeout):
        captured.append((request.full_url, request.get_method(), json.loads(request.data)))
        return _Response({"status": "unloaded"})

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)
    monkeypatch.setattr(
        lmstudio_manager, "_cli_unload", lambda *_a, **_k: pytest.fail("CLI not needed")
    )

    assert lmstudio_manager.unload_model("google/gemma-4-26b-a4b-qat") is True
    assert captured == [
        (
            "http://127.0.0.1:1234/api/v1/models/unload",
            "POST",
            {"instance_id": "google/gemma-4-26b-a4b-qat"},
        )
    ]


def test_a_typed_rest_refusal_is_final_and_skips_the_cli(monkeypatch):
    def fake_open(_request, *, timeout):
        raise _http_error(
            404, {"error": {"type": "model_not_found", "message": "not loaded."}}
        )

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)
    monkeypatch.setattr(
        lmstudio_manager, "_cli_unload", lambda *_a, **_k: pytest.fail("CLI not needed")
    )

    assert lmstudio_manager.unload_model("ghost") is False


def test_a_router_level_404_falls_back_to_the_cli(monkeypatch):
    def fake_open(_request, *, timeout):
        raise _http_error(404, {"error": "Unexpected endpoint or method."})

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: "/fake/lms")
    calls = []

    def fake_probe(argv, **_kwargs):
        calls.append(argv)
        return _completed(argv)

    monkeypatch.setattr(lmstudio_manager, "bounded_probe_run", fake_probe)

    assert lmstudio_manager.unload_model("google/gemma-4-26b-a4b-qat") is True
    assert calls == [["/fake/lms", "unload", "google/gemma-4-26b-a4b-qat"]]


def test_an_unreachable_server_falls_back_to_the_cli(monkeypatch):
    def fake_open(_request, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: "/fake/lms")
    monkeypatch.setattr(
        lmstudio_manager, "bounded_probe_run", lambda argv, **_kwargs: _completed(argv, 1)
    )

    assert lmstudio_manager.unload_model("google/gemma-4-26b-a4b-qat") is False


def test_unload_returns_false_without_a_cli_to_fall_back_to(monkeypatch):
    def fake_open(_request, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(lmstudio_manager, "open_credentialed_url", fake_open)
    monkeypatch.setattr(lmstudio_manager, "_lms_binary", lambda: None)

    assert lmstudio_manager.unload_model("google/gemma-4-26b-a4b-qat") is False


@pytest.mark.parametrize("identifier", ["", "   ", None])
def test_unload_refuses_an_empty_identifier(monkeypatch, identifier):
    monkeypatch.setattr(
        lmstudio_manager,
        "open_credentialed_url",
        lambda *_a, **_k: pytest.fail("no request should be made"),
    )

    assert lmstudio_manager.unload_model(identifier) is False


# ---------------------------------------------------------------------------
# ensure_capacity
# ---------------------------------------------------------------------------


def test_ensure_capacity_evicts_nothing_when_it_already_fits(monkeypatch):
    monkeypatch.setattr(
        lmstudio_manager,
        "host_memory",
        lambda: {"total_gb": 137.44, "available_gb": 120.0, "free_pct": 87.3},
    )
    monkeypatch.setattr(
        lmstudio_manager, "loaded_models", lambda: [_resident("resident", 15.64)]
    )
    monkeypatch.setattr(
        lmstudio_manager,
        "unload_model",
        lambda *_a, **_k: pytest.fail("nothing should be evicted"),
    )

    result = lmstudio_manager.ensure_capacity(need_gb=18.0)

    assert result == {"evicted": [], "fit": True, "available_gb": 120.0}


def test_ensure_capacity_evicts_then_re_measures(monkeypatch):
    readings = iter([{"available_gb": 4.0}, {"available_gb": 19.6}])
    monkeypatch.setattr(lmstudio_manager, "host_memory", lambda: next(readings))
    monkeypatch.setattr(
        lmstudio_manager,
        "loaded_models",
        lambda: [_resident("idle-big", 15.64), _resident("busy", 18.85, status="LOADING")],
    )
    monkeypatch.setattr(lmstudio_manager, "unload_model", lambda *_a, **_k: True)

    result = lmstudio_manager.ensure_capacity(need_gb=18.0)

    assert result == {"evicted": ["idle-big"], "fit": True, "available_gb": 19.6}


def test_ensure_capacity_projects_when_the_second_reading_fails(monkeypatch):
    readings = iter([{"available_gb": 4.0}, {}])
    monkeypatch.setattr(lmstudio_manager, "host_memory", lambda: next(readings))
    monkeypatch.setattr(
        lmstudio_manager, "loaded_models", lambda: [_resident("idle-big", 15.64)]
    )
    monkeypatch.setattr(lmstudio_manager, "unload_model", lambda *_a, **_k: True)

    result = lmstudio_manager.ensure_capacity(need_gb=18.0)

    assert result == {"evicted": ["idle-big"], "fit": True, "available_gb": 19.64}


def test_ensure_capacity_reports_a_failed_unload_as_not_evicted(monkeypatch):
    monkeypatch.setattr(lmstudio_manager, "host_memory", lambda: {"available_gb": 4.0})
    monkeypatch.setattr(
        lmstudio_manager, "loaded_models", lambda: [_resident("idle-big", 15.64)]
    )
    monkeypatch.setattr(lmstudio_manager, "unload_model", lambda *_a, **_k: False)

    result = lmstudio_manager.ensure_capacity(need_gb=18.0)

    assert result == {"evicted": [], "fit": False, "available_gb": 4.0}


def test_ensure_capacity_never_unloads_a_protected_model(monkeypatch):
    monkeypatch.setattr(lmstudio_manager, "host_memory", lambda: {"available_gb": 1.0})
    monkeypatch.setattr(
        lmstudio_manager, "loaded_models", lambda: [_resident("serving-the-session", 15.64)]
    )
    monkeypatch.setattr(
        lmstudio_manager,
        "unload_model",
        lambda *_a, **_k: pytest.fail("the active model must survive"),
    )

    result = lmstudio_manager.ensure_capacity(
        need_gb=18.0, protect=("serving-the-session",)
    )

    assert result == {"evicted": [], "fit": False, "available_gb": 1.0}


def test_ensure_capacity_declines_to_evict_on_an_unreadable_host(monkeypatch):
    monkeypatch.setattr(lmstudio_manager, "host_memory", dict)
    monkeypatch.setattr(
        lmstudio_manager,
        "loaded_models",
        lambda: pytest.fail("no plan should be made without a memory reading"),
    )

    result = lmstudio_manager.ensure_capacity(need_gb=18.0)

    assert result == {"evicted": [], "fit": False, "available_gb": 0.0}
