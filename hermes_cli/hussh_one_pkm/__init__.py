# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Hussh One trusted-device and local PKM bridge.

This package is an edge capability. It does not add a Hermes core model tool or
change the MCP handshake.
"""

from .bridge import HusshVaultBridge

__all__ = ["HusshVaultBridge"]
