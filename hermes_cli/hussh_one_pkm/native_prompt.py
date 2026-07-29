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


def _run_native_script(script: str) -> str:
    """Run AppleScript through stdin so secrets never become process arguments."""
    if sys.platform != "darwin":
        raise RuntimeError("Hussh One vault enrollment is currently available on macOS only.")
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script,
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The Hussh One vault prompt timed out.") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not open the native Hussh One vault prompt.")
    return result.stdout.rstrip("\r\n")


def _apple_string(value: str) -> str:
    """Encode a value as an AppleScript literal without shell interpolation."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def prompt_for_vault_passphrase() -> str | None:
    """Return a passphrase from a native masked prompt, or ``None`` on cancel.

    Error output is intentionally discarded so a secret is never relayed into
    a terminal transcript or application log.
    """
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
    value = _run_native_script(script)
    return None if value == _CANCELLED else value


def prompt_for_new_vault_passphrase() -> str | None:
    """Collect and confirm a new vault passphrase in the protected native UI."""
    script = "\n".join(
        (
            "try",
            'set firstResponse to display dialog "Create a Hussh One vault passphrase. It must be at least 8 characters." default answer "" with hidden answer buttons {"Cancel", "Continue"} default button "Continue" cancel button "Cancel" with title "Hussh One"',
            'set secondResponse to display dialog "Confirm your Hussh One vault passphrase." default answer "" with hidden answer buttons {"Cancel", "Create vault"} default button "Create vault" cancel button "Cancel" with title "Hussh One"',
            "set firstValue to text returned of firstResponse",
            "set secondValue to text returned of secondResponse",
            'if firstValue is not secondValue then return "__HUSSH_ONE_PROMPT_MISMATCH__"',
            "return firstValue",
            "on error number -128",
            f'return "{_CANCELLED}"',
            "end try",
        )
    )
    value = _run_native_script(script)
    if value == _CANCELLED:
        return None
    if value == "__HUSSH_ONE_PROMPT_MISMATCH__":
        raise RuntimeError("The vault passphrases did not match.")
    return value


def disclose_recovery_key(recovery_key: str) -> bool:
    """Require a native save or copy acknowledgement for a one-time recovery key.

    The value is supplied to ``osascript`` over stdin.  It is not placed in a
    command argument, environment variable, terminal transcript, or chat.
    """
    script = "\n".join(
        (
            "try",
            f"set recoveryKey to {_apple_string(recovery_key)}",
            'set actionChoice to button returned of display dialog "Save this recovery key now. It is shown only once and is required if you lose your passphrase." & return & return & recoveryKey buttons {"Cancel", "Copy", "Save File"} default button "Save File" cancel button "Cancel" with title "Hussh One"',
            'if actionChoice is "Copy" then',
            "set the clipboard to recoveryKey",
            'display dialog "The recovery key was copied to your clipboard. Store it in a secure place before continuing." buttons {"Cancel", "I\'ve Saved It"} default button "I\'ve Saved It" cancel button "Cancel" with title "Hussh One"',
            "else",
            'set targetFile to choose file name with prompt "Save your Hussh One recovery key" default name "hushh-recovery-key.txt"',
            "set fileHandle to open for access targetFile with write permission",
            "set eof of fileHandle to 0",
            "write (\"Hussh Recovery Key\" & return & return & recoveryKey & return) to fileHandle",
            "close access fileHandle",
            "do shell script \"/bin/chmod 600 \" & quoted form of POSIX path of targetFile",
            'display dialog "The recovery key file was saved. Keep it somewhere secure before continuing." buttons {"Cancel", "I\'ve Saved It"} default button "I\'ve Saved It" cancel button "Cancel" with title "Hussh One"',
            "end if",
            'return "confirmed"',
            "on error number -128",
            f'return "{_CANCELLED}"',
            "end try",
        )
    )
    return _run_native_script(script) == "confirmed"


def confirm_disconnect(account_email: str) -> bool:
    """Confirm the destructive trusted-device disconnect outside chat context."""
    script = "\n".join(
        (
            "try",
            f"set accountEmail to {_apple_string(account_email)}",
            'set answer to button returned of display dialog ("Disconnect this Hermes profile from Hussh One (" & accountEmail & ")? This revokes the trusted device and removes its local vault envelope.") buttons {"Cancel", "Disconnect"} default button "Cancel" cancel button "Cancel" with title "Hussh One"',
            'return "confirmed"',
            "on error number -128",
            f'return "{_CANCELLED}"',
            "end try",
        )
    )
    return _run_native_script(script) == "confirmed"
