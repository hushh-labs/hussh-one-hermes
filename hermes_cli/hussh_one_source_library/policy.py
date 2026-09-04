# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Private reserved PKM policy for Source Library semantic/control memory."""

from __future__ import annotations

from typing import Any

from hermes_cli.hussh_one_pkm.pkm import PkmBridgeError

from .contracts import SourceLibraryMemoryV2


_KNOWLEDGE_FIELDS = frozenset({
    "kind",
    "statement",
    "confidence",
    "timestamp",
    "provenance_ref",
})
_KINDS = frozenset({"fact", "summary"})
_PRIVATE_BRANCHES = frozenset({
    "schema_version",
    "roots",
    "items",
    "collections",
    "knowledge",
    "relationships",
})


class SourceLibraryManifestPolicy:
    domain = "source_library"

    def validate_write(
        self,
        *,
        scope_path: str,
        merge_patch: dict[str, Any],
    ) -> None:
        if not isinstance(merge_patch, dict):
            raise PkmBridgeError("Source Library requires a JSON object mutation.")
        top_level = scope_path.split(".", 1)[0]
        if top_level not in _PRIVATE_BRANCHES:
            raise PkmBridgeError(
                "Source Library writes are restricted to its private V2 branches."
            )
        if set(merge_patch) != {top_level}:
            raise PkmBridgeError(
                "A Source Library mutation must stay in one reviewed private branch."
            )
        self.validate_domain_data(merge_patch)

    def validate_domain_data(self, domain_data: dict[str, Any]) -> None:
        if not domain_data or set(domain_data) - _PRIVATE_BRANCHES:
            raise PkmBridgeError(
                "Source Library PKM contains an unsupported private-memory branch."
            )
        try:
            memory = SourceLibraryMemoryV2.from_json(domain_data)
        except (TypeError, ValueError) as exc:
            raise PkmBridgeError(str(exc)) from exc
        if "knowledge" not in domain_data:
            return
        knowledge = memory.knowledge
        if not isinstance(knowledge, dict) or not knowledge:
            raise PkmBridgeError("Source Library knowledge must be a non-empty object.")
        for knowledge_id, item in knowledge.items():
            if not str(knowledge_id).startswith("k_") or not isinstance(item, dict):
                raise PkmBridgeError(
                    "A normalized Source Library knowledge item is required."
                )
            if set(item) != _KNOWLEDGE_FIELDS:
                raise PkmBridgeError(
                    "Source Library knowledge has unsupported or missing fields."
                )
            if item.get("kind") not in _KINDS:
                raise PkmBridgeError("Source Library knowledge kind is invalid.")
            statement = item.get("statement")
            if (
                not isinstance(statement, str)
                or not statement.strip()
                or len(statement) > 4_000
            ):
                raise PkmBridgeError("Source Library knowledge statement is invalid.")
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise PkmBridgeError("Source Library confidence must be numeric.")
            if not 0.0 <= float(confidence) <= 1.0:
                raise PkmBridgeError(
                    "Source Library confidence must be between 0 and 1."
                )
            if (
                not isinstance(item.get("timestamp"), str)
                or not item["timestamp"].strip()
            ):
                raise PkmBridgeError("Source Library knowledge requires a timestamp.")
            provenance = item.get("provenance_ref")
            if not isinstance(provenance, str) or not provenance.startswith("prov_"):
                raise PkmBridgeError("Source Library provenance reference is invalid.")

    def top_level_scope_paths(self, domain_data: dict[str, Any]) -> list[str]:
        self.validate_domain_data(domain_data)
        # This is an internal capability boundary, not a consent scope catalog.
        return []

    def decorate_path(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        return {
            **descriptor,
            "exposure_eligibility": False,
            "consent_label": "Private Source Library information",
            "sensitivity_label": "private",
            "source_agent": "hussh_one_source_library",
        }


SOURCE_LIBRARY_MANIFEST_POLICY = SourceLibraryManifestPolicy()
