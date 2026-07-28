# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Additive local MCP surface for a trusted Hussh One Hermes profile.

The server preserves Hermes' normal MCP handshake and approval path. It never
offers an unlock tool to the model: identity and vault enrollment happen in the
desktop control plane, while every mutation commit requires MCP elicitation.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .bridge import HusshVaultBridge
from .pkm import PkmClient, PkmProposal

try:
    from mcp.server.fastmcp import Context
except ImportError:  # pragma: no cover - optional MCP extra
    Context = Any  # type: ignore[misc,assignment]

_PROPOSAL_TTL_SECONDS = 10 * 60
_MAX_PROPOSALS = 64
_logger = logging.getLogger(__name__)


class WriteApproval(BaseModel):
    """Schema displayed by Hermes' existing MCP form-elicitation handler."""

    confirm: bool = Field(
        default=True,
        description="Approve the displayed PKM mutation and sharing impact.",
    )


@dataclass
class _PendingProposal:
    proposal: PkmProposal
    created_at: float


class ProposalStore:
    """Small process-memory proposal store; plaintext patches never touch disk."""

    def __init__(self) -> None:
        self._items: dict[str, _PendingProposal] = {}
        self._lock = threading.Lock()

    def put(self, proposal: PkmProposal) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._items) >= _MAX_PROPOSALS:
                oldest = min(self._items, key=lambda key: self._items[key].created_at)
                self._items.pop(oldest, None)
            self._items[proposal.proposal_id] = _PendingProposal(proposal, now)

    def take(self, proposal_id: str) -> PkmProposal:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            pending = self._items.pop(proposal_id, None)
        if pending is None:
            raise ValueError(
                "The PKM proposal is missing or expired. Propose it again."
            )
        return pending.proposal

    def _purge(self, now: float) -> None:
        expired = [
            proposal_id
            for proposal_id, pending in self._items.items()
            if now - pending.created_at > _PROPOSAL_TTL_SECONDS
        ]
        for proposal_id in expired:
            self._items.pop(proposal_id, None)


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise ImportError(
            "The Hussh One bridge requires the Hermes MCP extra."
        ) from exc

    bridge = HusshVaultBridge()
    pkm = PkmClient(bridge)
    proposals = ProposalStore()
    mcp = FastMCP(
        "hussh-one-pkm",
        instructions=(
            "Use this local trusted-device bridge for Hussh One PKM status and "
            "user-approved writes. Reads remain consent-gated through the "
            "canonical Hussh MCP encrypted-export lifecycle. Never ask for, "
            "repeat, or store a vault passphrase, vault key, refresh credential, "
            "owner capability, or decrypted full-domain snapshot."
        ),
    )

    @mcp.tool(
        name="hussh_identity_status",
        description=(
            "Report whether this Hermes profile is linked to Hussh One. "
            "This returns identifiers and status only, never identity credentials."
        ),
    )
    def identity_status() -> dict[str, Any]:
        return bridge.identity_status()

    @mcp.tool(
        name="hussh_vault_status",
        description=(
            "Report whether the profile-scoped local vault envelope is enrolled "
            "and presently unlocked. Enrollment and unlock happen outside model context."
        ),
    )
    def vault_status() -> dict[str, Any]:
        return bridge.vault_status()

    @mcp.tool(
        name="hussh_pkm_request_scope",
        description=(
            "Describe how to request a consented PKM scope through the canonical "
            "Hussh MCP. Decrypt only after status is granted. A local connector "
            "retains its private export key in secure local storage and decrypts "
            "outside model context so future revisions can be processed; the LLM "
            "must never receive or remember that key."
        ),
    )
    def request_scope(scope: str, purpose: str) -> dict[str, Any]:
        clean_scope = scope.strip()
        clean_purpose = purpose.strip()
        if not clean_scope or not clean_purpose:
            raise ValueError("A scope and purpose are required.")
        return {
            "status": "consent_required",
            "requested_scope": clean_scope,
            "purpose": clean_purpose,
            "canonical_lifecycle": [
                "search-user-scopes",
                "request-consent",
                "check-consent-status",
                "get-encrypted-scoped-export",
            ],
            "decrypt_when": "consent_status=granted",
            "decryption_owner": "trusted_connector_runtime",
            "key_retention": (
                "Keep the connector private key in local secure storage for "
                "authorized export revisions; never place it in model context."
            ),
            "note": "This bridge does not bypass PCHP or return decrypted PKM information.",
        }

    @mcp.tool(
        name="hussh_pkm_propose_write",
        description=(
            "Create an in-memory PKM write proposal. The merge patch is held only "
            "inside this local bridge and is not echoed in the result. This does "
            "not write until hussh_pkm_commit_write elicits explicit user approval."
        ),
    )
    def propose_write(
        domain: str,
        scope_path: str,
        merge_patch: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        bridge.require_vault_key()
        proposal = pkm.propose(
            domain=domain,
            scope_path=scope_path,
            merge_patch=merge_patch,
            summary=summary,
        )
        proposals.put(proposal)
        return proposal.safe_view()

    @mcp.tool(
        name="hussh_pkm_commit_write",
        description=(
            "Show the current domain, path, mutation summary, and export-sharing "
            "impact through Hermes MCP elicitation, then commit only if the user "
            "accepts. The bridge rechecks revisions and sharing impact before save."
        ),
    )
    async def commit_write(proposal_id: str, ctx: Context) -> dict[str, Any]:
        proposal = proposals.take(proposal_id)
        impact = proposal.safe_view()["sharing_impact"]
        message = (
            "Approve this Hussh One PKM update?\n"
            f"Domain: {proposal.domain}\n"
            f"Path: {proposal.scope_path}\n"
            f"Change: {proposal.summary}\n"
            f"Sharing/export impact: {impact['summary']}\n"
            f"Active recipients: {impact['active_recipient_count']}\n"
            "Overlapping exports may be marked stale and refreshed only through "
            "their existing consent path."
        )
        decision = await ctx.elicit(message=message, schema=WriteApproval)
        if decision.action != "accept":
            return {"success": False, "status": "declined", "domain": proposal.domain}
        content = getattr(decision, "data", None) or getattr(decision, "content", None)
        if content is not None and getattr(content, "confirm", True) is False:
            return {"success": False, "status": "declined", "domain": proposal.domain}
        return pkm.commit(proposal)

    @mcp.tool(
        name="hussh_vault_lock",
        description=(
            "Clear the in-process Hussh vault key and identity access token for "
            "this Hermes profile. This does not delete the encrypted local envelope."
        ),
    )
    def vault_lock() -> dict[str, Any]:
        return bridge.lock()

    return mcp


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")
    try:
        _build_server().run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        _logger.exception("Hussh One PKM MCP server stopped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
