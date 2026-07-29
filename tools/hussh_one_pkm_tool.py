# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Native, service-gated Hussh One Personal Data write capability."""

from __future__ import annotations

from hermes_constants import get_hermes_home

from hermes_cli.hussh_one_pkm.bridge import get_profile_bridge
from hermes_cli.hussh_one_pkm.service import HusshPkmWriteService
from tools.approval import request_fresh_action_consent
from tools.registry import registry, tool_error, tool_result


def _connector_enrolled() -> bool:
    """Side-effect-free availability check; never touches Keychain or network."""
    root = get_hermes_home() / "hussh-one"
    return (root / "identity.json").is_file() and (
        root / "vault-envelope.json"
    ).is_file()


def save_to_pkm(args, **_kwargs) -> str:
    try:
        service = HusshPkmWriteService(
            get_profile_bridge(),
            approve=lambda message, description: request_fresh_action_consent(
                message,
                description,
                surface="hussh-one-pkm-write",
            ),
        )
        return tool_result(
            service.save(
                domain=args.get("domain", ""),
                scope_path=args.get("scope_path", ""),
                merge_patch=args.get("merge_patch"),
                summary=args.get("summary", ""),
                operation=args.get("operation", "upsert"),
            )
        )
    except Exception as exc:
        return tool_error(str(exc))


registry.register(
    name="save_to_pkm",
    toolset="hussh_one",
    schema={
        "name": "save_to_pkm",
        "description": (
            "Propose and, only after a fresh user approval, save one encrypted "
            "update to the active Hermes profile's Hussh One Personal Data. "
            "Use the hosted Hussh Consent MCP for all consented reads. This "
            "tool never returns vault material or decrypted domain snapshots."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "scope_path": {"type": "string"},
                "merge_patch": {"type": "object"},
                "summary": {"type": "string", "maxLength": 500},
                "operation": {
                    "type": "string",
                    "enum": [
                        "upsert",
                        "create",
                        "update",
                        "merge",
                        "delete_path",
                        "delete_scope",
                        "delete_domain",
                    ],
                    "default": "upsert",
                    "description": (
                        "Use upsert unless the owner explicitly requested a "
                        "specific create, merge, or delete operation."
                    ),
                },
            },
            "required": ["domain", "scope_path", "merge_patch", "summary"],
            "additionalProperties": False,
        },
    },
    handler=save_to_pkm,
    check_fn=_connector_enrolled,
    emoji="🤫",
)
