# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Run the Hussh One agent hierarchy on this machine.

The hierarchy is 22 authored agents, each an `agent.yaml` declaring an id, a
parent, a model and a system instruction. That manifest is already the portable
unit: the same file describes the agent in the cloud, in the user's pod, and
here. Nothing needed inventing; what was missing was a runtime on this side that
reads it.

The reason the hierarchy is cloud-bound is narrower than it looks. Each manifest
names a model, and the model names happen to be Gemini's, but the code never
consults the manifest for a PROVIDER at all: it builds one shared client for
every agent in the chain. So "the agents are Gemini-bound" is a property of one
line of wiring, not of the agents.

This loader separates the two. A manifest's model is a *preference*; the
provider is resolved per environment. On a machine running the local model, the
same instruction runs against it, and the agent behaves the same way because the
instruction is the agent.

What this does NOT claim: it does not make the cloud service on-device. That
service keeps its own client. This is the Hermes-side runtime for the same
manifests, which is what "native to Hussh One Hermes" meant.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "agent.yaml"
DEFAULT_ROOT_AGENT = "agent_one"

# Providers that answer without leaving this machine.
LOCAL_PROVIDERS = frozenset({"lmstudio", "lm-studio", "lm_studio", "ollama"})

# Model-name prefixes that imply a provider when a manifest does not name one.
# Most manifests declare only `model:`, so the provider has to be inferred to
# know whether running it would leave the machine.
_MODEL_PROVIDER_HINTS = (
    ("gemini", "gemini"),
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
)


class ManifestError(ValueError):
    """A manifest that cannot be trusted to describe an agent."""


@dataclass
class AgentManifest:
    """One agent, as authored. The same file the cloud and the pod read."""

    id: str
    name: str = ""
    model: str = ""
    provider: str = ""
    parent: Optional[str] = DEFAULT_ROOT_AGENT
    system_instruction: str = ""
    version: str = ""
    subagents: list[str] = field(default_factory=list)
    source_path: str = ""

    @property
    def declared_provider(self) -> str:
        """The provider this manifest implies, named or inferred from the model.

        Inference matters: only three of the manifests write `provider:`, so a
        loader that trusted the field alone would report nineteen agents as
        having no provider and quietly treat them as safe to run anywhere.
        """
        if self.provider:
            return self.provider.strip().casefold()
        lowered = self.model.strip().casefold()
        for prefix, provider in _MODEL_PROVIDER_HINTS:
            if lowered.startswith(prefix):
                return provider
        return ""

    @property
    def leaves_machine_as_authored(self) -> bool:
        provider = self.declared_provider
        return bool(provider) and provider not in LOCAL_PROVIDERS


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_manifest(text: str, *, source_path: str = "") -> AgentManifest:
    """Read the fields this runtime needs from an agent manifest.

    Deliberately a narrow reader rather than a full YAML parse. Hermes does not
    depend on a YAML library, and the alternative -- adding one to read four
    scalars and a block string -- is a dependency the fork would carry through
    every upstream sync. It reads top-level scalars and the `system_instruction`
    block, and ignores everything else rather than guessing at it.
    """
    manifest_id = ""
    fields: dict[str, str] = {}
    instruction_lines: list[str] = []
    subagents: list[str] = []

    in_instruction = False
    instruction_indent: Optional[int] = None
    in_subagents = False

    for raw_line in text.splitlines():
        if in_instruction:
            if not raw_line.strip():
                instruction_lines.append("")
                continue
            indent = len(raw_line) - len(raw_line.lstrip())
            if instruction_indent is None:
                instruction_indent = indent
            if indent >= (instruction_indent or 0):
                instruction_lines.append(raw_line[instruction_indent:])
                continue
            in_instruction = False
            instruction_indent = None

        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Nested `id:` lines under `subagents:` are children, not this agent.
        if in_subagents:
            match = re.match(r"^-?\s*id:\s*(.+)$", stripped)
            if match and (len(raw_line) - len(raw_line.lstrip())) > 0:
                subagents.append(_strip_yaml_scalar(match.group(1)))
                continue
            if (len(raw_line) - len(raw_line.lstrip())) == 0:
                in_subagents = False

        if stripped.rstrip() == "subagents:":
            in_subagents = True
            continue

        if raw_line.startswith("system_instruction:"):
            remainder = raw_line.split(":", 1)[1].strip()
            if remainder in {"|", ">", "|-", ">-"}:
                in_instruction = True
                instruction_indent = None
            elif remainder:
                instruction_lines.append(_strip_yaml_scalar(remainder))
            continue

        match = re.match(r"^([a-z_]+):\s*(.*)$", raw_line)
        if match:
            key, value = match.group(1), _strip_yaml_scalar(match.group(2))
            if key == "id" and not manifest_id:
                manifest_id = value
            elif value:
                fields[key] = value

    if not manifest_id:
        raise ManifestError(f"{source_path or 'manifest'} has no id")

    parent_raw = fields.get("parent", DEFAULT_ROOT_AGENT)
    parent = None if parent_raw in {"null", "~", ""} else parent_raw

    return AgentManifest(
        id=manifest_id,
        name=fields.get("name", ""),
        model=fields.get("model", ""),
        provider=fields.get("provider", ""),
        parent=parent,
        system_instruction="\n".join(instruction_lines).strip(),
        version=fields.get("version", ""),
        subagents=subagents,
        source_path=source_path,
    )


