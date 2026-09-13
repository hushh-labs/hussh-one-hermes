# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real agent/SDK request boundary against a local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def local_front(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "agent:\n  local_keep_awake: false\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n"
    )
    requests = []
    release = threading.Event()
    stalled = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "fixture-local", "state": "loaded",
                                        "loaded_context_length": 131072}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.path.endswith("/chat/completions"):
                # Model-metadata probes are not inference requests.
                body = json.dumps({"model_info": {"fixture.context_length": 131072}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            if stalled.is_set():
                release.wait(20)
                return
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                     "model": "fixture-local", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": "Verified local response."},
                     "finish_reason": None}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, stalled, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


def _agent(url, db, *, budget=30):
    from run_agent import AIAgent
    return AIAgent(
        provider="lmstudio", base_url=url, api_key="fixture", model="fixture-local",
        enabled_toolsets=["no_mcp"], quiet_mode=True, max_iterations=2,
        max_tokens=256, skip_context_files=True, skip_memory=True,
        skip_background_review=True, session_id="http-rehearsal", session_db=db,
        stream_delta_callback=lambda *_: None, run_budget_seconds=budget,
    )


def test_local_http_response_is_persisted_in_same_session(local_front, tmp_path):
    from hermes_state import SessionDB
    url, requests, _, _ = local_front
    db = SessionDB(tmp_path / "state.db")
    try:
        agent = _agent(url, db)
        result = agent.run_conversation("Give one short response.")
        assert result["final_response"] == "Verified local response."
        assert len(requests) == 1
        assert not requests[0].get("tools")
        assert requests[0]["model"] == "fixture-local"
        assert requests[0]["max_tokens"] == 256
        messages = db.get_messages_as_conversation("http-rehearsal")
        assert any(m.get("content") == "Verified local response." for m in messages)
    finally:
        db.close()


def test_wire_budget_uses_loaded_window_over_catalog(local_front, tmp_path):
    from hermes_state import SessionDB
    url, requests, _, _ = local_front
    db = SessionDB(tmp_path / "state.db")
    try:
        agent = _agent(url, db)
        agent.context_compressor.context_length = 256000
        agent.max_tokens = 200000
        result = agent.run_conversation("Give one short response.")
        assert result["final_response"] == "Verified local response."
        assert len(requests) == 1
        assert 100000 < requests[0]["max_tokens"] < 131072
    finally:
        db.close()


def test_loaded_inventory_does_not_hide_stalled_request(local_front, tmp_path):
    from hermes_state import SessionDB
    url, requests, stalled, _ = local_front
    stalled.set()
    db = SessionDB(tmp_path / "state.db")
    try:
        agent = _agent(url, db, budget=3)
        result = agent.run_conversation("Give one short response.")
        assert len(requests) == 1
        assert "deadline" in result["final_response"].lower()
        assert result["final_response"] != "Verified local response."
        assert db.get_messages_as_conversation("http-rehearsal")
    finally:
        db.close()


def test_repeated_stall_recovery_preserves_same_session(local_front, tmp_path):
    from hermes_state import SessionDB
    url, requests, stalled, release = local_front
    db = SessionDB(tmp_path / "state.db")
    try:
        for cycle in range(2):
            release.clear()
            stalled.set()
            before = len(requests)
            history = db.get_messages_as_conversation("http-rehearsal")
            result = _agent(url, db, budget=3).run_conversation(
                f"Read-only recovery probe {cycle}", conversation_history=history)
            assert "deadline" in result["final_response"].lower()
            assert len(requests) == before + 1
            # Release the fake backend before the next attempt, not merely its inventory.
            release.set()
            stalled.clear()
            history = db.get_messages_as_conversation("http-rehearsal")
            result = _agent(url, db).run_conversation(
                "Continue the read-only probe", conversation_history=history)
            assert result["final_response"] == "Verified local response."
            assert len(requests) == before + 2
        messages = db.get_messages_as_conversation("http-rehearsal")
        assert sum(m.get("content") == "Verified local response." for m in messages) == 2
    finally:
        db.close()
