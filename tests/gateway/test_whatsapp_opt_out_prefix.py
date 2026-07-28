# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig


def _make_adapter():
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = "open"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._is_dm_intake_allowed = lambda sender_id: True
    adapter._is_group_allowed = lambda chat_id: True
    adapter._is_broadcast_chat = lambda chat_id: False
    adapter._compile_mention_patterns = lambda: []
    adapter._whatsapp_free_response_chats = lambda: set()
    adapter._whatsapp_require_mention = lambda: False
    return adapter


def test_whatsapp_opt_out_prefix_suppresses_processing():
    adapter = _make_adapter()

    # Direct messages starting with @none, @no, @off, @mute must return False
    opt_out_messages = [
        {"body": "@none taking a quick note here", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@NONE hello world", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@no don't process this", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@off turn off reply", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@mute", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
    ]

    for msg in opt_out_messages:
        assert adapter._should_process_message(msg) is False, f"Expected {msg['body']} to be suppressed"


def test_whatsapp_normal_messages_are_processed():
    adapter = _make_adapter()

    normal_messages = [
        {"body": "hello agent", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@One help me", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
        {"body": "@nonebook", "chatId": "12012419368@s.whatsapp.net", "senderId": "12012419368@s.whatsapp.net"},
    ]

    for msg in normal_messages:
        assert adapter._should_process_message(msg) is True, f"Expected {msg['body']} to be processed"
