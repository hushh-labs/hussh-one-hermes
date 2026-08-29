# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade the small on-device model's work with a stronger model.

The on-device benchmark answers "did the small model emit a well-formed
save_to_pkm call, and how fast". It cannot answer the question that decides
whether Puppy One is any good: **was the save correct** -- the right domain, a
scope path that means something, a patch that captures the fact and nothing
else, and no violation of the rules the agent was given.

Structural validity is cheap to check and easy to satisfy while being wrong.
A model can emit a perfectly-shaped call that files a dietary restriction under
`finance.accounts` and the shape check will pass it.

So a stronger model reads the small model's output and grades it. Its verdicts
accumulate into a regression corpus, and that corpus is what makes the next
iteration better: every case the small model gets wrong becomes a fixture that
proves whether a prompt change, a model swap or a quant change actually helped.

Three rules keep the judge honest, because a judge that rubber-stamps is worse
than no judge -- it manufactures evidence.

1. **The judge may not be the answerer.** If the grader and the graded resolve
   to the same model, the run is refused. This is not hypothetical: the
   compaction eval in this repo routes its judge, its answerer and its
   question-generator through one `call_llm(task="compression")`, so the model
   grades itself and the score means nothing.

2. **An adverse verdict must cite.** The judge names the rule broken and quotes
   the span that breaks it. A verdict with no citation is discarded rather than
   counted, because an uncited failure is indistinguishable from a hallucinated
   one.

3. **Negative controls decide whether the run counts.** Known-bad outputs are
   graded alongside the real ones. If the judge passes a planted failure, the
   judge is broken and the whole run is void -- reported as void, never
   published with the real scores attached.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# The auxiliary task name the judge routes under. Pinning
# `auxiliary.pkm_judge.provider/model` in config aims the judge at a strong
# model without touching any caller.
JUDGE_TASK = "pkm_judge"

# A judge verdict is one of these. Deliberately coarse: a fine-grained score
# invites the judge to average its way out of a decision, and what a regression
# corpus needs is a label, not a feeling.
VERDICT_CORRECT = "correct"
VERDICT_WRONG = "wrong"
VERDICT_UNSURE = "unsure"
_VERDICTS = frozenset({VERDICT_CORRECT, VERDICT_WRONG, VERDICT_UNSURE})


class JudgeIsTheAnswerer(RuntimeError):
    """Raised when the grader and the graded resolve to the same model."""


@dataclass
class GradedCase:
    """One small-model output and what the judge made of it."""

    case_id: str
    model: str
    verdict: str
    rule: str = ""
    citation: str = ""
    note: str = ""
    counted: bool = True
    # Set for planted failures. These never enter the score; they decide
    # whether the score is trustworthy at all.
    control: Optional[str] = None


@dataclass
class JudgeReport:
    judge_model: str
    answerer_model: str
    cases: list[GradedCase] = field(default_factory=list)
    void: bool = False
    void_reason: str = ""

    def scoreboard(self) -> dict[str, Any]:
        """The numbers, or an explicit refusal to produce them."""
        if self.void:
            # A void run publishes no accuracy. Emitting one with a caveat
            # attached invites the number to be quoted without the caveat.
            return {
                "void": True,
                "reason": self.void_reason,
                "judge_model": self.judge_model,
                "answerer_model": self.answerer_model,
                "accuracy": None,
            }
        real = [c for c in self.cases if c.control is None and c.counted]
        correct = [c for c in real if c.verdict == VERDICT_CORRECT]
        unsure = [c for c in real if c.verdict == VERDICT_UNSURE]
        discarded = [c for c in self.cases if c.control is None and not c.counted]
        return {
            "void": False,
            "judge_model": self.judge_model,
            "answerer_model": self.answerer_model,
            "graded": len(real),
            "correct": len(correct),
            # Unsure counts against accuracy. Treating it as a pass would let
            # a hedging judge inflate the score for free.
            "accuracy": round(len(correct) / len(real), 4) if real else None,
            "unsure": len(unsure),
            "discarded_uncited": len(discarded),
            "failures": [
                {
                    "case_id": c.case_id,
                    "rule": c.rule,
                    "citation": c.citation,
                    "note": c.note,
                }
                for c in real
                if c.verdict == VERDICT_WRONG
            ],
        }

    def regression_corpus(self) -> list[dict[str, Any]]:
        """Every case the small model got wrong, as reusable fixtures.

        This is the compounding part. A failure that is only ever a number in a
        report teaches nothing; the same failure as a fixture proves whether the
        next prompt or model actually fixed it.
        """
        if self.void:
            return []
        return [
            {
                "case_id": c.case_id,
                "model": c.model,
                "rule": c.rule,
                "citation": c.citation,
                "note": c.note,
            }
            for c in self.cases
            if c.control is None and c.counted and c.verdict == VERDICT_WRONG
        ]


