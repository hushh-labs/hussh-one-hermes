# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Reserved PKM manifest policy for approved source-derived knowledge."""

from __future__ import annotations

from typing import Any

from hermes_cli.hussh_one_pkm.pkm import PkmBridgeError


_KNOWLEDGE_FIELDS = frozenset({
    "kind", "statement", "confidence", "timestamp", "provenance_ref"
})
_KINDS = frozenset({"fact", "summary"})


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
        if not scope_path.startswith("knowledge."):
            raise PkmBridgeError(
                "Source Library writes are restricted to source_library.knowledge."
            )
        if set(merge_patch) != {"knowledge"}:
            raise PkmBridgeError(
                "Source Library mutations may contain only the knowledge branch."
            )
        self.validate_domain_data(merge_patch)

    def validate_domain_data(self, domain_data: dict[str, Any]) -> None:
        if set(domain_data) != {"knowledge"}:
            raise PkmBridgeError(
                "Source Library PKM may contain only approved derived knowledge."
            )
        knowledge = domain_data.get("knowledge")
        if not isinstance(knowledge, dict) or not knowledge:
            raise PkmBridgeError("Source Library knowledge must be a non-empty object.")
        for knowledge_id, item in knowledge.items():
            if not str(knowledge_id).startswith("k_") or not isinstance(item, dict):
                raise PkmBridgeError("A normalized Source Library knowledge item is required.")
            if set(item) != _KNOWLEDGE_FIELDS:
                raise PkmBridgeError(
                    "Source Library knowledge has unsupported or missing fields."
                )
            if item.get("kind") not in _KINDS:
                raise PkmBridgeError("Source Library knowledge kind is invalid.")
            statement = item.get("statement")
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 4_000:
                raise PkmBridgeError("Source Library knowledge statement is invalid.")
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise PkmBridgeError("Source Library confidence must be numeric.")
            if not 0.0 <= float(confidence) <= 1.0:
                raise PkmBridgeError("Source Library confidence must be between 0 and 1.")
            if not isinstance(item.get("timestamp"), str) or not item["timestamp"].strip():
                raise PkmBridgeError("Source Library knowledge requires a timestamp.")
            provenance = item.get("provenance_ref")
            if not isinstance(provenance, str) or not provenance.startswith("prov_"):
                raise PkmBridgeError("Source Library provenance reference is invalid.")

    def top_level_scope_paths(self, domain_data: dict[str, Any]) -> list[str]:
        self.validate_domain_data(domain_data)
        return ["knowledge"]

    def decorate_path(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        path = str(descriptor.get("json_path") or "")
        eligible = path.startswith("knowledge.") and descriptor.get("path_type") == "leaf"
        return {
            **descriptor,
            "exposure_eligibility": eligible,
            "consent_label": "Source-derived knowledge",
            "sensitivity_label": "consent_required",
            "source_agent": "hussh_one_source_library",
        }


SOURCE_LIBRARY_MANIFEST_POLICY = SourceLibraryManifestPolicy()
