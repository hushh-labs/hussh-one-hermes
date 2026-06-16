"""🤫 Hussh One — Intelligent Workload and Intent Router.

This module implements the custom workload-gated routing capability for the
🤫 Hussh One variant of Hermes Agent. It analyzes incoming prompts for complexity
and intent to determine the optimal execution model dynamically:

  * Low complexity (chit-chat, simple queries) -> Gemini 3.5 Flash (low cost, fast)
  * High complexity (coding, filesystem edits, deploy work) -> Claude Opus (deep reasoning)

This routing is upgrade-safe (it lives purely in the overlay brand layer)
and serves as the true implementation backing the auto [A] mode token.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Any

logger = logging.getLogger("gateway.hussh_one_router")

# Default model definitions for the routing tiers
MODEL_LOW = "gemini-3.5-flash"
MODEL_HIGH = "claude-opus-4-8"

# Rule-based fallback keywords that indicate high workload complexity
_HIGH_COMPLEXITY_KEYWORDS = {
    "code", "program", "script", "file", "directory", "write", "edit", "patch", "git", "pr", 
    "commit", "deploy", "gcp", "database", "migration", "verify", "assert", "test", "audit", 
    "workflow", "scaffold", "mcp", "terminal", "compile", "docker", "reorganize", "organize"
}


def _classify_via_rules(message: str) -> str:
    """Fallback rule-based complexity classifier when LLM routing is unavailable."""
    msg_lower = (message or "").lower()
    # Strip trigger tokens and names to avoid false-positives
    clean_msg = re.sub(r"@(OneTeam|One|husshOne|hussh-one)", "", msg_lower).strip()
    
    # Simple token matching
    tokens = set(re.findall(r"\b\w+\b", clean_msg))
    intersect = tokens & _HIGH_COMPLEXITY_KEYWORDS
    if intersect:
        logger.debug(
            "[hussh-one-router] Rule match: complexity=high due to keywords %s", 
            intersect
        )
        return "high"
    return "low"


async def route_workload(
    message: str, 
    user_config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Analyze user prompt complexity and route to the optimal model.

    Resolves and returns: (routed_model_id, routed_runtime_kwargs)

    Args:
        message: The raw incoming prompt from the user.
        user_config: The active gateway configuration.
    """
    # Clean the message
    clean_message = (message or "").strip()
    if not clean_message:
        return MODEL_LOW, {}

    # Default baseline configuration (low complexity)
    routed_model = MODEL_LOW
    routed_runtime = {}

    # Check if we should attempt LLM-based classification
    # If the default model in config is already overridden (e.g. pinned), we respect it directly.
    # In cloud-enabled mode, we run a fast classification pass using the low-cost model.
    try:
        from run_agent import AIAgent

        # Formulate a structured classification prompt
        prompt = f"""You are the Intelligent Workload and Intent Router for 🤫 Hussh One.
Analyze the incoming user request complexity.

A request has HIGH complexity if it requires:
1. Writing, editing, patching, compiling, or executing code.
2. Modifying files, creating folders, or running terminal commands.
3. Accessing databases, running migrations, or managing GCP/infrastructure.
4. Strategic, deep reasoning, or structured architectural design.

A request has LOW complexity if it is:
1. Casual conversation, greetings, simple questions, or social chit-chat.
2. Sourcing basic public information, RSS feeds, or short text summaries.
3. Simple queries that do not require any tools.

Return your decision in JSON format EXACTLY as:
{{"complexity": "high" | "low", "reason": "one-sentence explanation of the complexity"}}

User request: "{clean_message}"
"""

        # Spawn a fast, tool-less, memory-isolated AIAgent to run the classification
        classifier = AIAgent(
            model=MODEL_LOW,
            enabled_toolsets=[],  # disable tools for high speed
            skip_memory=True,     # bypass memory DB loads
            skip_context_files=True, # bypass workspace scan
            max_iterations=1,     # single turn completion
            quiet_mode=True,
            ephemeral_system_prompt="You are a precise JSON classifier. Output JSON only."
        )

        logger.debug("[hussh-one-router] Dispatched LLM classification turn...")
        # AIAgent.run_conversation is a SYNCHRONOUS method that returns a dict.
        # Awaiting it raised "object dict can't be used in 'await' expression"
        # on every turn, silently killing LLM routing and forcing the crude
        # keyword fallback. Run it off-thread so we never block the event loop.
        import asyncio as _asyncio

        result = await _asyncio.to_thread(classifier.run_conversation, prompt)
        response_text = str(result.get("final_response") or "").strip()
        logger.debug("[hussh-one-router] Raw classifier response: %s", response_text)

        # Extract JSON from potential markdown blocks
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            complexity = str(data.get("complexity") or "low").strip().lower()
            reason = data.get("reason") or ""
            
            logger.info(
                "[hussh-one-router] LLM routed: complexity=%s reason='%s'", 
                complexity, reason
            )
            if complexity == "high":
                routed_model = MODEL_HIGH
        else:
            raise ValueError("No JSON block resolved in classifier output.")

    except Exception as exc:
        # Fall back to rule-based classification on any network/API/parse error
        logger.warning(
            "[hussh-one-router] LLM classification failed (%s); falling back to rule-engine...", 
            exc
        )
        complexity = _classify_via_rules(clean_message)
        logger.info("[hussh-one-router] Rule routed: complexity=%s", complexity)
        if complexity == "high":
            routed_model = MODEL_HIGH

    # Populate matching runtime variables from global configuration keys
    # Sourced upgrade-safely without hardcoding API keys
    routed_runtime["provider"] = "gemini" if routed_model == MODEL_LOW else "google-vertex"
    
    # Vertex-AI configurations are loaded dynamically in the gateway, we align to them
    if routed_model == MODEL_HIGH:
        # Pinned Vertex AI configurations
        routed_runtime["api_mode"] = "vertex"
        routed_runtime["provider"] = "google-vertex"
        
    return routed_model, routed_runtime


def get_feature_catalog() -> dict[str, str]:
    """Return the documented catalog of custom 🤫 Hussh One features for marketing."""
    return {
        "🤫 Hussh One": "Dynamic stacked branding header showing model and mode tokens.",
        "Intelligent Workload Router": "Dynamic workload-complexity routing between Gemini and Claude.",
        "Owner-Only Capsule Bypass": "Bypasses sandboxed capsule groups for the owner via owner tag.",
        "Sandboxed Group Capsules": "Restricted, read-only social containers for group pings.",
        "Zero-Friction Autopilot": "Gateway auto-approvals, muted tool logs, and silent heartbeats.",
        "Catalyst SQLite Offline Sync": "Queries macOS local database directly to extract JIDs and assets."
    }
