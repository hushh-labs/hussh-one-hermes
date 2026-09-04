# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Recover exam cases from this owner's real Hermes session dumps.

The dumps live at ``$HERMES_HOME/sessions/request_dump_*.json`` and are the
agent's own traffic: real prompts, real tool catalogs, real calls. This is the
owner's device and the owner's data, which is the entire premise of an on-device
agent, so the corpus is built from it directly and kept local rather than
committed.

Four things in the data will silently corrupt the exam unless handled here, and
all four were measured rather than guessed:

**Both wire formats.** OpenAI style puts calls in ``assistant.tool_calls`` and
tools in ``tools[].function``; Anthropic style uses ``content`` blocks of type
``tool_use`` and ``tools[].input_schema``. Walking only the first drops 3 patch
calls, and those 3 are the only ``.ts``/``.tsx`` edits in the whole corpus, so
the omission removes exactly the extension class no validator covers.

**History replay.** Every dump replays the full conversation, so one call
appears in one to three dumps. 1,503 raw tool calls collapse to far fewer unique
actions, and counting raw occurrences inflates every number.

**The compactor sentinel.** Hermes truncates history to exactly 214 characters
ending ``...[truncated]``. 17 of 47 ``write_file`` calls are cut this way and 16
of those fail ``ast.parse``. Scoring them reports a 36% syntax-failure rate when
the true rate is 3.8%: an artifact of the dumper, blamed on the model. They are
quarantined here, and reused only as fixtures for the check that detects them.

**Every dump is a failed request** (``max_retries_exhausted`` 71,
``non_retryable_client_error`` 51). The corpus is therefore drawn from hard
cases, which is worth stating beside any number that comes out of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

DUMP_GLOB = "request_dump_*.json"

# Hermes's own history compactor. Anything ending in this was cut by the dumper,
# not written by the model.
TRUNCATION_SENTINEL = re.compile(
    r"(\.\.\.\[truncated\]|\[\s*\.\.\.\s*\d[\d,]*\s*characters?[^\]]*\])\s*$"
)

# Measured on this corpus with the real Gemma-4 tokenizer: code, JSON and base64
# run 3.05 characters per token, not the 4.0 that a generic estimate assumes.
# Using 4.0 undercounts real prompts by 31%.
CHARS_PER_TOKEN = 3.05


def sessions_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "sessions"


def cron_jobs_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cron" / "jobs.json"


# A cron session id (or a case id built from one) starts with the JOB id:
# ``cron_<hex>_<date>_<time>`` for a session, ``cron_<hex>#<n>`` for a case.
# Older case ids kept only 7 characters of the job id, hence prefix matching.
CRON_SESSION = re.compile(r"^cron_([0-9a-f]+)(?:[_#]|$)")


def active_cron_job_ids(path: Optional[Path] = None) -> set:
    """Ids of the cron jobs that are enabled right now; empty when unknown.

    A disabled job's sessions are not the owner's live work. The founder
    disabled the PR-train jobs on purpose, and their turns kept teaching the
    loop to fix a workflow nobody runs (every learnable failure in the first
    replay-suite rounds was an ungrounded ``/pr-train`` path from those jobs).
    Only active jobs and interactive sessions belong in the exam, the loop and
    the goal-progress numbers. A missing or unreadable jobs file means no job
    is known to be active, so every cron session is excluded: the safe side.
    """
    target = Path(path) if path is not None else cron_jobs_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    active = set()
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        if job.get("enabled", True) and not job.get("paused"):
            active.add(str(job["id"]))
    return active


def session_is_active(session_id: str, active_jobs: Optional[set]) -> bool:
    """Interactive sessions always count; a cron session only if its job is active."""
    match = CRON_SESSION.match(session_id or "")
    if not match:
        return True
    job = match.group(1)
    return any(
        active.startswith(job) or job.startswith(active)
        for active in (active_jobs or ())
    )


def active_jobs_for(root: Optional[Path]) -> set:
    """The active-job set a corpus root should be read with.

    A frozen corpus carries the set that was active when it was frozen in its
    manifest, so re-reading it months later yields the same cases; the live
    sessions directory is read against the live jobs file.
    """
    if root is not None:
        manifest = Path(root) / "manifest.json"
        if manifest.exists():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(payload.get("active_cron_jobs"), list):
                    return {str(j) for j in payload["active_cron_jobs"]}
            except Exception:  # noqa: BLE001
                logger.warning("unreadable corpus manifest at %s", manifest)
    return active_cron_job_ids()


def iter_dumps(root: Optional[Path] = None) -> Iterator[dict]:
    """Yield each parsed dump. A dump that will not parse is skipped, loudly."""
    base = Path(root) if root is not None else sessions_dir()
    for path in sorted(base.glob(DUMP_GLOB)):
        try:
            yield json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            logger.warning("unparseable session dump: %s", path.name)


def request_body(dump: dict) -> Optional[dict]:
    """The request body, whether it was stored as a dict or a JSON string."""
    body = (dump.get("request") or {}).get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            return None
    return body if isinstance(body, dict) else None


