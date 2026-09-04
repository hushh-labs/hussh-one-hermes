"""A rejected WS credential must reach the browser as a readable 4401.

Background: the dashboard session token is regenerated on every gateway
start unless ``HERMES_DASHBOARD_SESSION_TOKEN`` is injected. Every browser
tab opened before a restart therefore holds a dead credential. The server
always meant to answer that with close code 4401, but it closed the socket
*before* ``accept()`` (which fails the HTTP upgrade rather than sending a
close frame), so browsers reported the ambiguous ``1006`` and the tab
reconnected forever instead of saying "reload me".

These tests pin the contract both ways:
  * a trusted local caller with a bad credential gets 4401 + a reason;
  * an off-origin caller still gets the opaque pre-accept refusal, so the
    readable code can never become a credential oracle.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server


@pytest.fixture
def loopback_client(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    client = TestClient(web_server.app)
    try:
        yield client
    finally:
        client.close()


def _reject_code(client: TestClient, url: str, **kwargs) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url, **kwargs) as conn:
            # An accepted-then-closed rejection surfaces on the first read.
            conn.receive_text()
    return exc.value


@pytest.mark.parametrize(
    "path",
    ["/api/pty", "/api/events", "/api/ws", "/api/pub", "/api/console"],
)
def test_stale_token_closes_with_readable_4401(loopback_client, path):
    err = _reject_code(loopback_client, f"{path}?token=stale-from-a-dead-gateway&channel=c1")

    assert err.code == 4401
    # The reason is what the browser console echoes; it must name the cause.
    assert "auth" in (err.reason or "")


def test_missing_credential_closes_with_readable_4401(loopback_client):
    err = _reject_code(loopback_client, "/api/pty?channel=c1")

    assert err.code == 4401


def test_off_origin_bad_token_gets_the_opaque_refusal(monkeypatch):
    """No credential oracle: the boundary gate runs first and stays pre-accept.

    A page on the internet must not be able to tell "wrong token" from
    "wrong origin". Both have to look like a failed handshake to it.
    """
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

    client = TestClient(web_server.app)
    try:
        headers = {"Host": "evil.example", "Origin": "http://evil.example"}
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/pty?token=wrong&channel=c1", headers=headers):
                pass
        assert exc.value.code == 4403
    finally:
        client.close()


def test_valid_token_still_connects(loopback_client, monkeypatch):
    """The readable rejection must not have broken the accept path."""
    monkeypatch.setattr(
        web_server,
        "_resolve_chat_argv",
        lambda **_kwargs: (["sh", "-c", "printf hi; sleep 5"], None, None),
    )
    url = f"/api/pty?token={web_server._SESSION_TOKEN}&channel=ok-chan&fresh=1"
    with loopback_client.websocket_connect(url) as conn:
        assert conn.receive_bytes() is not None
