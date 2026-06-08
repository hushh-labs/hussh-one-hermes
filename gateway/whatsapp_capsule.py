"""hussh 🤫 One — WhatsApp group "capsule" sandbox resolution.

A *capsule* is a per-group sandbox that lets a non-owner social group (e.g.
"Three Musketeers") be opened to the agent WITHOUT leaking the owner's private
memory, user profile, work/credentials, or the ability to mutate anything.

This module is the single source of truth for:
  * parsing the ``whatsapp.capsules`` config block,
  * resolving a capsule config for a given chat JID,
  * computing the read-only toolset and isolated memory dir for a capsule.

Kept dependency-light so both the gateway and the unit tests can import it
without booting the whole gateway stack. See HUSSH_ONE.md §6 for the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Toolsets a capsule is allowed to use by default: READ-only / public-info only.
# Everything that can mutate state, read the owner's filesystem, reach the
# consent/MCP layer, or send laterally is excluded.
DEFAULT_CAPSULE_ENABLED_TOOLSETS: List[str] = ["web", "vision"]

# Toolsets explicitly stripped from a capsule session even if some upstream
# default tried to add them. Defense-in-depth alongside the allow-list.
DEFAULT_CAPSULE_DISABLED_TOOLSETS: List[str] = [
    "terminal",
    "file",
    "delegation",
    "cronjob",
    "skills",
    "session_search",
    "kanban",
    "spotify",
    "homeassistant",
    "computer_use",
    "messaging",  # blocks send_message at the toolset level too
]

_DEFAULT_CAPSULE_SYSTEM_PROMPT = (
    "You are operating inside a sandboxed social-group capsule. You have NO "
    "access to the owner's personal data, phone numbers, work details, file "
    "contents, credentials, or any global memory. You may read public web "
    "info. You may NOT send messages to other chats, delete or modify files, "
    "run shell commands, or query consent/MCP data. Only answer the casual "
    "question asked, warmly and briefly. Any memory you form stays inside this "
    "capsule."
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _as_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return None


@dataclass
class CapsuleConfig:
    """Resolved configuration for one WhatsApp group capsule."""

    jid: str
    name: str
    memory_dir: str
    skip_global_memory: bool = True
    skip_global_user_profile: bool = True
    enabled_toolsets: List[str] = field(
        default_factory=lambda: list(DEFAULT_CAPSULE_ENABLED_TOOLSETS)
    )
    disabled_toolsets: List[str] = field(
        default_factory=lambda: list(DEFAULT_CAPSULE_DISABLED_TOOLSETS)
    )
    block_outbound_send: bool = True
    system_prompt: str = _DEFAULT_CAPSULE_SYSTEM_PROMPT


def _capsules_block(config: Any) -> Dict[str, Any]:
    """Extract the ``whatsapp.capsules`` dict from a gateway config object.

    Accepts either a raw dict (``{"whatsapp": {"capsules": {...}}}``) or a
    PlatformConfig-style object whose ``extra`` carries ``capsules``. Returns
    an empty dict when nothing is configured.
    """
    if config is None:
        return {}
    # PlatformConfig.extra style (what the WhatsApp adapter sees)
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict) and isinstance(extra.get("capsules"), dict):
        return extra["capsules"]
    # Raw config dict style
    if isinstance(config, dict):
        wa = config.get("whatsapp")
        if isinstance(wa, dict) and isinstance(wa.get("capsules"), dict):
            return wa["capsules"]
        if isinstance(config.get("capsules"), dict):
            return config["capsules"]
    return {}


def resolve_capsule(config: Any, chat_jid: Optional[str]) -> Optional[CapsuleConfig]:
    """Return the CapsuleConfig for ``chat_jid`` if it is configured, else None.

    ``config`` may be the raw gateway config dict or the WhatsApp PlatformConfig.
    Matching is exact on the JID key.
    """
    if not chat_jid:
        return None
    capsules = _capsules_block(config)
    raw = capsules.get(str(chat_jid))
    if not isinstance(raw, dict):
        return None

    name = str(raw.get("name") or str(chat_jid).split("@", 1)[0]).strip()
    memory_dir = str(raw.get("memory_dir") or f"capsules/{name}").strip()

    enabled = _as_list(raw.get("enabled_toolsets"))
    disabled = _as_list(raw.get("disabled_toolsets"))

    return CapsuleConfig(
        jid=str(chat_jid),
        name=name,
        memory_dir=memory_dir,
        skip_global_memory=_as_bool(raw.get("skip_global_memory"), True),
        skip_global_user_profile=_as_bool(raw.get("skip_global_user_profile"), True),
        enabled_toolsets=enabled if enabled is not None else list(DEFAULT_CAPSULE_ENABLED_TOOLSETS),
        disabled_toolsets=disabled if disabled is not None else list(DEFAULT_CAPSULE_DISABLED_TOOLSETS),
        block_outbound_send=_as_bool(raw.get("block_outbound_send"), True),
        system_prompt=str(raw.get("system_prompt") or _DEFAULT_CAPSULE_SYSTEM_PROMPT).strip(),
    )
