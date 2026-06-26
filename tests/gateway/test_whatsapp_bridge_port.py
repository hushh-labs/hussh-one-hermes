"""WhatsApp bridge port: dedicated, conflict-free default + config override.

Regression guard for the port move off 3000 (which collides with Next.js /
Vite / CRA dev servers) to the dedicated loopback port 8473.

These are invariant/contract tests, not change-detector snapshots:
- the adapter default must NOT be the collision-prone 3000;
- a user-set ``whatsapp.bridge_port`` must actually reach ``config.extra``
  and be honored by the adapter.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.whatsapp import WhatsAppAdapter


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_adapter_default_bridge_port_is_not_3000():
    """Default must be the dedicated port, never the dev-server collision port."""
    adapter = WhatsAppAdapter(PlatformConfig(enabled=False, extra={}))
    assert adapter._bridge_port == 8473
    assert adapter._bridge_port != 3000


def test_adapter_honors_explicit_bridge_port():
    """An explicit bridge_port in config.extra overrides the default."""
    adapter = WhatsAppAdapter(
        PlatformConfig(enabled=False, extra={"bridge_port": 8799})
    )
    assert adapter._bridge_port == 8799


def test_bridge_js_default_port_matches_adapter_default():
    """bridge.js CLI default must agree with the Python adapter default so a
    manually-launched bridge lands on the same port the gateway probes."""
    bridge = REPO_ROOT / "scripts" / "whatsapp-bridge" / "bridge.js"
    text = bridge.read_text(encoding="utf-8")
    m = re.search(r"getArg\(\s*'port'\s*,\s*'(\d+)'\s*\)", text)
    assert m, "could not find the --port default in bridge.js"
    assert m.group(1) == "8473"


def test_top_level_whatsapp_bridge_port_bridges_into_extra(tmp_path, monkeypatch):
    """A top-level ``whatsapp: bridge_port:`` must be bridged into the
    WhatsApp PlatformConfig.extra by the gateway config loader."""
    import yaml

    import gateway.config as gwconfig

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "gateway": {"platforms": {"whatsapp": {"enabled": True}}},
                "whatsapp": {"bridge_port": 8799},
            }
        ),
        encoding="utf-8",
    )
    # load_gateway_config() reads from get_hermes_home()/config.yaml — point it
    # at our temp home so we exercise the real loader without env-var noise.
    monkeypatch.setattr(gwconfig, "get_hermes_home", lambda: tmp_path)
    cfg = gwconfig.load_gateway_config()
    wa = cfg.platforms.get(Platform.WHATSAPP)
    assert wa is not None
    assert wa.extra.get("bridge_port") == 8799
