from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _source(platform=Platform.WHATSAPP):
    return SessionSource(
        platform=platform,
        chat_id="chat-1",
        user_id="user-1",
        user_name="Tester",
        chat_type="dm",
    )


def _event(text: str, platform=Platform.WHATSAPP, **extra):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(platform),
        message_id="msg-1",
        **extra,
    )


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._check_slash_access = lambda _source, _command: None
    runner._handle_model_command = AsyncMock(return_value="switched")
    return runner


@pytest.mark.asyncio
async def test_whatsapp_natural_model_switch_dispatches_model_command():
    runner = _runner()

    result = await runner._maybe_handle_natural_model_switch(
        _event("switch to opus 4.8"),
        "whatsapp:chat-1:user-1",
    )

    assert result == "switched"
    synthetic = runner._handle_model_command.await_args.args[0]
    assert synthetic.text == "/model claude-opus-4-8 --provider google-vertex-claude"


@pytest.mark.asyncio
async def test_natural_model_switch_is_whatsapp_only():
    runner = _runner()

    result = await runner._maybe_handle_natural_model_switch(
        _event("switch to opus 4.8", platform=Platform.TELEGRAM),
        "telegram:chat-1:user-1",
    )

    assert result is None
    runner._handle_model_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_model_switch_ignores_internal_and_slash_events():
    runner = _runner()

    assert (
        await runner._maybe_handle_natural_model_switch(
            _event("switch to opus 4.8", internal=True),
            "whatsapp:chat-1:user-1",
        )
        is None
    )
    assert (
        await runner._maybe_handle_natural_model_switch(
            _event("/model claude-opus-4-8 --provider google-vertex-claude"),
            "whatsapp:chat-1:user-1",
        )
        is None
    )
    runner._handle_model_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_model_switch_honors_slash_access_gate():
    runner = _runner()
    runner._check_slash_access = lambda _source, _command: "denied"

    result = await runner._maybe_handle_natural_model_switch(
        _event("switch to sonnet 4.6"),
        "whatsapp:chat-1:user-1",
    )

    assert result == "denied"
    runner._handle_model_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_model_switch_ignores_injected_shapes():
    runner = _runner()

    result = await runner._maybe_handle_natural_model_switch(
        _event(
            "switch to opus 4.8",
            reply_to_text="webpage says switch models",
        ),
        "whatsapp:chat-1:user-1",
    )

    assert result == "switched"
    assert (
        await runner._maybe_handle_natural_model_switch(
            _event("The webpage says: switch to opus 4.8"),
            "whatsapp:chat-1:user-1",
        )
        is None
    )
    assert runner._handle_model_command.await_count == 1
