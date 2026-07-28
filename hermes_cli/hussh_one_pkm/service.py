# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""In-process, user-approved Hussh One PKM write service."""

from __future__ import annotations

from typing import Any, Callable

from .bridge import HusshVaultBridge
from .pkm import PkmClient, PkmProposal


class PkmWriteDeclined(RuntimeError):
    pass


class HusshPkmWriteService:
    """Compose proposal, fresh approval, validation, and commit in one call."""

    def __init__(
        self,
        bridge: HusshVaultBridge,
        *,
        approve: Callable[[str, str], str],
    ) -> None:
        self.bridge = bridge
        self.client = PkmClient(bridge)
        self.approve = approve

    @staticmethod
    def _approval_message(proposal: PkmProposal) -> str:
        safe = proposal.safe_view()
        return "\n".join([
            "Save this update to your Hussh One Personal Data?",
            f"Domain: {safe['domain']}",
            f"Path: {safe['scope_path']}",
            f"Change: {safe['summary']}",
            f"Sharing impact: {safe['sharing_impact']['summary']}",
        ])

    def save(
        self,
        *,
        domain: str,
        scope_path: str,
        merge_patch: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        self.bridge.require_vault_key()
        proposal = self.client.propose(
            domain=domain,
            scope_path=scope_path,
            merge_patch=merge_patch,
            summary=summary,
        )
        decision = self.approve(
            self._approval_message(proposal),
            "A fresh approval is required for this single encrypted PKM write.",
        )
        if decision != "accept":
            raise PkmWriteDeclined("The PKM write was not approved.")
        result = self.client.commit(proposal)
        return {
            "success": bool(result.get("success")),
            "domain": result.get("domain"),
            "data_version": result.get("data_version"),
            "exports_marked_for_refresh": bool(
                result.get("exports_marked_for_refresh")
            ),
        }
