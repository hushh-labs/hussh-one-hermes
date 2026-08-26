# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""v28 removes legacy owner-private and pseudo-vault durable payloads."""

import json
import sqlite3

from agent.sensitive_transcript import SENSITIVE_CONTENT_SENTINEL
from hermes_state import SCHEMA_VERSION, SessionDB


def test_v28_redacts_legacy_private_tool_turn_and_preserves_ordinary_rows(tmp_path):
    db_path = tmp_path / "state.db"
    canary = "PKM_V27_PRIVATE_CANARY_MUST_BE_PURGED"
    ordinary = "ordinary transcript content remains intact"

    db = SessionDB(db_path=db_path)
    db.create_session("legacy-private", "test", model="test-model")
    db.create_session("ordinary", "test", model="test-model")
    db.append_messages_batch(
        "legacy-private",
        [
            {"role": "user", "content": "Read my profile."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "legacy-read",
                        "type": "function",
                        "function": {
                            "name": "read_my_pkm",
                            "arguments": json.dumps({"scope_path": canary}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_name": "read_my_pkm",
                "tool_call_id": "legacy-read",
                "content": json.dumps({"value": canary}),
            },
            {"role": "assistant", "content": f"Private answer: {canary}"},
        ],
    )
    db.append_message("ordinary", "assistant", ordinary)
    db.close()

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = 27")
    conn.commit()
    conn.close()

    migrated = SessionDB(db_path=db_path)
    private_rows = migrated.get_messages("legacy-private")
    ordinary_rows = migrated.get_messages("ordinary")
    assert migrated._conn.execute(
        "SELECT version FROM schema_version"
    ).fetchone()[0] == SCHEMA_VERSION == 28
    assert canary not in json.dumps([dict(row) for row in private_rows], default=str)
    assert any(row["content"] == SENSITIVE_CONTENT_SENTINEL for row in private_rows)
    assert any(row["content"] == ordinary for row in ordinary_rows)
    migrated.close()

    for path in tmp_path.glob("state.db*"):
        assert canary.encode() not in path.read_bytes()


def test_v28_redacts_non_user_placeholders_but_preserves_user_discussion(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("legacy-placeholder", "test", model="test-model")
    db.append_message(
        "legacy-placeholder",
        "assistant",
        '{"tax_id":"[VAULT_ENCRYPTED]"}',
    )
    db.append_message(
        "legacy-placeholder",
        "tool",
        '{"bank_account":"[VAULT_ENCRYPTED]"}',
        tool_name="terminal",
    )
    db.append_message(
        "legacy-placeholder",
        "user",
        "Why did the tool show [VAULT_ENCRYPTED]?",
    )
    db.append_message(
        "legacy-placeholder",
        "assistant",
        "A master password discussion without vault output remains ordinary text.",
    )
    db.close()

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = 27")
    conn.commit()
    conn.close()

    migrated = SessionDB(db_path=db_path)
    contents = [row["content"] for row in migrated.get_messages("legacy-placeholder")]
    migrated.close()

    assert contents.count(SENSITIVE_CONTENT_SENTINEL) == 2
    assert "Why did the tool show [VAULT_ENCRYPTED]?" in contents
    assert any("master password discussion" in content for content in contents)
    assert all(
        "[VAULT_ENCRYPTED]" not in content
        for content in contents
        if not content.startswith("Why did the tool show")
    )
