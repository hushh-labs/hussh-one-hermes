# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Pick the right tool out of a catalog that can hold 232 of them.

Choosing well under a large catalog is a distinct skill from using a tool well,
and it is the one small models are most likely to lose. Real catalogs here range
into the hundreds once the MCP servers are attached: browser, wiki, plaid,
shadcn, consent, devtools, plus the built-ins.

**The labels are not ground truth and this suite must never pretend otherwise.**
Each label is the tool a *frontier* model actually called at that point:
claude-opus-4-8 in 527 cases, the gemini flashes in 765, gemma-4-31b in 54. A
local model that picks a better tool than the recorded one is scored as wrong
here. So this measures **imitation fidelity under catalog load**, not
competence, and every number it produces carries that sentence.

Two things in the data constrain what can honestly be claimed:

**Catalog size never varies inside a session.** So catalog size is perfectly
confounded with session identity, and "accuracy drops as the catalog grows"
cannot be separated from "accuracy drops on the sessions that happen to have big
catalogs". Reported as a correlation, never as an effect.

**The escape-hatch hypothesis did not survive contact.** The obvious prediction
is that a struggling model reaches for `terminal` instead of the specific tool.
Measured on 829 real terminal calls: 43 read a file through the shell, but 42 of
those `cat` multiple files or pipe, which `read_file` cannot do. The strict
single-file no-pipe count is **1 of 829**, and grep-while-`search_files`-was-
offered is **0 of 829**. The frontier models are not doing this, so a check for
it would measure almost nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from .model import FAIL, PASS, SKIP, Outcome, Verdict

logger = logging.getLogger(__name__)

SUITE_ID = "tool_select"

# Tools grouped by what they accomplish. A model that reaches for `search_files`
# where the reference used `read_file` got the intent right and the instrument
# wrong, which is a different and much smaller error than calling `browser_type`
# to read a file.
FAMILIES = {
    "read": {"read_file", "search_files", "session_search", "web_extract",
             "wiki_read", "mcp_hushh_wiki_wiki_read"},
    "write": {"write_file", "patch", "skill_manage", "wiki_write",
              "mcp_hushh_wiki_wiki_write", "mcp_hushh_wiki_wiki_patch"},
    "execute": {"terminal", "execute_code", "process"},
    "browse": {"browser_navigate", "browser_click", "browser_snapshot",
               "browser_type", "browser_scroll", "browser_press",
               "browser_vision", "browser_console", "browser_back",
               "browser_get_images", "web_search"},
    "delegate": {"delegate_task", "clarify", "todo", "cronjob"},
    "memory": {"memory", "skills_list", "skill_view"},
}

CANNOT_CATCH = (
    "Whether the label was the best tool. It is one frontier trajectory, so a "
    "model that picks better is scored wrong and there is no reward for "
    "genuinely superior play.",
    "Whether the chosen tool would have succeeded. No responses were recorded "
    "in these dumps, so nothing here observes an outcome.",
    "Any effect of catalog size, because catalog size never varies inside a "
    "session and is therefore confounded with session identity.",
)


def _family_of(name: str) -> str:
    for family, members in FAMILIES.items():
        if name in members:
            return family
    for family, members in FAMILIES.items():
        # MCP-prefixed variants of the same underlying tool.
        if any(name.endswith(member) for member in members):
            return family
    return ""


def check_name_in_catalog(chosen: Optional[str], catalog: Sequence[str]) -> Outcome:
    """The tool must exist. A hallucinated name is not a near miss.

    Ranked ahead of exact-match on purpose: calling a tool that was never
    offered is a different failure from calling the wrong real one, and the
    fixes differ too.
    """
    if not catalog:
        return Outcome("tool_in_catalog", SKIP, "no catalog recorded")
    if chosen is None:
        return Outcome("tool_in_catalog", SKIP, "no tool called")
    if chosen not in catalog:
        return Outcome(
            "tool_in_catalog", FAIL,
            f"{chosen!r} was not among the {len(catalog)} tools offered",
        )
    return Outcome("tool_in_catalog", PASS)


def check_name_matches(chosen: Optional[str], expected: Optional[str]) -> Outcome:
    """The chosen tool is the one the reference trajectory used."""
    if expected is None:
        return Outcome("tool_name_correct", SKIP, "no label for this case")
    if chosen is None:
        return Outcome("tool_name_correct", FAIL, f"called nothing; expected {expected}")
    if chosen != expected:
        return Outcome(
            "tool_name_correct", FAIL, f"called {chosen}, reference used {expected}"
        )
    return Outcome("tool_name_correct", PASS)


