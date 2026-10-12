# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The vocabulary boundary: it must translate, and it must refuse.

The migration record calls the ``device``/``cloud`` vs ``puppy``/``gcp`` split a
deliberate incompatibility and warns that a ``target`` field carrying the wrong
dialect "will silently deserialize into neither". These tests exist so that
warning is enforced rather than merely written down.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_burst.types import PlacementTarget
from hermes_cli.hussh_one_burst.wire import (
    WireVocabularyError,
    from_wire,
    to_wire,
)


def test_the_mapping_the_migration_record_specifies():
    assert to_wire("device") == "puppy"
    assert to_wire("cloud") == "gcp"
    assert from_wire("puppy") == "device"
    assert from_wire("gcp") == "cloud"


def test_round_trips_in_both_directions():
    for hermes in ("device", "cloud"):
        assert from_wire(to_wire(hermes)) == hermes
    for wire in ("puppy", "gcp"):
        assert to_wire(from_wire(wire)) == wire


def test_covers_every_placement_hermes_can_produce():
    """If PlacementTarget gains a value, this fails until the adapter learns it."""
    for target in PlacementTarget.__args__:  # type: ignore[attr-defined]
        assert to_wire(target)


def test_wire_vocabulary_is_refused_on_the_hermes_side():
    """The failure mode this module exists for: passing the wrong dialect.

    Accepting both would make the boundary meaningless — the whole point is that
    exactly one vocabulary is valid on each side.
    """
    for stale in ("puppy", "gcp"):
        with pytest.raises(WireVocabularyError, match="not a Hermes placement"):
            to_wire(stale)


def test_hermes_vocabulary_is_refused_on_the_wire_side():
    for native in ("device", "cloud"):
        with pytest.raises(WireVocabularyError, match="not a husshone placement"):
            from_wire(native)


def test_the_error_names_both_dialects_and_the_other_function():
    """A wrong-dialect value is nearly always right somewhere else, so the error
    should say where rather than only that it is wrong."""
    with pytest.raises(WireVocabularyError) as exc:
        to_wire("gcp")
    message = str(exc.value)
    assert "cloud" in message and "device" in message
    assert "puppy" in message and "gcp" in message
    assert "from_wire()" in message


@pytest.mark.parametrize("junk", ["", "  ", "DEVICE", "Puppy", "aws", None, 0])
def test_nonsense_is_refused_rather_than_guessed(junk):
    with pytest.raises((WireVocabularyError, TypeError)):
        to_wire(junk)  # type: ignore[arg-type]
    with pytest.raises((WireVocabularyError, TypeError)):
        from_wire(junk)  # type: ignore[arg-type]


def test_the_two_tables_cannot_disagree():
    """from_wire's table is derived from to_wire's, so drift is impossible."""
    from hermes_cli.hussh_one_burst.wire import _FROM_WIRE, _TO_WIRE

    assert _FROM_WIRE == {v: k for k, v in _TO_WIRE.items()}
    assert len(_FROM_WIRE) == len(_TO_WIRE)


def test_matches_the_openapi_enum_actually_migrated_into_this_repo():
    """Reads the migrated v1 contract rather than trusting memory of it."""
    import pathlib
    import re

    spec = pathlib.Path(
        "docs/hussh-one/reference/xtreme-burst/burst-control-plane.openapi.yaml"
    )
    if not spec.exists():  # pragma: no cover - repo layout guard
        pytest.skip("migrated OpenAPI spec not present")
    text = spec.read_text(encoding="utf-8")
    match = re.search(r"placement:\s*\{\s*type:\s*string,\s*enum:\s*\[([^\]]+)\]", text)
    assert match, "no placement enum found in the migrated spec"
    declared = {v.strip() for v in match.group(1).split(",")}
    assert declared <= {"puppy", "gcp"}
    for value in declared:
        assert from_wire(value)