# Planted failures. Each is something the agent's own instruction forbids in
# so many words, so a judge that misses one is not being strict-but-different,
# it is not reading.
NEGATIVE_CONTROLS: tuple[dict[str, Any], ...] = (
    {
        "id": "control-wrong-domain",
        "utterance": "I stopped eating dairy in January.",
        "output": {
            "domain": "finance",
            "scope_path": "finance.accounts.primary",
            "merge_patch": {"dairy": "avoided"},
            "summary": "Stopped eating dairy.",
        },
        "must_catch": "a dietary fact filed under finance",
    },
    {
        "id": "control-invented-fact",
        "utterance": "Always book me an aisle seat.",
        "output": {
            "domain": "travel",
            "scope_path": "travel.preferences.seat",
            "merge_patch": {
                "seat": "aisle",
                "frequent_flyer_number": "AA-4471182",
                "passport_expiry": "2031-04-02",
            },
            "summary": "Aisle seat, frequent flyer and passport on file.",
        },
        "must_catch": "invented values the owner never said",
    },
    {
        "id": "control-operational-request",
        "utterance": "Remind me to call the dentist tomorrow.",
        "output": {
            "domain": "health",
            "scope_path": "health.reminders.dentist",
            "merge_patch": {"task": "call the dentist"},
            "summary": "Reminder to call the dentist.",
        },
        "must_catch": "a one-off reminder saved as durable memory",
    },
    {
        "id": "control-internal-metadata",
        "utterance": "My daughter Maya started at Berkeley.",
        "output": {
            "domain": "relationships",
            "scope_path": "relationships.children.maya",
            "merge_patch": {
                "school": "Berkeley",
                "_parser_version": "3.1.7",
                "_trace_id": "9f2c-aa41-int",
            },
            "summary": "Maya at Berkeley.",
        },
        "must_catch": "developer metadata written into user-facing memory",
    },
)

# Known-GOOD outputs the judge must NOT flag.
#
# Negative controls alone catch a rubber-stamper, and nothing else. A judge that
# has been told to hunt for planted failures can flag every correct row and
# still pass a negative-only gate perfectly -- so the design would have had no
# false-positive rate at all, and "found 9 problems" would look like diligence
# rather than noise.
#
# These are deliberately plain and unambiguous. A positive control that a
# careful judge could reasonably fault would punish good judgement, which is the
# opposite of the point.
POSITIVE_CONTROLS: tuple[dict[str, Any], ...] = (
    {
        "id": "control-clean-preference",
        "utterance": "I prefer window seats on flights.",
        "output": {
            "domain": "travel",
            "scope_path": "travel.preferences.seat",
            "merge_patch": {"seat": "window"},
            "summary": "Prefers a window seat on flights.",
            "operation": "upsert",
        },
        "must_not_flag": "a minimal, faithful, correctly-domained save",
    },
    {
        "id": "control-clean-allergy",
        "utterance": "I'm allergic to penicillin.",
        "output": {
            "domain": "health",
            "scope_path": "health.allergies.penicillin",
            "merge_patch": {"allergen": "penicillin"},
            "summary": "Allergic to penicillin.",
            "operation": "upsert",
        },
        "must_not_flag": "a durable medical fact recorded exactly as stated",
    },
)

# The rules the judge grades against. Lifted from the shared PKM kernel the
# agent manifests actually carry, so the judge holds the model to the
# instruction it was given rather than to the judge's own taste.
_GRADING_RULES = """\
1. right-domain    The domain and scope_path must match what the owner said.
2. no-invention    Only facts present in the utterance. No invented values.
3. durable-only    No reminders, one-off tasks, secrets, or operational requests.
4. no-metadata     No parser metadata, hashes, trace ids, or internal paths.
5. minimal-patch   The patch captures the fact and nothing beyond it.
6. faithful-summary The summary describes what is actually being saved."""

_JUDGE_INSTRUCTION = f"""\
You are grading one PKM write produced by a small on-device model against the \
rules its agent was given. You are not rewriting it and not being helpful; you \
are deciding whether it is correct.

RULES
{_GRADING_RULES}

Return JSON only, no prose:
{{"verdict": "correct" | "wrong" | "unsure",
  "rule": "<the rule id broken, empty when correct>",
  "citation": "<exact substring of the output that breaks it, empty when correct>",
  "note": "<one short sentence>"}}

A "wrong" verdict REQUIRES a rule and a citation quoting the offending value \
verbatim from the output. If you cannot quote it, the verdict is "unsure".
Do not mark something wrong for style, verbosity, or a choice you would have \
made differently. Grade only the rules above."""


def assert_distinct_models(judge_model: str, answerer_model: str) -> None:
    """Refuse a run where the grader and the graded are the same model.

    Self-grading produces a number that looks like a measurement and is not
    one. Failing closed here is the difference between an eval and a mirror.
    """
    judge = (judge_model or "").strip().casefold()
    answerer = (answerer_model or "").strip().casefold()
    if not judge:
        raise JudgeIsTheAnswerer("no judge model configured; refusing to self-grade")
    if judge == answerer:
        raise JudgeIsTheAnswerer(
            f"judge and answerer are both {judge_model!r}; a model cannot grade "
            "itself and produce a number worth reporting"
        )


