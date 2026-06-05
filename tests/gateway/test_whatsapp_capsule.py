"""Tests for hussh-one WhatsApp capsule sandboxing (HUSSH_ONE.md §6).

Covers the Capsule contract:
  A. capsule resolution from config (JID match / non-match)
  B. isolated memory dir routing (reads + writes land in the capsule dir,
     never the owner's global memories/)
  C. read-only toolset (mutating/sensitive toolsets stripped; messaging too)
  D. outbound send lock refuses lateral sends to a non-capsule chat
"""

import os

from gateway.whatsapp_capsule import (
    resolve_capsule,
    DEFAULT_CAPSULE_ENABLED_TOOLSETS,
    DEFAULT_CAPSULE_DISABLED_TOOLSETS,
)


CAPSULE_JID = "120363405517552679@g.us"


def _config_with_capsule(extra=None):
    cfg = {
        "whatsapp": {
            "capsules": {
                CAPSULE_JID: {
                    "name": "three-musketeers",
                    "memory_dir": "capsules/three-musketeers",
                    **(extra or {}),
                }
            }
        }
    }
    return cfg


# --- A. resolution -----------------------------------------------------------

def test_resolve_capsule_matches_jid():
    cap = resolve_capsule(_config_with_capsule(), CAPSULE_JID)
    assert cap is not None
    assert cap.jid == CAPSULE_JID
    assert cap.name == "three-musketeers"
    assert cap.memory_dir == "capsules/three-musketeers"


def test_resolve_capsule_non_match_returns_none():
    assert resolve_capsule(_config_with_capsule(), "999999999@g.us") is None
    assert resolve_capsule(_config_with_capsule(), None) is None
    assert resolve_capsule({"whatsapp": {}}, CAPSULE_JID) is None


def test_resolve_capsule_defaults_are_readonly_and_isolated():
    cap = resolve_capsule(_config_with_capsule(), CAPSULE_JID)
    # Read-only toolset defaults
    assert cap.enabled_toolsets == DEFAULT_CAPSULE_ENABLED_TOOLSETS
    assert cap.disabled_toolsets == DEFAULT_CAPSULE_DISABLED_TOOLSETS
    # Isolation + no-lateral-send default ON
    assert cap.skip_global_memory is True
    assert cap.skip_global_user_profile is True
    assert cap.block_outbound_send is True
    # Mutating/sensitive toolsets must be stripped
    for ts in ("terminal", "file", "delegation", "session_search", "messaging"):
        assert ts in cap.disabled_toolsets


def test_resolve_capsule_honors_overrides():
    cap = resolve_capsule(
        _config_with_capsule({
            "enabled_toolsets": ["web"],
            "block_outbound_send": False,
            "skip_global_memory": False,
        }),
        CAPSULE_JID,
    )
    assert cap.enabled_toolsets == ["web"]
    assert cap.block_outbound_send is False
    assert cap.skip_global_memory is False


# --- B. isolated memory dir --------------------------------------------------

def test_memory_dir_override_isolates_reads_and_writes(tmp_path, monkeypatch):
    import tools.memory_tool as mt

    # Point HERMES_HOME at a temp dir so "global" memory is a real path.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    global_dir = mt.get_memory_dir()
    assert global_dir == tmp_path / "memories"

    # Activate capsule override
    token = mt.set_memory_dir_override("capsules/three-musketeers")
    try:
        cap_dir = mt.get_memory_dir()
        assert cap_dir == tmp_path / "capsules" / "three-musketeers"
        assert cap_dir != global_dir
    finally:
        mt.reset_memory_dir_override(token)

    # After reset, global dir is restored
    assert mt.get_memory_dir() == global_dir


def test_capsule_memorystore_does_not_read_global(tmp_path, monkeypatch):
    import tools.memory_tool as mt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Seed the OWNER's global memory with a secret.
    global_mem = tmp_path / "memories"
    global_mem.mkdir(parents=True, exist_ok=True)
    (global_mem / "MEMORY.md").write_text("OWNER_SECRET phone +1 555", encoding="utf-8")
    (global_mem / "USER.md").write_text("Kushal private profile", encoding="utf-8")

    token = mt.set_memory_dir_override("capsules/three-musketeers")
    try:
        store = mt.MemoryStore()
        store.load_from_disk()
        snapshot = (
            store._system_prompt_snapshot.get("memory", "")
            + store._system_prompt_snapshot.get("user", "")
        )
        # The owner's secret must NOT appear in a capsule session snapshot.
        assert "OWNER_SECRET" not in snapshot
        assert "private profile" not in snapshot
        # The capsule memory dir was created (isolated growth target).
        assert (tmp_path / "capsules" / "three-musketeers").exists()
    finally:
        mt.reset_memory_dir_override(token)


# --- D. outbound send lock ---------------------------------------------------

def test_outbound_send_lock_blocks_lateral_send(monkeypatch):
    import tools.send_message_tool as sm

    token = sm.set_outbound_send_lock(CAPSULE_JID)
    try:
        # Sending to a DIFFERENT chat must be refused without hitting network.
        res = sm._handle_send({
            "action": "send",
            "target": f"whatsapp:919999999999@s.whatsapp.net",
            "message": "leak attempt",
        })
        assert "Blocked" in str(res) or "blocked" in str(res).lower()
    finally:
        sm.reset_outbound_send_lock(token)


def test_outbound_send_lock_cleared_after_reset(monkeypatch):
    import tools.send_message_tool as sm

    token = sm.set_outbound_send_lock(CAPSULE_JID)
    sm.reset_outbound_send_lock(token)
    # After reset the lock is gone (None) — guard inactive.
    assert sm._OUTBOUND_SEND_LOCK.get() is None
