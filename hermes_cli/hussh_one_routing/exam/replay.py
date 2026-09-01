# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Replay a real turn from this owner's sessions and grade what comes back.

This is the exam that matters, and it was the piece missing from all of the
others. The suites could grade a `write_file`, a `terminal` call or a tool
choice, but nothing turned a real session moment into a question a model could
be asked. Every model run so far therefore used merge conflicts, which are one
upstream chore and about 0% of what Hermes does day to day.

A replay case is the agent's own history, cut just before it acted:

    the same system prompt, the same conversation, the same tool results,
    the same catalog of up to 232 tools

and the question is the one the product actually asks: **what do you do next?**

That single question exercises everything at once, which is why it is one runner
and not three. Choosing the tool is `tool_select`. The arguments are graded by
whichever suite owns that tool: `terminal` statically checks the command,
`file_edit` parse-checks the write. And because the prefix is the real one, the
context length is the real one too, so long-context behaviour is measured rather
than simulated.

**What the label can and cannot say.** The recorded next action is what a
frontier model did (claude-opus-4-8, the gemini flashes, gemma-4-31b), not what
was correct. Matching it is imitation fidelity. But the *oracles* are not
imitation: a shell command that does not parse, or a write that breaks the file,
is wrong no matter what any model would have done. So this reports two numbers
and never adds them: agreement with the reference, and structural validity.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import build as B
from . import file_edit, terminal, tool_select
from .model import FAIL, PASS, SKIP, Outcome, Verdict

logger = logging.getLogger(__name__)

SUITE_ID = "replay"

# Which suite grades the arguments of which tool.
_ARGUMENT_GRADERS = {
    "terminal": "terminal",
    "mcp_terminal": "terminal",
    "write_file": "file_edit",
    "patch": "file_edit",
    "mcp_write_file": "file_edit",
    "mcp_patch": "file_edit",
}


@dataclass
class ReplayCase:
    """One real moment, with everything the model saw and what happened next."""

    case_id: str
    messages: list
    catalog: list = field(default_factory=list)
    schemas: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)
    expected_tool: Optional[str] = None
    expected_args: dict = field(default_factory=dict)
    wire_chars: int = 0
    reference_model: str = ""
    session_id: str = ""
    known_paths: list = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return B.estimate_tokens("x" * self.wire_chars)

    @property
    def catalog_size(self) -> int:
        return len(self.catalog)


def _known_paths(messages: Sequence[dict]) -> list:
    """Absolute paths that appeared anywhere in the context.

    What `paths_grounded` checks against. Gathered from the real prefix so a
    model is only faulted for inventing a path nobody mentioned, not for using
    one the conversation had already established.
    """
    import re

    blob = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            blob.append(content)
        elif isinstance(content, list):
            blob.append(json.dumps(content))
        if message.get("tool_calls"):
            blob.append(json.dumps(message["tool_calls"]))
    joined = "\n".join(blob)
    return sorted(set(re.findall(r"/(?:[\w.@-]+/)+[\w.@-]+", joined)))[:400]


def extract_cases(
    *,
    root=None,
    max_cases: int = 400,
    max_wire_chars: int = 900_000,
) -> list:
    """Cut every session at each point the agent acted.

    Deduped on the prefix plus the catalog, because the dumps replay full
    history and one decision therefore appears in several of them. Counting the
    replays would weight a single moment as though it were many.

    **Sampled round-robin across sessions, not sequentially.** Taking the first
    N in file order looked fine and was not: it drew 472 of 500 cases from one
    model and every single case from a 29-tool catalog, so an exam meant to
    measure behaviour under a catalog of up to 232 tools contained no large
    catalog at all. Long sessions produce hundreds of decisions and would
    otherwise crowd out every short one.
    """
    by_session: dict = {}
    seen = set()

    for dump in B.iter_dumps(root):
        body = B.request_body(dump)
        if not body:
            continue
        catalog = B.catalog_names(body)
        schemas = B.catalog_schemas(body)
        descriptions = B.catalog_descriptions(body)
        reference_model = str(body.get("model") or "")
        session_id = str(dump.get("session_id") or "")
        messages = body.get("messages") or []

        prefix: list = []
        for message in messages:
            calls = list(B.iter_tool_calls(message)) if message.get(
                "role"
            ) == "assistant" else []
            if calls and prefix:
                name, args, _cid = calls[0]  # the first call is the decision
                if not B.is_truncated(args):
                    wire = len(json.dumps(prefix)) + len(json.dumps(body.get("tools") or []))
                    if wire <= max_wire_chars:
                        key = B.fingerprint(
                            [m.get("content") for m in prefix][-6:],
                            sorted(catalog),
                            name,
                        )
                        if key not in seen:
                            seen.add(key)
                            bucket = by_session.setdefault(session_id, [])
                            bucket.append(
                                ReplayCase(
                                    case_id=f"{session_id[:12]}#{len(bucket)}",
                                    messages=[dict(m) for m in prefix],
                                    catalog=catalog,
                                    schemas=schemas,
                                    descriptions=descriptions,
                                    expected_tool=name,
                                    expected_args=args,
                                    wire_chars=wire,
                                    reference_model=reference_model,
                                    session_id=session_id,
                                    known_paths=_known_paths(prefix),
                                )
                            )
            prefix.append(message)

    return _round_robin(by_session, max_cases)


