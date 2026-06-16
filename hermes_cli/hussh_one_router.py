"""🤫 Hussh One — Intelligent Workload and Intent Router.

This module implements the custom workload-gated routing capability for the
🤫 Hussh One variant of Hermes Agent. It analyzes incoming prompts for complexity
and intent to determine the optimal execution model dynamically:

  * Low complexity (chit-chat, simple queries) -> Gemini 3.5 Flash (low cost, fast)
  * High complexity (coding, filesystem edits, deploy, deep reasoning) ->
    Claude Opus on GCP Vertex AI (deep reasoning)

Design goals (top-notch performance/accuracy ratio):

  * ZERO extra network round-trips on the hot path. Routing is a synchronous,
    weighted, confidence-scored rule engine — no per-turn classifier LLM call.
    (The previous LLM-classifier approach added a full model call per message,
    was the source of an ``await``-on-sync crash, and on failure mis-routed to a
    broken ``google-vertex`` endpoint that 404'd. Replaced wholesale.)
  * FAIL-SAFE: any internal error returns the safe Gemini default. The router
    must NEVER crash a turn.
  * UPGRADE-SAFE: lives purely in the overlay brand layer; emits only the
    runtime keys the gateway already understands.
  * Vertex-correct: the escalated Claude runtime is resolved as the provider
    profile ``google-vertex-claude`` with ``api_mode=anthropic_messages``,
    ``api_key=gcp-sdk`` and a real regional Vertex base URL.

The exported ``route_workload`` remains an async coroutine for backwards
compatibility with the gateway call site, but it performs no awaiting I/O.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("gateway.hussh_one_router")

# Default model definitions for the routing tiers
MODEL_LOW = "gemini-3.5-flash"
MODEL_HIGH = "claude-opus-4-8"

# Escalation threshold on the normalized confidence score [0.0, 1.0].
# Per the owner's directive we keep a GENEROUS bar: borderline prompts that
# *might* need real reasoning escalate to the heavy model. Tuned against the
# 100-case harness in tests/hussh_one_router.py.
DEFAULT_ESCALATION_THRESHOLD = 0.27

# ---------------------------------------------------------------------------
# Signal lexicons. Each tier carries a weight; the classifier sums the weights
# of matched signals, normalizes, and compares against the threshold.
# ---------------------------------------------------------------------------

# Strong signals: almost always require tools / deep reasoning.
_STRONG_SIGNALS = {
    "code", "coding", "program", "script", "function", "compile", "build",
    "debug", "refactor", "patch", "commit", "git", "pull request", "deploy",
    "deployment", "gcp", "cloud run", "terraform", "kubernetes", "docker",
    "database", "migration", "schema", "sql", "api", "endpoint", "backend",
    "frontend", "test", "tests", "unit test", "integration test", "pytest",
    "audit", "scaffold", "implement", "architecture", "roadmap", "design doc",
    "benchmark", "optimize", "optimization", "profiling", "regression",
    "confidence", "accuracy", "edge case", "edge cases", "test case",
    "test cases", "pipeline", "ci", "workflow", "mcp", "vertex", "adapter",
    "fix the", "root cause", "stack trace", "traceback", "exception",
}

# Medium signals: filesystem / action verbs and analytical asks.
_MEDIUM_SIGNALS = {
    "file", "files", "directory", "folder", "write", "edit", "create",
    "update", "modify", "delete", "rename", "move", "run", "execute",
    "install", "configure", "config", "setup", "verify", "validate",
    "analyze", "analyse", "investigate", "diagnose", "research", "compare",
    "evaluate", "plan", "strategy", "reorganize", "organize", "generate",
    "extract", "parse", "convert", "transform", "calculate", "compute",
    "review", "explain why", "step by step", "walk me through", "trade-off",
    "tradeoff", "tradeoffs", "pros and cons", "profile", "bottleneck",
    "memory leak", "find the", "what's wrong", "whats wrong", "clean it",
}

# Weak signals: light analytical lean; nudge but rarely decide alone.
_WEAK_SIGNALS = {
    "how", "why", "what if", "should i", "recommend", "suggest", "best way",
    "difference between", "summarize", "summary", "draft", "improve",
}

# Casual / low-complexity markers: pull the score DOWN.
_CASUAL_SIGNALS = {
    "hi", "hii", "hello", "hey", "yo", "sup", "thanks", "thank you", "thx",
    "ok", "okay", "cool", "nice", "great", "good morning", "good night",
    "gm", "gn", "lol", "haha", "👍", "❤️", "🙏", "how are you", "wassup",
    "what's up", "who are you", "your name", "good evening",
}

_WEIGHTS = {
    "strong": 1.0,
    "medium": 0.55,
    "weak": 0.20,
    "casual": -0.70,
}

# Tokens we strip before scoring so trigger handles never inflate complexity.
_TRIGGER_RE = re.compile(r"@(oneteam|one|husshone|hussh-one)\b", re.IGNORECASE)


def _phrase_present(text: str, phrase: str) -> bool:
    """Whole-word/phrase membership test that tolerates punctuation."""
    if " " in phrase:
        return phrase in text
    return re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text) is not None


def classify_complexity(message: str) -> tuple[str, float, dict[str, Any]]:
    """Score a prompt and return (complexity, confidence, debug_signals).

    confidence is the normalized escalation score in [0.0, 1.0]; complexity is
    "high" when confidence >= DEFAULT_ESCALATION_THRESHOLD else "low".
    """
    raw = (message or "").strip()
    if not raw:
        return "low", 0.0, {"reason": "empty"}

    text = _TRIGGER_RE.sub(" ", raw).lower()

    matched: dict[str, list[str]] = {"strong": [], "medium": [], "weak": [], "casual": []}
    for sig in _STRONG_SIGNALS:
        if _phrase_present(text, sig):
            matched["strong"].append(sig)
    for sig in _MEDIUM_SIGNALS:
        if _phrase_present(text, sig):
            matched["medium"].append(sig)
    for sig in _WEAK_SIGNALS:
        if _phrase_present(text, sig):
            matched["weak"].append(sig)
    for sig in _CASUAL_SIGNALS:
        if _phrase_present(text, sig):
            matched["casual"].append(sig)

    # Raw weighted score.
    score = 0.0
    score += min(len(matched["strong"]), 3) * _WEIGHTS["strong"]
    score += min(len(matched["medium"]), 3) * _WEIGHTS["medium"]
    score += min(len(matched["weak"]), 2) * _WEIGHTS["weak"]

    word_count = len(text.split())

    # Casual pull. Applies when there's little real complexity. Also applies to
    # SHORT casual-dominated messages even if one stray strong/medium token
    # matched (e.g. "thanks, that test was fun!" — "test" is incidental).
    casual_n = min(len(matched["casual"]), 2)
    short_casual_override = (
        matched["casual"]
        and word_count <= 7
        and len(matched["strong"]) <= 1
        and not matched["medium"]
    )
    if (not matched["strong"] and len(matched["medium"]) <= 1) or short_casual_override:
        score += casual_n * _WEIGHTS["casual"]
        if short_casual_override:
            # Extra damping so an incidental strong word can't clear the bar.
            score -= 0.6
    # Length heuristic: substantial multi-sentence asks lean complex — but only
    # when at least one real work signal is present, so a long casual ramble
    # ("so i was thinking about the weekend...") doesn't escalate on length alone.
    has_work_signal = bool(matched["strong"] or matched["medium"])
    if has_work_signal:
        if word_count >= 40:
            score += 0.5
        elif word_count >= 20:
            score += 0.25

    # Imperative multi-step markers ("and ... and ...", numbered lists).
    if re.search(r"\b(and|then|also|plus)\b.*\b(and|then|also|plus)\b", text):
        score += 0.3
    if re.search(r"(^|\s)\d+[\.\)]\s", raw):
        # An explicit numbered task list is a strong multi-step work signal.
        score += 0.5

    # Normalize: a single strong signal (1.0) should already clear the generous
    # bar; cap the denominator so confidence saturates sensibly.
    confidence = max(0.0, min(score / 2.0, 1.0))
    complexity = "high" if confidence >= DEFAULT_ESCALATION_THRESHOLD else "low"

    debug = {
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "matched": {k: v for k, v in matched.items() if v},
        "word_count": word_count,
    }
    return complexity, confidence, debug


def _vertex_claude_runtime(model: str) -> dict[str, Any]:
    """Resolve a correct GCP Vertex AI Claude runtime for the escalated model.

    Returns the exact keys the gateway/agent expect for the provider profile
    ``google-vertex-claude``. Resolution is best-effort and never raises.
    """
    base_url = "https://aiplatform.googleapis.com"  # global multi-region default
    try:
        from hermes_cli.vertex_ai_locations import vertex_aiplatform_base_url
        from hermes_cli.vertex_claude_access import candidate_locations_for_vertex_claude

        locations = candidate_locations_for_vertex_claude(model)
        if locations:
            base_url = vertex_aiplatform_base_url(locations[0])
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[hussh-one-router] vertex base_url resolution fell back: %s", exc)

    return {
        "provider": "google-vertex-claude",
        "api_mode": "anthropic_messages",
        "api_key": "gcp-sdk",
        "base_url": base_url,
        # Drop any stale Gemini credential pool so the agent rebuilds cleanly.
        "credential_pool": None,
    }


async def route_workload(
    message: str,
    user_config: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """Analyze prompt complexity and route to the optimal model.

    Synchronous logic wrapped in an async signature (no awaiting I/O) for
    backwards-compatibility with the gateway call site.

    Returns: (routed_model_id, routed_runtime_kwargs)
    """
    try:
        clean = (message or "").strip()
        if not clean:
            return MODEL_LOW, {}

        complexity, confidence, debug = classify_complexity(clean)
        if complexity == "high":
            runtime = _vertex_claude_runtime(MODEL_HIGH)
            logger.info(
                "[hussh-one-router] HIGH (conf=%.2f) -> %s | signals=%s",
                confidence, MODEL_HIGH, debug.get("matched"),
            )
            return MODEL_HIGH, runtime

        logger.debug(
            "[hussh-one-router] LOW (conf=%.2f) -> %s", confidence, MODEL_LOW
        )
        return MODEL_LOW, {}
    except Exception as exc:  # pragma: no cover - hard fail-safe
        logger.warning(
            "[hussh-one-router] routing failed (%s); safe-defaulting to Gemini", exc
        )
        return MODEL_LOW, {}


def get_feature_catalog() -> dict[str, str]:
    """Return the documented catalog of custom 🤫 Hussh One features."""
    return {
        "🤫 Hussh One": "Dynamic stacked branding header showing model and mode tokens.",
        "Intelligent Workload Router": "Zero-latency confidence-scored routing between Gemini and Vertex Claude.",
        "Owner-Only Capsule Bypass": "Bypasses sandboxed capsule groups for the owner via owner tag.",
        "Sandboxed Group Capsules": "Restricted, read-only social containers for group pings.",
        "Zero-Friction Autopilot": "Gateway auto-approvals, muted tool logs, and silent heartbeats.",
    }
