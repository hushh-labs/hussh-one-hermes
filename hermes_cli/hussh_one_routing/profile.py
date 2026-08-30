# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Measure what a model can actually do, because nothing will tell you.

This is the stage that makes the harness usable for models that do not exist
yet. Everything else can be reused; this is what a new model needs, and it must
not require a code change to run.

**Capability cannot be read.** ``/v1/models`` exposes exactly three fields -- id,
object, owned_by -- and no capability information at all. ``/api/v0/models``
reports ``capabilities: ["tool_use"]`` for every LLM on this fleet, identically,
including models that behave very differently. So the profile is measured.

**And it cannot be probed by error.** Unknown parameters are dropped with HTTP
200, and ``tool_choice: "required"`` is accepted and then ignored -- prose comes
back with ``finish_reason: "stop"``. A probe that asks "did this error" learns
nothing. Every probe here inspects what came back instead.

**Per feature-combination, never per model.** ``reasoning_effort: "none"``
suppresses reasoning on most of this fleet and is *defeated on gemma-4-e2b when
combined with json_schema* -- 337 reasoning tokens with the schema, 0 without,
same model, same instruction. A per-model answer records the wrong one. So
reasoning suppression is probed twice: alone, and in combination.

The profile is the comparability key. Two runs whose profiles differ were not
asked the same question, and `compare_runs` already refuses to compare across a
differing ``probe_mode`` -- this widens that key rather than adding a second
mechanism beside it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .request import DEFAULT_SERVER_ROOT, Turn, complete

logger = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1

# Generous enough that a refusal is a real refusal and not a budget artifact,
# small enough that a runaway costs seconds.
_PROBE_MAX_TOKENS = 600

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_value",
        "description": "Record a single named value.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["name", "value"],
            "additionalProperties": False,
        },
    },
}

