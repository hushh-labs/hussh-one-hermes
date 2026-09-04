# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Deciding which on-device model does which job, and keeping that answer fresh.

Separate from ``hussh_one_pkm`` on purpose. Routing is not PKM, and every
docstring in that package is written about PKM saves; folding a general model
harness into it would make both harder to read.

The ``hussh_one_`` prefix is this fork's mechanism for surviving a near-daily
upstream sync. A new package under that prefix adds one row to the sync-risk
table in ``docs/hussh-one/features/puppy-one-edge-compute.md`` at zero risk.
"""

from __future__ import annotations

__all__ = [
    "Capability",
    "CapabilityProfile",
    "CircuitBreaker",
    "RungResult",
    "Turn",
    "UnboundedRequest",
    "build_body",
    "comparability",
    "complete",
    "counterbalanced_order",
    "drain",
    "probe_capabilities",
    "walk",
]

from .ladder import (  # noqa: F401
    RungResult,
    comparability,
    counterbalanced_order,
    drain,
    walk,
)
from .profile import (  # noqa: F401
    Capability,
    CapabilityProfile,
    probe_capabilities,
)
from .request import (  # noqa: F401
    CircuitBreaker,
    Turn,
    UnboundedRequest,
    build_body,
    complete,
)
