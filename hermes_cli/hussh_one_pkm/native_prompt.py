# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Native macOS prompt for the Hussh One vault passphrase.

This module is deliberately usable by the local TUI and the Desktop bridge
without placing a vault passphrase in a chat message, configuration file, or
model context.
"""

from __future__ import annotations

import subprocess
import sys


_CANCELLED = "__HUSSH_ONE_PROMPT_CANCELLED__"


def prompt_for_vault_passphrase() -> str | None:
    """Return a passphrase from a native masked prompt, or ``None`` on cancel.

    Error output is intentionally discarded so a secret is never relayed into
    a terminal transcript or application log.
    """
    if sys.platform != "darwin":
        raise RuntimeError("Hussh One vault enrollment is currently available on macOS only.")

    script = "\n".join(
        (
            "try",
            'set response to display dialog "Enter your Hussh One vault passphrase to secure this device." default answer "" with hidden answer buttons {"Cancel", "Secure this device"} default button "Secure this device" cancel button "Cancel" with title "Hussh One"',
            "return text returned of response",
            "on error number -128",
            f'return "{_CANCELLED}"',
            "end try",
        )
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The Hussh One vault prompt timed out.") from exc

    if result.returncode != 0:
        raise RuntimeError("Could not open the native Hussh One vault prompt.")
    value = result.stdout.rstrip("\r\n")
    return None if value == _CANCELLED else value
