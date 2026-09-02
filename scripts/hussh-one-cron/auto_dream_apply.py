#!/usr/bin/env python3
"""Apply the Auto-Dream model's structured output to the memory layers.

The model thinks; this script writes. Three Auto-Dream runs on 2026-09-02
proved that a 4B-active on-device model cannot be trusted to drive a
multi-step file-editing workflow: one run wrote nothing and claimed
consolidation, one replaced 250K of memory with write_file, one read two
lines of each file and claimed "Consolidation Complete". So the agent job now
emits ONE JSON object (facts, procedures, index entries, archive ids, dream,
vision, brief) and this no-agent job, scheduled a few minutes later, applies it
deterministically: snapshot first, append-only, idempotent per run, and the
brief it delivers states exactly what was applied.

Runs as a Hermes cron `--no-agent` script: stdout is delivered verbatim.
Silent (no stdout) when there is nothing new to apply.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
HERMES_DIR = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes")
MEMORY_DIR = HERMES_DIR / "memory"
OUTPUT_DIR = HERMES_DIR / "cron" / "output" / "2e5aee0849fb"
APPLIED_LEDGER = MEMORY_DIR / ".auto-dream-applied.json"
MAX_AGE_S = 3 * 3600
HEADER = "*🤫 Hussh One* · *Auto-Dream Daemon*\n======================================\n"

sys.path.insert(0, str(HERMES_DIR / "scripts"))
try:
    from auto_dream import snapshot_memory_layers  # the pre-write insurance
except Exception:  # noqa: BLE001
    snapshot_memory_layers = None


def latest_output() -> Path | None:
    if not OUTPUT_DIR.exists():
        return None
    files = sorted(OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        if time.time() - path.stat().st_mtime <= MAX_AGE_S:
            return path
        break
    return None


def extract_json(text: str) -> dict | None:
    """The LAST fenced JSON object in the text (the model's final answer)."""
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = fences[::-1]
    if not candidates:
        # No fence: try the last top-level object that contains "brief".
        start = text.rfind('{"')
        if start != -1:
            candidates = [text[start:]]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict) and "brief" in payload:
            return payload
    return None


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _append(path: Path, block: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + block, encoding="utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def apply(payload: dict, stamp: str) -> tuple[dict, list[str]]:
    counts = {"facts": 0, "procedures": 0, "index": 0, "archived": 0, "dream": 0}
    problems: list[str] = []
    date = time.strftime("%Y-%m-%d")

    facts = _strings(payload.get("long_term"))
    if facts:
        _append(HERMES_DIR / "MEMORY.md",
                f"## {date} — Consolidated by Auto-Dream\n" + "".join(f"- {f}\n" for f in facts))
        counts["facts"] = len(facts)

    procedures = _strings(payload.get("procedures"))
    if procedures:
        _append(MEMORY_DIR / "procedures.md",
                f"## {date} — Workflows noted by Auto-Dream\n" + "".join(f"- {p}\n" for p in procedures))
        counts["procedures"] = len(procedures)

    index_path = MEMORY_DIR / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        entries = index.get("entries")
        if not isinstance(entries, list):
            entries = []
            index["entries"] = entries
        known = {e.get("id") for e in entries if isinstance(e, dict)}
        numbers = [int(str(i).split("_")[-1]) for i in known if str(i).startswith("mem_") and str(i).split("_")[-1].isdigit()]
        next_number = (max(numbers) + 1) if numbers else 1
        now = int(time.time())
        for item in payload.get("index_entries") or []:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            entry_id = f"mem_{next_number:03d}"
            next_number += 1
            relations = [r for r in _strings(item.get("relations")) if r in known]
            entries.append({
                "id": entry_id,
                "category": str(item.get("category") or "long-term"),
                "text": str(item["text"]).strip(),
                "importance": 0.625,  # base 5 * recency 1.0 * log2(2) / 8
                "last_referenced": now,
                "relations": relations,
                "created_at": now,
                "source": "auto-dream",
            })
            known.add(entry_id)
            counts["index"] += 1
        archive_ids = [a for a in _strings(payload.get("archive")) if a in known]
        if archive_ids:
            archived_lines = []
            kept = []
            for entry in entries:
                if isinstance(entry, dict) and entry.get("id") in archive_ids:
                    archived_lines.append(f"- {entry.get('id')} ({date}): {str(entry.get('text', ''))[:160]}")
                else:
                    kept.append(entry)
            index["entries"] = kept
            _append(MEMORY_DIR / "archive.md", "\n".join(archived_lines) + "\n")
            counts["archived"] = len(archived_lines)
        if counts["index"] or counts["archived"]:
            index["last_updated"] = now
            _atomic_json(index_path, index)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"index.json not updated ({exc})")

    dream = str(payload.get("dream") or "").strip()
    vision = str(payload.get("vision") or "").strip()
    if dream:
        _append(MEMORY_DIR / "dreams" / "journal.md",
                f"---\n\n## {date} — Auto-Dream\n\n**Dream:** {dream}\n\n⭐ **Vision:** {vision or '(none recorded)'}\n")
        counts["dream"] = 1
    else:
        problems.append("no dream narrative in the output")
    return counts, problems


def main() -> int:
    source = latest_output()
    if source is None:
        print(HEADER + "\n• Tonight's consolidation could not be applied: no Auto-Dream output from the last 3 hours.\n")
        return 0
    applied = {}
    if APPLIED_LEDGER.exists():
        try:
            applied = json.loads(APPLIED_LEDGER.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            applied = {}
    if source.name in applied:
        return 0  # already applied: silent
    payload = extract_json(source.read_text(encoding="utf-8", errors="ignore"))
    if payload is None:
        print(HEADER + f"\n• Tonight's consolidation could not be applied: the model's output ({source.name}) carried no JSON object with a brief. Nothing was written.\n")
        applied[source.name] = {"at": int(time.time()), "status": "no-json"}
        APPLIED_LEDGER.write_text(json.dumps(applied, indent=2), encoding="utf-8")
        return 0
    if snapshot_memory_layers is not None:
        snapshot_memory_layers()
    counts, problems = apply(payload, source.name)
    applied[source.name] = {"at": int(time.time()), "status": "applied", "counts": counts, "problems": problems}
    APPLIED_LEDGER.write_text(json.dumps(applied, indent=2), encoding="utf-8")

    brief = str(payload.get("brief") or "").strip()
    if not brief.startswith("*🤫 Hussh One*"):
        brief = HEADER + "\n" + brief
    memory_line = (f"• Memory: +{counts['facts']} facts, +{counts['procedures']} procedures, "
                   f"+{counts['index']} index entries, {counts['archived']} archived, "
                   f"{'dream recorded' if counts['dream'] else 'no dream recorded'}")
    out = brief.rstrip() + "\n\n" + memory_line + "\n"
    for problem in problems:
        out += f"\n• Not applied: {problem}\n"
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