def load_manifests(root: Path | str) -> list[AgentManifest]:
    """Every agent manifest under `root`, sorted by id."""
    manifests: list[AgentManifest] = []
    for path in sorted(Path(root).glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifests.append(
                parse_manifest(path.read_text(encoding="utf-8"), source_path=str(path))
            )
        except (ManifestError, OSError) as exc:
            # A manifest that cannot be read is reported, never skipped
            # silently: a hierarchy missing an agent nobody noticed is worse
            # than one that fails loudly.
            logger.error("could not load %s: %s", path, exc)
            raise
    return manifests


def build_hierarchy(manifests: Sequence[AgentManifest]) -> dict[str, list[str]]:
    """Parent to children. The tree the contract says must be single-rooted."""
    children: dict[str, list[str]] = {}
    for manifest in manifests:
        if manifest.parent:
            children.setdefault(manifest.parent, []).append(manifest.id)
    for parent in children:
        children[parent].sort()
    return children


def resolve_runtime(
    manifest: AgentManifest,
    *,
    on_device: bool,
    local_provider: str = "lmstudio",
    local_model: str = "",
) -> dict[str, Any]:
    """Decide which provider and model actually run this agent.

    The manifest's model is a preference, not a binding. On-device, the same
    instruction runs against the local model, because the instruction is what
    makes the agent that agent -- the weights behind it are an implementation
    detail the manifest has no business fixing for every environment.

    Returns the substitution explicitly, so a caller can report that the agent
    ran on something other than its authored model rather than implying parity
    it did not measure.
    """
    if not on_device:
        return {
            "agent": manifest.id,
            "provider": manifest.declared_provider or "auto",
            "model": manifest.model,
            "substituted": False,
        }
    return {
        "agent": manifest.id,
        "provider": local_provider,
        "model": local_model or manifest.model,
        # True whenever the authored model is not what ran. The caller owes the
        # reader this: an agent running on a different model is a different
        # measurement, and silently swapping it is how a benchmark starts
        # describing a system nobody deployed.
        "substituted": bool(local_model) and local_model != manifest.model,
        "authored_model": manifest.model,
    }


def audit_hierarchy(
    manifests: Sequence[AgentManifest], *, on_device: bool
) -> dict[str, Any]:
    """What the whole hierarchy would do in a given environment."""
    rows = []
    for manifest in manifests:
        runtime = resolve_runtime(manifest, on_device=on_device)
        rows.append(
            {
                "agent": manifest.id,
                "parent": manifest.parent,
                "authored_model": manifest.model,
                "authored_provider": manifest.declared_provider or "(implied)",
                "runs_on": runtime["provider"],
                "leaves_machine": (
                    manifest.leaves_machine_as_authored if not on_device else False
                ),
            }
        )
    return {
        "on_device": on_device,
        "agents": rows,
        "count": len(rows),
        "leaves_machine": sum(1 for r in rows if r["leaves_machine"]),
    }


def run_agent(
    manifest: AgentManifest,
    user_input: str,
    *,
    call: Callable[..., Any],
    provider: str = "lmstudio",
    model: str = "",
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Run one agent's instruction against a model, on this machine.

    `call` is injected rather than imported so the runtime is testable without
    a live server, and so the caller chooses the transport.
    """
    runtime = resolve_runtime(
        manifest, on_device=provider in LOCAL_PROVIDERS, local_provider=provider,
        local_model=model,
    )
    messages = [
        {"role": "system", "content": manifest.system_instruction},
        {"role": "user", "content": user_input},
    ]
    try:
        raw = call(
            model=runtime["model"],
            messages=messages,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001
        return {**runtime, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {**runtime, "ok": True, "output": raw}


def parse_agent_json(raw: str) -> tuple[Optional[Any], str]:
    """Read an agent's JSON reply. Returns (value, error).

    The manifests all say "Return JSON only. Never return markdown", and models
    return fenced markdown anyway, so the fence is stripped rather than treated
    as a failure. A genuinely unparseable reply returns the error instead of
    None-as-success, because an agent that answered nothing and one that
    answered badly need different fixes.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        body = text[3:]
        if body.lstrip().lower().startswith("json"):
            body = body.lstrip()[4:]
        text = body.split("```")[0].strip()
    if not text:
        return None, "empty reply"
    try:
        return json.loads(text), ""
    except ValueError as exc:
        return None, f"unparseable JSON: {exc}"
