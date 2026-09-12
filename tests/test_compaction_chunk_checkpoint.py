"""Durable intermediate summaries survive a process/session-db reopen."""
import pytest
from hermes_state import SessionDB


def test_chunk_checkpoint_reopens_and_does_not_replace_transcript(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("resume-test", "cli")
    db.save_compression_chunk("resume-test", "source-and-model-hash", "Goal and committed receipts preserved.")
    db.close()
    db = SessionDB(db_path=path)
    try:
        assert db.get_compression_chunk("resume-test", "source-and-model-hash") == "Goal and committed receipts preserved."
        assert db.get_compression_chunk("resume-test", "different-source-hash") is None
        assert db.get_messages("resume-test") == []
        with pytest.raises(ValueError):
            db.save_compression_chunk("resume-test", "empty", " ")
    finally:
        db.close()


def test_interrupted_chunk_pass_reuses_committed_digest(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import agent.context_compressor as module
    import agent.auxiliary_client as auxiliary

    db = SessionDB(db_path=tmp_path / "chunks.db")
    db.create_session("chunks", "cli")
    monkeypatch.setattr(module, "_LEAN_DIGEST_CHUNK_CHARS", 400)
    monkeypatch.setattr(module, "_serialize_turns_for_digest", lambda *args: "a" * 400 + "b" * 400)
    requests = []

    def complete(**kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            raise ConnectionError("backend stopped")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Verified goal, source identifiers and committed tool receipts."))])

    monkeypatch.setattr(auxiliary, "call_llm", complete)

    def compressor():
        obj = module.ContextCompressor(model="meta/muse-glimmer", provider="lmstudio", base_url="http://127.0.0.1:1234/v1", config_context_length=131072)
        obj.bind_session_state(db, "chunks")
        return obj

    try:
        with pytest.raises(RuntimeError, match="prior checkpoints preserved"):
            compressor()._build_chunk_digests([{"role": "user", "content": "goal"}])
        result = compressor()._build_chunk_digests([{"role": "user", "content": "goal"}])
        assert len(requests) == 3  # First chunk reused; only failed second chunk retried.
        assert "Segment 1/2" in result and "Segment 2/2" in result
        assert requests[0]["reasoning_config"]["effort"] == "high"
        assert requests[0]["max_tokens"] > module._LEAN_DIGEST_MAX_TOKENS
    finally:
        db.close()