def _round_robin(by_session: dict, max_cases: int) -> list:
    """One case from each session, then a second from each, until full.

    Keeps a session that produced 300 decisions from drowning out one that
    produced three, so the exam reflects the range of work rather than the
    length of the longest transcript.
    """
    ordered = [list(v) for _k, v in sorted(by_session.items())]
    out: list = []
    depth = 0
    while ordered and len(out) < max_cases:
        progressed = False
        for bucket in ordered:
            if depth < len(bucket):
                out.append(bucket[depth])
                progressed = True
                if len(out) >= max_cases:
                    break
        if not progressed:
            break
        depth += 1
    return out


def tools_payload(case: ReplayCase) -> list:
    """The catalog in OpenAI tool shape, as offered originally.

    The descriptions are the load-bearing part, learned the expensive way. The
    first version hardcoded every description to "" while this docstring
    claimed fidelity, and that erasure cost qwen3.8-27b its ranking: it depends
    on descriptions to pick the specific tool instead of shelling out, and on a
    real failing case restoring them alone flipped it to the reference tool.
    The gemma models tolerate the erasure, which is exactly what made the
    damage invisible: the models that shrugged it off set the curve.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": case.descriptions.get(name, ""),
                "parameters": case.schemas.get(name) or {"type": "object"},
            },
        }
        for name in case.catalog
    ]


def grade(
    case: ReplayCase,
    *,
    chosen: Optional[str],
    arguments: Any = None,
) -> Verdict:
    """Grade one replayed decision.

    The tool choice is scored against the reference, and the arguments are
    handed to whichever suite owns that tool. A model that picks a different
    tool still gets its arguments checked: producing a broken shell command is
    wrong whether or not the reference would have run one.
    """
    verdict = Verdict(case_id=case.case_id, suite=SUITE_ID)
    args = arguments if isinstance(arguments, dict) else {}

    selection = tool_select.grade(
        case_id=case.case_id,
        chosen=chosen,
        arguments=args,
        expected=case.expected_tool,
        catalog=case.catalog,
        schemas=case.schemas,
    )
    verdict.outcomes.extend(selection.outcomes)
    verdict.label_match = selection.label_match

    grader = _ARGUMENT_GRADERS.get(chosen or "")
    if grader == "terminal":
        inner = terminal.grade(
            case_id=case.case_id, args=args, known_paths=case.known_paths
        )
        verdict.outcomes.extend(inner.outcomes)
    elif grader == "file_edit":
        path = args.get("path") or ""
        if path:
            inner = file_edit.grade(case_id=case.case_id, path=path, args=args)
            # Confinement and anchor checks need a pre-image that a replay does
            # not have, so they arrive as SKIP and are kept as SKIP rather than
            # quietly counted as passes.
            verdict.outcomes.extend(inner.outcomes)
    elif chosen:
        verdict.outcomes.append(
            Outcome("argument_depth", SKIP, f"no argument grader for {chosen}")
        )
    return verdict


def summarize(verdicts: Sequence[Verdict]) -> dict:
    """Two numbers, never added together.

    ``agreement`` is imitation of a frontier trajectory. ``structural`` is
    whether the output would have worked. A model can score badly on the first
    and well on the second by doing something different and correct, and
    collapsing them into one figure would hide exactly that.
    """
    gradeable = [v for v in verdicts if not v.indeterminate]
    labelled = [v for v in gradeable if v.label_match is not None]

    def rate(items, predicate):
        return round(sum(1 for i in items if predicate(i)) / len(items), 4) if items else None

    structural_names = {
        "shell_parses", "parses", "no_escaped_delimiter", "no_truncation",
        "no_interactive_command", "background_flag_consistency",
        "no_unrequested_destructive_verb", "bounded_recursive_scan",
        "paths_grounded", "arguments_valid", "no_invented_arguments",
        "tool_in_catalog",
    }

    def structurally_ok(verdict):
        return not any(
            o.outcome == FAIL and o.name in structural_names for o in verdict.outcomes
        )

    per_oracle: dict = {}
    for verdict in gradeable:
        for outcome in verdict.outcomes:
            row = per_oracle.setdefault(outcome.name, {PASS: 0, FAIL: 0, SKIP: 0})
            row[outcome.outcome] += 1

    return {
        "suite": SUITE_ID,
        "offered": len(verdicts),
        "graded": len(gradeable),
        "indeterminate": len(verdicts) - len(gradeable),
        "agreement": rate(labelled, lambda v: v.label_match),
        "structural": rate(gradeable, structurally_ok),
        "per_oracle": per_oracle,
        "caveat": (
            "agreement measures imitation of a frontier trajectory and is not "
            "correctness; structural measures whether the output would have "
            "worked. They are never added."
        ),
    }
