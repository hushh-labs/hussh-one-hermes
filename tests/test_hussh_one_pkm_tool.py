# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from tools import hussh_one_pkm_tool
from tools.registry import registry


def test_native_owner_read_tool_is_distinct_from_hosted_consent_mcp(monkeypatch) -> None:
    class Client:
        def __init__(self, _bridge) -> None:
            pass

        @staticmethod
        def list_domains() -> dict:
            return {"success": True, "domains": [{"domain": "profile"}]}

    monkeypatch.setattr(hussh_one_pkm_tool, "PkmClient", Client)
    monkeypatch.setattr(hussh_one_pkm_tool, "get_profile_bridge", lambda: object())

    result = json.loads(
        hussh_one_pkm_tool.read_my_pkm({"action": "list_domains"})
    )

    assert result == {"success": True, "domains": [{"domain": "profile"}]}
    entry = registry.get_entry("read_my_pkm")
    assert entry is not None
    assert entry.toolset == "hussh_one"
    assert "no consent request is needed" in entry.schema["description"]
    assert "external agent" in entry.schema["description"]
