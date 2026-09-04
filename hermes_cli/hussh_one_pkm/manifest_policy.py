# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Domain-specific policy hooks for native PKM manifest construction."""

from __future__ import annotations

from typing import Any, Protocol


class PkmManifestPolicy(Protocol):
    """Fail-closed policy applied before a reserved PKM domain is proposed."""

    domain: str

    def validate_write(
        self,
        *,
        scope_path: str,
        merge_patch: dict[str, Any],
    ) -> None: ...

    def validate_domain_data(self, domain_data: dict[str, Any]) -> None: ...

    def top_level_scope_paths(self, domain_data: dict[str, Any]) -> list[str]: ...

    def decorate_path(self, descriptor: dict[str, Any]) -> dict[str, Any]: ...


RESERVED_PKM_DOMAINS = frozenset({"source_library"})