def check_family_matches(chosen: Optional[str], expected: Optional[str]) -> Outcome:
    """A softer signal: right kind of action, wrong instrument.

    Worth separating because the two failures need different fixes. Reaching for
    `search_files` where the reference read a file is a slip; reaching for
    `browser_type` is a misread of the whole task.
    """
    if not chosen or not expected:
        return Outcome("tool_family_match", SKIP, "nothing to compare")
    chosen_family, expected_family = _family_of(chosen), _family_of(expected)
    if not chosen_family or not expected_family:
        return Outcome("tool_family_match", SKIP, "tool not in a known family")
    if chosen_family != expected_family:
        return Outcome(
            "tool_family_match", FAIL,
            f"{chosen} is a {chosen_family} action; the reference took a "
            f"{expected_family} action",
        )
    return Outcome("tool_family_match", PASS)


def check_arguments_validate(
    chosen: Optional[str], arguments: Any, schemas: dict
) -> Outcome:
    """Arguments must satisfy the schema of the tool that was offered.

    Validated against the catalog the model was actually given, not against a
    canonical copy: the catalogs in this corpus carry several schema revisions
    of the same tool, and checking against the wrong revision would fail a model
    for obeying the instructions it was handed.
    """
    if not chosen:
        return Outcome("arguments_valid", SKIP, "no tool called")
    schema = schemas.get(chosen)
    if not schema:
        return Outcome("arguments_valid", SKIP, f"no schema offered for {chosen}")
    if not isinstance(arguments, dict):
        return Outcome("arguments_valid", FAIL, "arguments were not an object")
    try:
        import jsonschema

        jsonschema.validate(instance=arguments, schema=schema)
    except ImportError:  # pragma: no cover
        return Outcome("arguments_valid", SKIP, "jsonschema unavailable")
    except Exception as exc:  # noqa: BLE001
        message = str(exc).splitlines()[0] if str(exc) else "schema violation"
        return Outcome("arguments_valid", FAIL, message[:160])
    return Outcome("arguments_valid", PASS)


def check_no_extra_keys(
    chosen: Optional[str], arguments: Any, schemas: dict
) -> Outcome:
    """No invented parameters.

    Separate from schema validation because most real schemas do not set
    ``additionalProperties: false``, so an invented key passes validation
    silently and then gets dropped by the server. The model believes it asked
    for something it did not.
    """
    if not chosen or not isinstance(arguments, dict):
        return Outcome("no_invented_arguments", SKIP, "nothing to check")
    schema = schemas.get(chosen) or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return Outcome("no_invented_arguments", SKIP, "schema declares no properties")
    extra = sorted(set(arguments) - set(properties))
    if extra:
        return Outcome(
            "no_invented_arguments", FAIL,
            f"parameters not in the schema: {', '.join(extra[:4])}",
        )
    return Outcome("no_invented_arguments", PASS)


def check_abstains(chosen: Optional[str], expected: Optional[str]) -> Outcome:
    """Calling no tool when no tool was the right answer.

    The negative control for this suite. Without it a model that calls something
    on every single turn can score well, and over-calling is the characteristic
    small-model failure under a large catalog.
    """
    if expected is not None:
        return Outcome("abstains_when_no_tool_fits", SKIP, "a tool was expected")
    if chosen is not None:
        return Outcome(
            "abstains_when_no_tool_fits", FAIL,
            f"called {chosen} where the reference called nothing",
        )
    return Outcome("abstains_when_no_tool_fits", PASS)


def grade(
    *,
    case_id: str,
    chosen: Optional[str],
    arguments: Any = None,
    expected: Optional[str] = None,
    catalog: Sequence[str] = (),
    schemas: Optional[dict] = None,
) -> Verdict:
    """Grade one tool choice against the reference trajectory."""
    schema_map = schemas or {}
    verdict = Verdict(case_id=case_id, suite=SUITE_ID)
    verdict.outcomes = [
        check_name_in_catalog(chosen, catalog),
        check_name_matches(chosen, expected),
        check_family_matches(chosen, expected),
        check_arguments_validate(chosen, arguments, schema_map),
        check_no_extra_keys(chosen, arguments, schema_map),
        check_abstains(chosen, expected),
    ]
    if expected is not None:
        verdict.label_match = chosen == expected
    return verdict
