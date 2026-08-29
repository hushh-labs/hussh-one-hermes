# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Keep the agents on this machine matching what the fleet says they should be.

Modelled on the pod, because the pod is the thing here that is actually
maintainable: a declarative surface, versioned, **replaced rather than
patched**. Hermes already works this way -- config is memoised per process, so
applying a change means replacing the process -- and this reuses that instead of
inventing a reload.

It also does not invent an agent format. An agent is already a Hermes profile,
and `distribution.yaml` is already its declarative manifest, with an explicit
split between distribution-owned files and user-owned ones (memories, sessions,
auth, state). The only new artifact is a **fleet manifest**: which agents this
machine should run, and at which ref.

    fleet (desired)  ->  plan  ->  stage  ->  validate  ->  swap  ->  restart

Three properties are load-bearing.

**Provenance, which did not exist.** The installed manifest recorded a source
but never which commit was applied, and staging deleted `.git` before anything
captured it. A machine therefore could not answer "what version am I running",
which makes drift undetectable and a rollback a guess. Sync records the resolved
ref and a content digest.

**User-owned paths are never touched.** Not by convention: the plan refuses to
emit an action that would write one, so a sync that would delete somebody's
memories fails to plan rather than failing halfway through.

**Nothing swaps until it validates.** Staging into place and repairing on
failure means the broken state was live in between. Stage, validate, then swap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

FLEET_FILENAME = "agents.yaml"
STATE_FILENAME = "agent-sync-state.json"
SCHEMA_VERSION = 1

ACTION_INSTALL = "install"
ACTION_UPDATE = "update"
ACTION_NOOP = "noop"
ACTION_REMOVE = "remove"
ACTION_BLOCKED = "blocked"

# Paths inside a profile that belong to the person, not the distribution.
# Mirrors USER_OWNED_EXCLUDE in profile_distribution; duplicated as a guard
# rather than imported, because this list existing in one place was how a sync
# could quietly grow the authority to delete it.
USER_OWNED = frozenset(
    {
        "memories",
        "sessions",
        ".env",
        "auth.json",
        "state.db",
        "logs",
        "workspace",
        "local",
    }
)


@dataclass
class DesiredAgent:
    """One row of the fleet manifest: what this machine should be running."""

    name: str
    source: str = ""
    ref: str = ""
    autostart: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "DesiredAgent":
        if not isinstance(data, dict):
            raise ValueError(f"fleet entry must be a mapping, got {type(data).__name__}")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("fleet entry needs a name")
        return cls(
            name=name,
            source=str(data.get("source") or "").strip(),
            ref=str(data.get("ref") or "").strip(),
            autostart=bool(data.get("autostart")),
        )


@dataclass
class InstalledAgent:
    """What is actually on disk, including where it came from."""

    name: str
    source: str = ""
    applied_ref: str = ""
    applied_digest: str = ""
    installed_at: str = ""


@dataclass
class SyncAction:
    name: str
    action: str
    reason: str
    source: str = ""
    ref: str = ""
    # Populated for BLOCKED so a refusal explains itself rather than just
    # declining.
    blocked_paths: list[str] = field(default_factory=list)


def content_digest(paths: Iterable[Path], *, root: Optional[Path] = None) -> str:
    """A stable digest over distribution-owned file contents.

    Hashes the path RELATIVE to `root`, never the absolute path. Absolute paths
    would give the same distribution a different digest depending on where it
    was installed or staged, so every comparison between a staged tree and the
    live one would report drift and every sync would replace everything. The
    digest exists to detect real change; a location-sensitive one detects
    nothing but its own location.

    Names and contents both, so a rename changes the digest. Sorted, so
    filesystem ordering cannot make two identical trees look different.
    """
    hasher = hashlib.sha256()
    entries = []
    for path in paths:
        if root is not None:
            try:
                name = str(path.relative_to(root))
            except ValueError:
                name = path.name
        else:
            name = path.name
        entries.append((name, path))

    for name, path in sorted(entries, key=lambda pair: pair[0]):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        try:
            if path.is_file():
                hasher.update(path.read_bytes())
            hasher.update(b"\0")
        except OSError:
            # An unreadable file must not hash the same as its absence, or a
            # broken install would look like a clean one.
            hasher.update(b"<unreadable>\0")
    return hasher.hexdigest()[:16]


def _touches_user_owned(relative_paths: Sequence[str]) -> list[str]:
    """Which of these would write something the person owns."""
    offenders = []
    for raw in relative_paths:
        head = Path(str(raw)).parts[0] if Path(str(raw)).parts else str(raw)
        if head in USER_OWNED:
            offenders.append(str(raw))
    return offenders


