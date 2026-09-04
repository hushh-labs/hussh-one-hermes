# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The boundary between Hermes' vocabulary and the husshone v1 wire contract.

Hermes says ``device`` / ``cloud``.  The v1 control plane in ``hushh-labs/husshone``
says ``puppy`` / ``gcp`` — pinned in ``src/lib/burst/types.ts``, in the enums of
``burst-control-plane.openapi.yaml`` (``placement: [puppy, gcp]``), and in
``BurstClient.swift``.

The mapping is total and exact, which is precisely why it is dangerous to leave
implicit: a ``target`` field carrying the wrong dialect does not fail loudly, it
deserializes into neither value and the caller reads a placement that was never
decided.  So the translation lives here, in one place, and **raises rather than
guesses**.

This module is pure.  It converts strings; it does not know what a burst is.

Direction matters and is named in the function, not inferred from context:

* :func:`to_wire` — leaving Hermes, toward the v1 control plane.
* :func:`from_wire` — arriving from the v1 control plane, into Hermes.

Whether Hermes should speak this dialect at all is a separate question — the v1
control plane still serves husshone's callers, and until they cut over, anything
that talks to it needs this.  See
``docs/hussh-one/architecture/xtreme-burst.md`` § *Vocabulary*.
"""

from __future__ import annotations

from typing import Final, Literal

from .types import PlacementTarget

#: The husshone control plane's placement vocabulary (OpenAPI ``[puppy, gcp]``).
WirePlacement = Literal["puppy", "gcp"]

#: Hermes → wire.  ``puppy`` bakes in one device tier, ``gcp`` one provider;
#: that is why Hermes does not use them internally.
_TO_WIRE: Final[dict[str, str]] = {"device": "puppy", "cloud": "gcp"}

#: wire → Hermes.  Derived from ``_TO_WIRE`` so the two can never disagree.
_FROM_WIRE: Final[dict[str, str]] = {v: k for k, v in _TO_WIRE.items()}


class WireVocabularyError(ValueError):
    """A placement value belongs to neither dialect, or to the wrong one.

    Carries both dialects in the message because the overwhelmingly likely cause
    is a value that was correct somewhere else.
    """


def to_wire(target: PlacementTarget | str) -> WirePlacement:
    """Translate a Hermes placement into the husshone wire value.

    >>> to_wire("device")
    'puppy'
    >>> to_wire("cloud")
    'gcp'

    Passing a value that is *already* wire vocabulary raises rather than passing
    it through. Silently accepting both dialects is how a boundary stops being a
    boundary.
    """
    try:
        return _TO_WIRE[target]  # type: ignore[return-value]
    except KeyError:
        raise WireVocabularyError(
            f"{target!r} is not a Hermes placement. Hermes uses "
            f"{sorted(_TO_WIRE)}; the husshone wire uses {sorted(_FROM_WIRE)}. "
            "If this value came off the wire, use from_wire()."
        ) from None


def from_wire(placement: WirePlacement | str) -> PlacementTarget:
    """Translate a husshone wire value into a Hermes placement.

    >>> from_wire("puppy")
    'device'
    >>> from_wire("gcp")
    'cloud'
    """
    try:
        return _FROM_WIRE[placement]  # type: ignore[return-value]
    except KeyError:
        raise WireVocabularyError(
            f"{placement!r} is not a husshone placement. The wire uses "
            f"{sorted(_FROM_WIRE)}; Hermes uses {sorted(_TO_WIRE)}. "
            "If this value came from Hermes, use to_wire()."
        ) from None