def catalog_names(body: dict) -> list:
    """Every tool offered, across both wire formats."""
    names = []
    for tool in body.get("tools") or []:
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if name:
            names.append(name)
    return names


def catalog_schemas(body: dict) -> dict:
    """``{tool_name: json_schema}`` for the offered catalog, both formats."""
    schemas = {}
    for tool in body.get("tools") or []:
        function = tool.get("function") or {}
        name = tool.get("name") or function.get("name")
        if not name:
            continue
        schemas[name] = (
            tool.get("input_schema")
            or function.get("parameters")
            or tool.get("parameters")
            or {}
        )
    return schemas


def catalog_descriptions(body: dict) -> dict:
    """``{tool_name: description}`` for the offered catalog, both formats.

    This did not exist, and its absence cost a model its ranking. The replay
    exam rebuilt the catalog with every description hardcoded to the empty
    string while its docstring claimed the catalog was offered "exactly as
    originally". The dumps carry real descriptions on every tool (median about
    400 characters), and qwen3.8-27b turns out to depend on them: on a real
    failing case, restoring the descriptions alone flipped it from a
    hallucinated shell scan to the reference tool with clean arguments, while
    the gemma models tolerate the erasure. An exam that quietly strips signal a
    model relies on is not measuring the model.
    """
    descriptions = {}
    for tool in body.get("tools") or []:
        function = tool.get("function") or {}
        name = tool.get("name") or function.get("name")
        if not name:
            continue
        descriptions[name] = (
            tool.get("description") or function.get("description") or ""
        )
    return descriptions


def iter_tool_calls(message: dict) -> Iterator[tuple[str, dict, str]]:
    """``(name, arguments, call_id)`` from one assistant message, both formats.

    Anthropic-style ``tool_use`` blocks are not an edge case to tidy up later:
    they carry the only ``.ts``/``.tsx`` edits in the corpus.
    """
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        raw = function.get("arguments")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                raw = {"__unparseable__": raw}
        yield name, (raw if isinstance(raw, dict) else {}), call.get("id", "")

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name:
                yield name, (block.get("input") or {}), block.get("id", "")


def iter_tool_results(message: dict) -> Iterator[tuple[str, Any]]:
    """``(call_id, result)`` from a tool message or an Anthropic result block."""
    if message.get("role") == "tool":
        yield message.get("tool_call_id", ""), message.get("content")
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block.get("tool_use_id", ""), block.get("content")


def is_truncated(value: Any) -> bool:
    """True when the dumper cut this, not the model.

    Each string is tested against the end of *its own* value. Serialising the
    whole payload first and searching that does not work: the sentinel then sits
    in the middle of the JSON with ``"}`` after it, the end-anchor never matches,
    and every truncated case sails through into the scoring set. Measured, that
    mistake turns a true 3.8% syntax-failure rate into a reported 36%.
    """
    if isinstance(value, str):
        return bool(TRUNCATION_SENTINEL.search(value))
    if isinstance(value, dict):
        return any(is_truncated(v) for v in value.values())
    if isinstance(value, list):
        return any(is_truncated(v) for v in value)
    return False


def estimate_tokens(text: str) -> int:
    """Token count for this corpus. Measured ratio, not the generic 4.0."""
    return int(len(text) / CHARS_PER_TOKEN)


def wire_size(body: dict) -> int:
    """Characters actually sent, which is not the same as message content.

    Summing only ``message.content`` omits the assistant's ``tool_calls`` JSON
    and the entire ``tools[]`` schema block, both of which are on the wire and
    both of which consume the model's context. That omission undercounts the
    real prompt by about 4x at the median: 23,866 characters against 95,388.
    """
    total = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += len(json.dumps(content))
        if message.get("tool_calls"):
            total += len(json.dumps(message["tool_calls"]))
    if body.get("tools"):
        total += len(json.dumps(body["tools"]))
    if isinstance(body.get("system"), str):
        total += len(body["system"])
    return total


def is_partial_read(args: dict) -> bool:
    """True when a ``read_file`` call asked for a window rather than the file.

    This matters twice over, and missing it corrupts the exam both times.

    As a **pre-image**, a paginated read is not the file. Splicing a patch into
    one produces a fragment that starts mid-indentation, and the parser then
    reports ``line 1: unexpected indent`` for 30 real edits that are perfectly
    valid. That is a 42% failure rate manufactured entirely by the harness.

    As a **verdict**, it is the stale-read overwrite risk: Hermes already emits
    ``_warning: was last read with offset/limit pagination`` on 11 of 72 real
    results and allows the write anyway.
    """
    return any(args.get(k) is not None for k in ("offset", "limit"))


def fingerprint(*parts: Any) -> str:
    """Stable id for deduping replayed history."""
    blob = "\x1f".join(
        p if isinstance(p, str) else json.dumps(p, sort_keys=True) for p in parts
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