def plan(
    desired: Sequence[DesiredAgent],
    installed: Sequence[InstalledAgent],
    *,
    owned_paths: Optional[dict[str, Sequence[str]]] = None,
    prune: bool = False,
) -> list[SyncAction]:
    """Work out what to do, without doing any of it.

    Pure: no filesystem, no network. The plan is reviewable before it runs,
    which is the difference between a reconcile loop and an automated mistake.

    `prune` is off by default. An agent on disk but absent from the fleet is
    usually a fleet that has not been updated yet, not an agent to delete, and
    deleting is the one action here that cannot be undone by re-running.
    """
    installed_by_name = {a.name: a for a in installed}
    owned = owned_paths or {}
    actions: list[SyncAction] = []

    for want in desired:
        offenders = _touches_user_owned(owned.get(want.name, []))
        if offenders:
            # Refuse to plan it at all. A sync that discovered this halfway
            # through would already have deleted something.
            actions.append(
                SyncAction(
                    name=want.name,
                    action=ACTION_BLOCKED,
                    reason="distribution claims paths the person owns",
                    source=want.source,
                    ref=want.ref,
                    blocked_paths=offenders,
                )
            )
            continue

        have = installed_by_name.get(want.name)
        if have is None:
            actions.append(
                SyncAction(
                    name=want.name,
                    action=ACTION_INSTALL,
                    reason="not installed",
                    source=want.source,
                    ref=want.ref,
                )
            )
        elif want.ref and have.applied_ref != want.ref:
            actions.append(
                SyncAction(
                    name=want.name,
                    action=ACTION_UPDATE,
                    reason=f"ref {have.applied_ref or 'unknown'} -> {want.ref}",
                    source=want.source,
                    ref=want.ref,
                )
            )
        elif not have.applied_ref:
            # Installed but with no recorded provenance. Not drift exactly, but
            # a machine that cannot say what it runs cannot be trusted to say
            # it is up to date either.
            actions.append(
                SyncAction(
                    name=want.name,
                    action=ACTION_UPDATE,
                    reason="installed without a recorded ref",
                    source=want.source,
                    ref=want.ref,
                )
            )
        else:
            actions.append(
                SyncAction(
                    name=want.name,
                    action=ACTION_NOOP,
                    reason=f"at {have.applied_ref}",
                    source=want.source,
                    ref=want.ref,
                )
            )

    if prune:
        wanted = {a.name for a in desired}
        for have in installed:
            if have.name not in wanted:
                actions.append(
                    SyncAction(
                        name=have.name,
                        action=ACTION_REMOVE,
                        reason="not in the fleet manifest",
                        source=have.source,
                    )
                )
    return actions


def summarize(actions: Sequence[SyncAction]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.action] = counts.get(action.action, 0) + 1
    changes = sum(
        counts.get(k, 0) for k in (ACTION_INSTALL, ACTION_UPDATE, ACTION_REMOVE)
    )
    return {
        "counts": counts,
        "changes": changes,
        "blocked": counts.get(ACTION_BLOCKED, 0),
        # A restart is only warranted when something actually changed. Restarting
        # on a no-op sync would interrupt turns for nothing, which is how a
        # safety mechanism trains people to disable it.
        "restart_required": changes > 0,
    }


def reconcile(
    actions: Sequence[SyncAction],
    *,
    stage: Callable[[SyncAction], Path],
    validate: Callable[[SyncAction, Path], bool],
    swap: Callable[[SyncAction, Path], None],
    discard: Callable[[SyncAction, Path], None],
    resolve_ref: Optional[Callable[[SyncAction, Path], str]] = None,
    digest: Optional[Callable[[SyncAction, Path], str]] = None,
) -> dict[str, Any]:
    """Stage, validate, swap. Never swap something that did not validate.

    Each agent is independent: one failure does not abandon the rest, because a
    half-synced fleet where the failure stopped everything after it is harder to
    reason about than one where each agent's state is its own.
    """
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for action in actions:
        if action.action in (ACTION_NOOP, ACTION_BLOCKED):
            skipped.append({"name": action.name, "action": action.action,
                            "reason": action.reason})
            continue

        staged: Optional[Path] = None
        try:
            staged = stage(action)
            if not validate(action, staged):
                discard(action, staged)
                failed.append(
                    {"name": action.name, "reason": "staged payload failed validation"}
                )
                continue
            # Capture provenance BEFORE the swap. The staging step is where the
            # source metadata still exists; afterwards it is gone, which is
            # exactly how the previous implementation lost it.
            resolved_ref = resolve_ref(action, staged) if resolve_ref else action.ref
            resolved_digest = digest(action, staged) if digest else ""
            swap(action, staged)
            applied.append(
                {
                    "name": action.name,
                    "action": action.action,
                    "applied_ref": resolved_ref,
                    "applied_digest": resolved_digest,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("sync failed for %s", action.name, exc_info=True)
            if staged is not None:
                try:
                    discard(action, staged)
                except Exception:
                    logger.error("could not discard staged payload", exc_info=True)
            failed.append({"name": action.name, "reason": f"{type(exc).__name__}: {exc}"})

    return {
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        # Only a change that actually landed justifies interrupting turns.
        "restart_required": bool(applied),
    }


def write_state(
    path: Path | str, *, result: dict[str, Any], timestamp: Optional[int] = None
) -> dict[str, Any]:
    """Record what this machine is running, so drift becomes answerable."""
    state = {
        "schema_version": SCHEMA_VERSION,
        "at": int(timestamp if timestamp is not None else time.time()),
        "agents": {row["name"]: row for row in result.get("applied", [])},
        "failed": result.get("failed", []),
        "skipped": result.get("skipped", []),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)
    return state


def read_state(path: Path | str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "agents": {}}


def render(actions: Sequence[SyncAction]) -> str:
    lines = []
    for action in actions:
        line = f"  {action.name:24} {action.action:8} {action.reason}"
        if action.blocked_paths:
            line += f" [{', '.join(action.blocked_paths)}]"
        lines.append(line)
    summary = summarize(actions)
    lines.append("")
    lines.append(
        f"  {summary['changes']} change(s), {summary['blocked']} blocked, "
        f"restart {'required' if summary['restart_required'] else 'not needed'}"
    )
    return "\n".join(lines)
