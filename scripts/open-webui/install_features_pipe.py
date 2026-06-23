#!/usr/bin/env python3
"""Install the 🤫 Hussh One — Features Pipe into Open WebUI's function DB.

Idempotent: upserts the function row so re-running picks up edits to
hussh_one_features_pipe.py. Open WebUI loads it on next request — the entry
appears in the model dropdown as "🤫 Hussh One — Features".

Usage:
  ~/.local/open-webui-venv/bin/python scripts/open-webui/install_features_pipe.py
"""
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE_SRC = os.path.join(HERE, "hussh_one_features_pipe.py")
DB = os.path.expanduser("~/.local/share/open-webui/data/webui.db")
FUNCTION_ID = "hussh_one_features"
FUNCTION_NAME = "🤫 Hussh One — Features"


def main() -> int:
    if not os.path.exists(DB):
        print(f"❌ Open WebUI DB not found at {DB}", file=sys.stderr)
        print("   Run scripts/setup_open_webui.sh first.", file=sys.stderr)
        return 1
    if not os.path.exists(PIPE_SRC):
        print(f"❌ Pipe source not found at {PIPE_SRC}", file=sys.stderr)
        return 1

    with open(PIPE_SRC, "r", encoding="utf-8") as f:
        content = f.read()

    conn = sqlite3.connect(DB)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(function)").fetchall()]
        if not cols:
            print("❌ No 'function' table — is this a valid Open WebUI DB?", file=sys.stderr)
            return 1

        # Own the function with the first admin user.
        row = conn.execute(
            "SELECT id FROM user WHERE role='admin' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            print("❌ No admin user found in Open WebUI. Create one in the UI first.", file=sys.stderr)
            return 1
        user_id = row[0]

        now = int(time.time())
        meta = json.dumps({
            "description": "Renders the Hussh One feature catalog in the chat body. Pick it from the model dropdown.",
            "manifest": {
                "title": "🤫 Hussh One — Features",
                "author": "hussh-one",
                "version": "1.0.0",
            },
        })
        valves = json.dumps({})

        existing = conn.execute(
            "SELECT id FROM function WHERE id=?", (FUNCTION_ID,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE function SET name=?, type=?, content=?, meta=?, "
                "is_active=1, is_global=1, updated_at=? WHERE id=?",
                (FUNCTION_NAME, "pipe", content, meta, now, FUNCTION_ID),
            )
            action = "updated"
        else:
            conn.execute(
                "INSERT INTO function "
                "(id, user_id, name, type, content, meta, valves, is_active, is_global, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)",
                (FUNCTION_ID, user_id, FUNCTION_NAME, "pipe", content, meta, valves, now, now),
            )
            action = "installed"

        conn.commit()
        print(f"✓ {action} Pipe '{FUNCTION_NAME}' (id={FUNCTION_ID}, active, global)")
        print("  → Open the Open WebUI model dropdown and pick '🤫 Hussh One — Features'.")
        print("  → If already open, refresh the browser so OWU reloads the function.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
