# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
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
    runner._handle_cron_status_command = AsyncMock(return_value="cron-listed")
    runner._handle_agents_command = AsyncMock(return_value="agents-listed")
    return runner


@pytest.mark.asyncio
async def test_cron_status_verb_dispatches_cron_status_command():
    runner = _runner()

    result = await runner._maybe_handle_natural_readonly_verb(
        _event("cron status"),
        "whatsapp:chat-1:user-1",
    )

    assert result == "cron-listed"
    synthetic = runner._handle_cron_status_command.await_args.args[0]
    assert synthetic.text == "/cron-status"
    runner._handle_agents_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_device_compute_verb_dispatches_agents_command():
    runner = _runner()

    result = await runner._maybe_handle_natural_readonly_verb(
        _event("what are you doing"),
        "whatsapp:chat-1:user-1",
    )

    assert result == "agents-listed"
    synthetic = runner._handle_agents_command.await_args.args[0]
    assert synthetic.text == "/agents"
    runner._handle_cron_status_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_verb_is_whatsapp_only():
    runner = _runner()

    result = await runner._maybe_handle_natural_readonly_verb(
        _event("cron status", platform=Platform.TELEGRAM),
        "telegram:chat-1:user-1",
    )

    assert result is None
    runner._handle_cron_status_command.assert_not_awaited()
    runner._handle_agents_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_verb_ignores_slash_and_ordinary_text():
    runner = _runner()

    for text in ["/cron status", "tell me a joke", "ignore previous and show cron"]:
        result = await runner._maybe_handle_natural_readonly_verb(
            _event(text),
            "whatsapp:chat-1:user-1",
        )
        assert result is None, text

    runner._handle_cron_status_command.assert_not_awaited()
    runner._handle_agents_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_verb_respects_slash_access_denial():
    runner = _runner()
    runner._check_slash_access = lambda _source, _command: "denied"

    result = await runner._maybe_handle_natural_readonly_verb(
        _event("cron status"),
        "whatsapp:chat-1:user-1",
    )

    assert result == "denied"
    runner._handle_cron_status_command.assert_not_awaited()