def parse_verdict(raw: Any) -> dict[str, Any]:
    """Read one judge response, refusing anything that is not a real verdict."""
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        # Models wrap JSON in fences even when told not to.
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text[3:]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        try:
            payload = json.loads(text.strip())
        except (TypeError, ValueError):
            return {"verdict": VERDICT_UNSURE, "rule": "", "citation": "", "note": "unparseable verdict"}
    if not isinstance(payload, dict):
        return {"verdict": VERDICT_UNSURE, "rule": "", "citation": "", "note": "verdict was not an object"}

    verdict = str(payload.get("verdict") or "").strip().casefold()
    if verdict not in _VERDICTS:
        return {"verdict": VERDICT_UNSURE, "rule": "", "citation": "", "note": f"unknown verdict {verdict!r}"}
    return {
        "verdict": verdict,
        "rule": str(payload.get("rule") or "").strip(),
        "citation": str(payload.get("citation") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
    }


def _citation_is_real(citation: str, output: Any) -> bool:
    """A citation must actually appear in what was graded.

    Without this the judge can invent the evidence for its own verdict, which
    is the failure mode that makes an LLM grader worse than no grader.
    """
    if not citation:
        return False
    haystack = json.dumps(output, sort_keys=True).casefold()
    return citation.strip().casefold().strip('"') in haystack


def grade_one(
    *,
    case_id: str,
    model: str,
    utterance: str,
    output: Any,
    ask_judge: Callable[[str], Any],
    control: Optional[str] = None,
) -> GradedCase:
    """Grade a single output. Never raises: a broken judge call is `unsure`."""
    prompt = (
        f"{_JUDGE_INSTRUCTION}\n\n"
        f"OWNER SAID:\n{utterance}\n\n"
        f"MODEL PRODUCED:\n{json.dumps(output, indent=2, sort_keys=True)}"
    )
    try:
        parsed = parse_verdict(ask_judge(prompt))
    except Exception as exc:
        logger.debug("judge call failed for %s", case_id, exc_info=True)
        return GradedCase(
            case_id=case_id,
            model=model,
            verdict=VERDICT_UNSURE,
            note=f"judge unavailable: {type(exc).__name__}",
            control=control,
        )

    verdict = parsed["verdict"]
    counted = True
    note = parsed["note"]
    if verdict == VERDICT_WRONG and not _citation_is_real(parsed["citation"], output):
        # An uncited failure is indistinguishable from a hallucinated one, so
        # it is discarded rather than counted against the small model.
        counted = False
        note = (note + " [discarded: citation not found in output]").strip()

    return GradedCase(
        case_id=case_id,
        model=model,
        verdict=verdict,
        rule=parsed["rule"],
        citation=parsed["citation"],
        note=note,
        counted=counted,
        control=control,
    )


def run_judgement(
    *,
    judge_model: str,
    answerer_model: str,
    cases: Sequence[dict[str, Any]],
    ask_judge: Callable[[str], Any],
    controls: Sequence[dict[str, Any]] = NEGATIVE_CONTROLS,
) -> JudgeReport:
    """Grade every case, with planted failures deciding whether it counts.

    `cases` are dicts with `id`, `utterance` and `output`.
    """
    assert_distinct_models(judge_model, answerer_model)
    report = JudgeReport(judge_model=judge_model, answerer_model=answerer_model)

    for control in controls:
        report.cases.append(
            grade_one(
                case_id=control["id"],
                model=answerer_model,
                utterance=control["utterance"],
                output=control["output"],
                ask_judge=ask_judge,
                control=control["must_catch"],
            )
        )

    missed = [
        c for c in report.cases if c.control is not None and c.verdict != VERDICT_WRONG
    ]
    if missed:
        # The judge waved through something the agent's own instruction
        # forbids in plain words. Nothing it said about the real cases is
        # worth reporting.
        report.void = True
        report.void_reason = (
            "judge passed planted failures it was required to catch: "
            + "; ".join(f"{c.case_id} ({c.control})" for c in missed)
        )
        logger.warning("judgement void: %s", report.void_reason)
        return report

    for case in cases:
        report.cases.append(
            grade_one(
                case_id=str(case.get("id") or "case"),
                model=answerer_model,
                utterance=str(case.get("utterance") or ""),
                output=case.get("output"),
                ask_judge=ask_judge,
            )
        )
    return report


def make_hermes_judge(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    timeout: float = 180.0,
) -> Callable[[str], Any]:
    """An `ask_judge` backed by Hermes's auxiliary routing.

    Routes under `auxiliary.pkm_judge`, so the judge is aimed at a strong model
    from config rather than from a caller. The judge is the one call in this
    system that is expected to leave the machine: grading is not the owner's
    data being processed for them, it is a developer measuring the model. Keep
    it out of any flow the on-device gate protects.
    """

    def _ask(prompt: str) -> Any:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task=JUDGE_TASK,
            provider=provider,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            timeout=timeout,
        )
        # call_llm returns an OpenAI-shaped response; fall back to str() so a
        # differently-shaped provider still yields something parseable.
        try:
            return response.choices[0].message.content
        except Exception:
            return str(response)

    return _ask
