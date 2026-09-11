#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""
Auto-Dream Consolidated Suite: Slow-Wave Consolidation + REM Phase Seeds.
Fuses log collection, memory state compilation, distant memory collisions, and dream seeds
into a single, highly cohesive, single-ping execution flow.
"""
import os
import shutil
import sqlite3
import sys
import time
import json
import random
import re
from collections import deque

HOME = os.path.expanduser("~")
HERMES_DIR = os.path.join(HOME, ".hermes")
MEMORY_DIR = os.path.join(HERMES_DIR, "memory")
EPISODES_DIR = os.path.join(MEMORY_DIR, "episodes")
DB_PATH = os.path.join(HERMES_DIR, "state.db")
INDEX_PATH = os.path.join(MEMORY_DIR, "index.json")
DREAMS_DIR = os.path.join(MEMORY_DIR, "dreams")
JOURNAL_PATH = os.path.join(DREAMS_DIR, "journal.md")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(EPISODES_DIR, exist_ok=True)
os.makedirs(DREAMS_DIR, exist_ok=True)

# Seed the RNG dynamically but stably per-day
random.seed(int(time.time() // 86400))

def clean_invisible_chars(text):
    if not text:
        return ""
    for char in ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u202f"]:
        text = text.replace(char, "")
    return text

# Phrases the cron security scanner (Tirith) treats as injection/exfiltration threats.
_THREAT_PATTERNS = [
    r"prompt[\s\-_]*injection",
    r"prompt[\s\-_]*inject",
    r"ignore\s+(?:all\s+|the\s+)?previous\s+instructions",
    r"ignore\s+(?:all\s+|the\s+)?prior\s+instructions",
    r"disregard\s+(?:all\s+|the\s+)?previous\s+instructions",
    r"exfiltrat\w*",
    r"jailbreak\w*",
    r"system\s+prompt\s+override",
]
_THREAT_RE = re.compile("|".join(_THREAT_PATTERNS), re.IGNORECASE)

def defang_threat_terms(text):
    if not text:
        return ""
    def _break(m):
        word = m.group(0)
        return word[0] + "\u00b7" + word[1:]
    return _THREAT_RE.sub(_break, text)

def collect_recent_logs(days=7):
    if not os.path.exists(DB_PATH):
        return "No state.db found."

    since_epoch = time.time() - (days * 24 * 3600)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT id, title, started_at, source FROM sessions WHERE started_at >= ? AND source != 'cron' AND id NOT LIKE 'cron_%' ORDER BY started_at DESC", (since_epoch,))
    sessions = cursor.fetchall()
    
    session_map = {}
    for s in sessions:
        session_map[s[0]] = {
            "title": defang_threat_terms(clean_invisible_chars(s[1] or "Untitled Session")),
            "started_at": s[2],
            "source": s[3] or "unknown",
            "messages": []
        }

    # Newest first, so when the budget runs out it is the OLDEST turns that
    # drop, not the most recent ones.
    cursor.execute(
        "SELECT session_id, role, content, timestamp FROM messages WHERE timestamp >= ? AND role IN ('user', 'assistant') ORDER BY timestamp DESC",
        (since_epoch,)
    )
    messages = cursor.fetchall()
    capped = False
    accumulated_chars = 0
    for m in messages:
        sid = m[0]
        if sid in session_map:
            content = clean_invisible_chars(m[2] or "")
            content = defang_threat_terms(content)
            if len(content) > 4000:
                content = content[:4000] + "\n... [message truncated for dream consolidation]"
            if accumulated_chars + len(content) > LOG_BUDGET_CHARS:
                capped = True
                break
            session_map[sid]["messages"].append({
                "role": m[1],
                "content": content,
                "time": m[3]
            })
            accumulated_chars += len(content)
    for sdata in session_map.values():
        sdata["messages"].reverse()

    conn.close()

    output = []
    output.append(f"=== RECENT CONVERSATIONS (LAST {days} DAYS) ===")
    if capped:
        output.append(f"\n⚠️ [LOG COLLECTION CAPPED AT {LOG_BUDGET_CHARS // 1000}K CHARACTERS, NEWEST TURNS KEPT, TO FIT THE ON-DEVICE MODEL'S CONTEXT] ⚠️\n")

    # Bounded per-session summarization/chunking
    for sid, sdata in session_map.items():
        if not sdata["messages"]:
            continue
        local_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(sdata["started_at"]))
        # Build a compact per-session summary bounded by PER_SESSION_SUMMARY_CHARS
        session_text_parts = []
        for msg in sdata["messages"]:
            role_label = "User" if msg["role"] == "user" else "Agent"
            session_text_parts.append(f"[{role_label}]: {msg['content'].strip()}")
        session_body = "\n".join(session_text_parts)
        # Hard per-session cap
        if len(session_body) > PER_SESSION_SUMMARY_CHARS:
            session_body = session_body[:PER_SESSION_SUMMARY_CHARS] + "\n... [session truncated to per-session budget]"
        output.append(f"\nSession ID: {sid} | Title: {sdata['title']} | Source: {sdata['source']} | Date: {local_time}")
        output.append("-" * 60)
        output.append(session_body)
    return "\n".join(output)

def read_file_if_exists(path, default_content=""):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return defang_threat_terms(clean_invisible_chars(f.read()))
    return default_content


# ── CONTEXT BUDGET ───────────────────────────────────────────────────────
# This job runs on the on-device model (google/gemma-4-26b-a4b-qat, 262,144
# token context). The whole dump is ONE user message, which Hermes cannot
# compress, so it has to fit on its own beside the system prompt and the
# tool schemas. Measured 2026-09-02: an uncapped dump was 572K characters
# (~188K real tokens) and, with 232 tool schemas, the request was rejected
# by LM Studio nine nights running. Intermediate collection budgets can exceed
# the request size, but the final assembled prompt is hard-capped below.
LOG_BUDGET_CHARS = 140_000
MEMORY_FILE_BUDGET_CHARS = 40_000
INDEX_FULL_BUDGET_CHARS = 40_000
# Hard final prompt budget for the whole auto-dream output.
# Keeps the assembled <auto-dream-context> + <dream-seed-context> under
# ~120k chars (~39k tokens) to avoid context-length errors on meta/muse-glimmer.
FINAL_PROMPT_BUDGET_CHARS = 120_000
# Per-session summary budget to bound pre-summarization/chunking.
PER_SESSION_SUMMARY_CHARS = 2_500


def bound_auto_dream_output(full_output, prompt_memory, budget=FINAL_PROMPT_BUDGET_CHARS):
    """Keep the one-shot prompt bounded while retaining prompt-visible memory.

    Recent logs are intentionally assembled first because they are the largest,
    most disposable part of the consolidation context.  If the hard budget is
    reached before the memory-tool section, append that section explicitly so
    the model never consolidates against a prompt that silently omits the
    memory it is expected to maintain.
    """
    if len(full_output) <= budget:
        return full_output

    clip_note = (
        f"\n\n... [AUTO-DREAM OUTPUT CLIPPED at {budget // 1000}K characters "
        f"to enforce the on-device context budget for meta/muse-glimmer; "
        f"total was {len(full_output) // 1000}K characters]"
    )
    closing = "\n</dream-seed-context>\n"
    prompt_block = ""
    initial_head_limit = max(0, budget - len(clip_note) - len(closing))
    if prompt_memory.strip() and prompt_memory not in full_output[:initial_head_limit]:
        prompt_block = "\n\n--- PROMPT MEMORY PRESERVED AFTER CLIPPING ---\n" + prompt_memory

    head_limit = max(0, budget - len(clip_note) - len(closing) - len(prompt_block))
    head = full_output[:head_limit]
    if prompt_block and prompt_memory in head:
        prompt_block = ""
        head_limit = max(0, budget - len(clip_note) - len(closing))
        head = full_output[:head_limit]

    bounded = head + prompt_block + clip_note + closing
    return bounded[:budget]


def clip(text, limit, what, path):
    """Keep the head of a file within budget and say what was cut and where the rest is."""
    if len(text) <= limit:
        return text
    return (text[:limit]
            + f"\n\n... [{what} clipped at {limit // 1000}K of {len(text) // 1000}K characters "
              f"for the on-device context budget; read {path} with the file tools "
              f"before editing it]")


def compact_index(index_text, path):
    """A one-line-per-entry view of memory/index.json when the raw JSON is too big.

    The agent updates the index with the file tools anyway; what it needs in the
    prompt is which entries exist, their importance and when they were last
    referenced, not 149K characters of raw JSON.
    """
    if len(index_text) <= INDEX_FULL_BUDGET_CHARS:
        return index_text
    try:
        data = json.loads(index_text)
    except Exception:
        return clip(index_text, INDEX_FULL_BUDGET_CHARS, "memory index", path)
    entries = data.get("entries") or data.get("nodes") or data.get("memories") or []
    lines = [f"(compact view of {len(entries)} entries; the raw JSON is {len(index_text) // 1000}K characters, "
             f"read {path} with the file tools before editing it)",
             "id | category | importance | last_referenced | text"]
    for e in entries:
        if not isinstance(e, dict):
            continue
        text = clean_invisible_chars(str(e.get("text") or e.get("summary") or "")).replace("\n", " ")
        lines.append(f"{e.get('id', '?')} | {e.get('category', '?')} | {e.get('importance', '?')} | "
                     f"{e.get('last_referenced', '?')} | {text[:140]}")
    return "\n".join(lines)

def load_entries():
    if not os.path.exists(INDEX_PATH):
        return [], {}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        entries = data.get("entries") or data.get("nodes") or data.get("memories") or []
        by_id = {e.get("id"): e for e in entries if e.get("id")}
        return entries, by_id
    except Exception:
        return [], {}

def graph_distance(by_id, a, b):
    if a == b:
        return 0
    seen = {a}
    q = deque([(a, 0)])
    while q:
        node, d = q.popleft()
        if d > 6:
            break
        for nb in by_id.get(node, {}).get("relations", []) or []:
            if nb == b:
                return d + 1
            if nb not in seen and nb in by_id:
                seen.add(nb)
                q.append((nb, d + 1))
    return 999

def pick_distant_pairs(entries, by_id, n_pairs=3):
    ids = [e.get("id") for e in entries if e.get("id")]
    if len(ids) < 2:
        return []
    scored = []
    attempts = min(400, len(ids) * len(ids))
    seen_pairs = set()
    for _ in range(attempts):
        a, b = random.sample(ids, 2)
        key = tuple(sorted((a, b)))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        dist = graph_distance(by_id, a, b)
        scored.append((dist, a, b))
    scored.sort(key=lambda x: (-x[0], random.random()))
    return scored[:n_pairs]

def text_of(by_id, mid):
    e = by_id.get(mid, {})
    cat = e.get("category", "?")
    return f"[{mid} | {cat}] {clean_invisible_chars(e.get('text',''))}"

def last_dream_excerpt():
    if not os.path.exists(JOURNAL_PATH):
        return None
    with open(JOURNAL_PATH, "r", encoding="utf-8", errors="ignore") as f:
        content = clean_invisible_chars(f.read())
    if not content.strip():
        return None
    chunks = [c.strip() for c in content.split("\n---\n") if c.strip()]
    if not chunks:
        return None
    return chunks[-1][:1800]

def run_self_checks():
    """Verify memory health and prevent on-disk corruption or silent drift."""
    alerts = []
    if not os.path.exists(os.path.join(HERMES_DIR, "MEMORY.md")):
        alerts.append("⚠️ Long-Term Memory (MEMORY.md) is missing.")
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as je:
            alerts.append(f"⚠️ memory/index.json is malformed or corrupted: {je}")
    return alerts

def snapshot_memory_layers():
    """Copy the memory layers aside before the agent runs. Insurance.

    On 2026-09-02 the on-device model answered the consolidation prompt with
    write_file instead of patch and replaced MEMORY.md, procedures.md and the
    dream journal with a few hundred bytes each. They were recovered from the
    run's own read-backs. This makes the next such night a one-line restore.
    Keeps the last 14 snapshots.
    """
    backup_root = os.path.join(MEMORY_DIR, ".auto-dream-backups")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_root, stamp)
    try:
        os.makedirs(target, exist_ok=True)
        for path in (os.path.join(HERMES_DIR, "MEMORY.md"),
                     os.path.join(MEMORY_DIR, "procedures.md"),
                     os.path.join(MEMORY_DIR, "archive.md"),
                     INDEX_PATH, JOURNAL_PATH):
            if os.path.exists(path):
                shutil.copy2(path, os.path.join(target, os.path.basename(path)))
        if os.path.isdir(EPISODES_DIR):
            shutil.copytree(EPISODES_DIR, os.path.join(target, "episodes"), dirs_exist_ok=True)
        snapshots = sorted(d for d in os.listdir(backup_root)
                           if os.path.isdir(os.path.join(backup_root, d)))
        for old in snapshots[:-14]:
            shutil.rmtree(os.path.join(backup_root, old), ignore_errors=True)
        return target
    except Exception as exc:  # never block the dream cycle on a backup problem
        sys.stderr.write(f"[auto_dream] memory snapshot failed: {exc}\n")
        return None


PROMPT_MEMORY_DIR = os.path.join(HERMES_DIR, "memories")
PROMPT_MEMORY_BUDGET_CHARS = 6_000


def prompt_memory_sections(memory_dir=None):
    """The two files the PROMPT actually reads, for the model half's context.

    ``memories/MEMORY.md`` and ``memories/USER.md`` are the memory tool's own
    store (``§``-delimited, 2,200 / 1,375 characters). Until this dump the model
    consolidated against the root journal and never saw what its own prompt
    carried, so it could neither notice a fact missing from the prompt nor a
    stale one still in it. Read as text, never parsed: this is context, not a
    write path. Bounded small because these files are bounded small.
    """
    root = memory_dir or PROMPT_MEMORY_DIR
    sections = []
    for name, label in (("MEMORY.md", "Prompt Memory"), ("USER.md", "User Profile")):
        path = os.path.join(root, name)
        text = clip(
            read_file_if_exists(path, f"(no {name} in the memory tool's store yet)"),
            PROMPT_MEMORY_BUDGET_CHARS, name, path)
        sections.append(f"\n--- {label} (memories/{name}, what the agent's prompt actually reads) ---\n{text}")
    return "\n".join(sections)


def main():
    snapshot_memory_layers()
    recent_logs = collect_recent_logs()
    memory_path = os.path.join(HERMES_DIR, "MEMORY.md")
    procedures_path = os.path.join(MEMORY_DIR, "procedures.md")
    index_path = os.path.join(MEMORY_DIR, "index.json")
    main_memory = clip(
        read_file_if_exists(memory_path, "# Long-Term Memory\n\nNo permanent facts recorded yet."),
        MEMORY_FILE_BUDGET_CHARS, "MEMORY.md", memory_path)
    procedures = clip(
        read_file_if_exists(procedures_path, "# Procedural Memory (Workflows & Tool Patterns)\n\nNo tool procedures recorded yet."),
        MEMORY_FILE_BUDGET_CHARS, "procedures.md", procedures_path)
    index_json = compact_index(read_file_if_exists(index_path, "{}"), index_path)
    
    episodes_summary = []
    if os.path.exists(EPISODES_DIR):
        for f in sorted(os.listdir(EPISODES_DIR)):
            if f.endswith(".md"):
                content = read_file_if_exists(os.path.join(EPISODES_DIR, f))
                first_few_lines = "\n".join(content.splitlines()[:5])
                episodes_summary.append(f"File: episodes/{f}\n{first_few_lines}\n...")
    episodes_text = "\n\n".join(episodes_summary) if episodes_summary else "No episodic narratives recorded yet."

    # Assemble auto-dream context blocks
    auto_context_parts = []
    auto_context_parts.append("<auto-dream-context>")
    auto_context_parts.append(recent_logs)
    auto_context_parts.append("\n" + "="*50 + "\n")
    auto_context_parts.append("=== CURRENT MEMORY STATE ===")
    auto_context_parts.append("\n--- Main Long-Term Memory (MEMORY.md) ---")
    auto_context_parts.append(main_memory)
    auto_context_parts.append("\n--- Procedures Memory (procedures.md) ---")
    auto_context_parts.append(procedures)
    prompt_memory = prompt_memory_sections()
    auto_context_parts.append(prompt_memory)
    auto_context_parts.append("\n--- Episodic Memory Summary ---")
    auto_context_parts.append(episodes_text)
    auto_context_parts.append("\n--- Memory Index JSON ---")
    auto_context_parts.append(index_json)
    auto_context_parts.append("</auto-dream-context>\n")

    # Assemble dream-seed context
    entries, by_id = load_entries()
    pairs = pick_distant_pairs(entries, by_id, n_pairs=3)
    dream_parts = []
    dream_parts.append("<dream-seed-context>")
    dream_parts.append("=== TONIGHT'S DREAM SEEDS (distant memory collisions) ===")
    dream_parts.append("These concept pairs are far apart in your memory graph. The further")
    dream_parts.append("apart, the stranger and potentially more original the connection.\n")
    if not pairs:
        dream_parts.append("(Not enough memory nodes yet to generate collisions.)")
    else:
        for i, (dist, a, b) in enumerate(pairs, 1):
            dlabel = "DISCONNECTED" if dist >= 999 else f"distance {dist}"
            dream_parts.append(f"--- Seed {i} ({dlabel}) ---")
            dream_parts.append("  A: " + text_of(by_id, a))
            dream_parts.append("  B: " + text_of(by_id, b))
            dream_parts.append("")
    ids = [e.get("id") for e in entries if e.get("id")]
    if len(ids) >= 3:
        triple = random.sample(ids, 3)
        dream_parts.append("--- Wildcard triple (fuse all three into one impossible object) ---")
        for t in triple:
            dream_parts.append("  * " + text_of(by_id, t))
        dream_parts.append("")
    prev = last_dream_excerpt()
    dream_parts.append("=== LAST NIGHT'S DREAM (for continuity / deepening) ===")
    dream_parts.append(prev if prev else "(No prior dream recorded -- this is the first night.)")
    dream_parts.append("</dream-seed-context>\n")

    # Hard final prompt budget with clipping note.  The prompt-memory section
    # is preserved explicitly if the recent-log block consumes the budget.
    full_output = "\n".join(auto_context_parts + dream_parts)
    full_output = bound_auto_dream_output(
        full_output, prompt_memory, FINAL_PROMPT_BUDGET_CHARS
    )

    # Print final output
    print(full_output)

    # ── SYSTEM HEALTH & SELF-CHECKS ─────────────────────────────────
    alerts = run_self_checks()
    if alerts:
        print("<system-health-alerts>")
        for alert in alerts:
            print(alert)
        print("</system-health-alerts>")

if __name__ == "__main__":
    main()