_PROBE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "probe",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"colour": {"type": "string"}},
            "required": ["colour"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class Capability:
    """One measured fact, with the observation that established it.

    `evidence` exists because a bare boolean is unfalsifiable later. When a
    future model behaves oddly, the question is always "what did we actually
    see", and a profile that cannot answer that has to be re-run from scratch.
    """

    name: str
    supported: bool
    evidence: str = ""
    measured: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityProfile:
    """Everything about a model that makes two runs comparable."""

    schema_version: int
    model: str
    capabilities: dict[str, Capability] = field(default_factory=dict)
    # The settings a suite run must use for this model, derived from the probes.
    recommended: dict[str, Any] = field(default_factory=dict)
    failed: bool = False
    failure_reason: str = ""

    def probe_mode(self, suite_id: str, output_protocol: str) -> str:
        """The comparability key handed to `compare_runs`.

        Includes the output protocol because a model asked for a whole file and
        one asked for a region were not asked the same question, and the
        difference between those two framings has already been mistaken for a
        difference between models.
        """
        effort = self.recommended.get("reasoning_effort", "?")
        budget = self.recommended.get("max_tokens", "?")
        return f"{suite_id}/{output_protocol}/effort={effort}/max_tokens={budget}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "capabilities": {k: asdict(v) for k, v in self.capabilities.items()},
            "recommended": dict(self.recommended),
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


def _emitted_tool_call(turn: Turn) -> bool:
    if not turn.tool_calls:
        return False
    first = turn.tool_calls[0] or {}
    return bool(((first.get("function") or {}).get("name")))


def _parses_as_json(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        parts = stripped.split("```")
        stripped = parts[1] if len(parts) > 1 else stripped
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
    try:
        json.loads(stripped.strip())
        return True
    except (TypeError, ValueError):
        return False


def probe_capabilities(
    model: str,
    *,
    base_url: str = DEFAULT_SERVER_ROOT,
    timeout: float = 90.0,
) -> CapabilityProfile:
    """Measure a model's usable surface. Five probes, none skippable."""
    profile = CapabilityProfile(schema_version=PROFILE_SCHEMA_VERSION, model=model)

    def _run(**kwargs: Any) -> Turn:
        return complete(
            model=model,
            max_tokens=_PROBE_MAX_TOKENS,
            base_url=base_url,
            timeout=timeout,
            **kwargs,
        )

    # 1. Does it answer at all? Everything downstream is meaningless otherwise,
    #    and a dead model should fail here rather than as five confusing zeros.
    alive = _run(
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        reasoning_effort="none",
    )
    if not alive.ok:
        profile.failed = True
        profile.failure_reason = alive.error or "model did not respond"
        return profile
    profile.capabilities["responds"] = Capability(
        name="responds",
        supported=True,
        evidence=f"{alive.elapsed_ms:.0f}ms, finish={alive.finish_reason}",
        measured={"elapsed_ms": alive.elapsed_ms},
    )

    # 2. Tool calling. Checked by whether a call came back, never by whether the
    #    request errored -- `tool_choice: "required"` is accepted and ignored on
    #    this fleet, so absence of an error proves nothing.
    tooled = _run(
        messages=[
            {
                "role": "user",
                "content": "Record the value 'blue' under the name 'colour'.",
            }
        ],
        tools=[_PROBE_TOOL],
        reasoning_effort="none",
    )
    profile.capabilities["tool_calling"] = Capability(
        name="tool_calling",
        supported=_emitted_tool_call(tooled),
        evidence=(
            f"tool_calls={len(tooled.tool_calls)} finish={tooled.finish_reason}"
        ),
        measured={"reasoning_tokens": tooled.reasoning_tokens},
    )

    # 3. Structured output.
    schema_turn = _run(
        messages=[{"role": "user", "content": "What colour is a clear midday sky?"}],
        response_format=_PROBE_SCHEMA,
        reasoning_effort="none",
    )
    profile.capabilities["json_schema"] = Capability(
        name="json_schema",
        supported=schema_turn.ok and _parses_as_json(schema_turn.content),
        evidence=f"finish={schema_turn.finish_reason} content={schema_turn.content[:60]!r}",
        measured={"reasoning_tokens": schema_turn.reasoning_tokens},
    )

    # 4 and 5. Reasoning suppression, probed BOTH ways, because this is the
    #    combination that lies. Verified per response via reasoning_tokens
    #    rather than assumed from the request having been accepted.
    plain_effort = tooled.reasoning_tokens
    schema_effort = schema_turn.reasoning_tokens

    profile.capabilities["reasoning_suppression"] = Capability(
        name="reasoning_suppression",
        supported=plain_effort == 0,
        evidence=f"reasoning_tokens={plain_effort} with effort=none, no schema",
        measured={"reasoning_tokens": plain_effort},
    )
    profile.capabilities["reasoning_suppression_with_schema"] = Capability(
        name="reasoning_suppression_with_schema",
        supported=schema_effort == 0,
        evidence=(
            f"reasoning_tokens={schema_effort} with effort=none AND json_schema"
            + (
                " -- suppression DEFEATED by the schema; budget for reasoning"
                if (schema_effort or 0) > 0 and plain_effort == 0
                else ""
            )
        ),
        measured={"reasoning_tokens": schema_effort},
    )

    profile.recommended = _recommend(profile)
    return profile


def _recommend(profile: CapabilityProfile) -> dict[str, Any]:
    """Turn measurements into the settings a suite run must use.

    The budget is the interesting one. A model whose reasoning suppression is
    defeated needs headroom for tokens it will spend before writing anything,
    and giving it the same budget as a model that suppresses cleanly is how a
    capable model gets scored as truncated.
    """
    suppressed = profile.capabilities.get("reasoning_suppression")
    with_schema = profile.capabilities.get("reasoning_suppression_with_schema")
    observed = max(
        (suppressed.measured.get("reasoning_tokens") or 0) if suppressed else 0,
        (with_schema.measured.get("reasoning_tokens") or 0) if with_schema else 0,
    )

    # Headroom for observed reasoning plus room for a real answer. Deliberately
    # generous: an over-budget run costs seconds, an under-budget one publishes
    # a truncation as a model failure.
    budget = 1200 + (observed * 3 if observed else 0)

    tool = profile.capabilities.get("tool_calling")
    schema = profile.capabilities.get("json_schema")
    if tool and tool.supported:
        mode = "tool_calling"
    elif schema and schema.supported:
        # Not scored zero for lacking tools -- tested through the shape it does
        # support. Scoring a capable model 0 because the probe assumed something
        # it does not do is a harness bug published as a model result.
        mode = "json_schema"
    else:
        mode = "text"

    return {
        "probe_shape": mode,
        "reasoning_effort": "none",
        "max_tokens": budget,
        "reasoning_tokens_observed": observed,
    }
