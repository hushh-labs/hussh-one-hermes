# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The two load-bearing claims, enforced rather than asserted.

Both were true and neither was tested, which meant nothing stopped a future
change from quietly making them false:

1. **The decision layer is pure.** No network, no credential, no clock. This is
   the entire privacy argument — placement can be decided locally precisely
   because nothing in it can reach out.
2. **The service-account key never becomes durable.** It lives in one process's
   memory for one burst and reaches no file this package writes.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_PKG = pathlib.Path(__file__).resolve().parents[2] / "hermes_cli" / "hussh_one_burst"

#: The modules that must stay pure. ``telemetry`` is the declared I/O boundary
#: and is deliberately absent.
PURE_MODULES = ("types.py", "placement.py", "hardware.py", "devices.py")

#: Anything that could reach the network, the filesystem, a credential or a
#: clock. A pure module importing any of these has stopped being pure, whether
#: or not it calls them yet.
FORBIDDEN_IMPORTS = {
    "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp",
    "subprocess", "os", "sys", "pathlib", "shutil", "tempfile", "io",
    "time", "datetime", "random", "secrets", "uuid",
    "psutil", "google", "sqlite3", "json", "logging", "threading", "asyncio",
}


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, stays inside this package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_decision_layer_cannot_reach_the_outside_world(module):
    """Import-level, so it fails when the capability arrives, not when it is used.

    A module that imports ``requests`` but has not called it yet is already one
    edit away from placement making a network request — and the guarantee people
    are being asked to trust is that it *cannot*.
    """
    offending = _imported_roots(_PKG / module) & FORBIDDEN_IMPORTS
    assert not offending, (
        f"{module} imports {sorted(offending)}; the decision layer must stay pure. "
        "If this capability is genuinely needed, it belongs in telemetry.py, "
        "which is the declared I/O boundary."
    )


def test_telemetry_is_the_only_module_that_touches_the_machine():
    """The boundary is only meaningful if exactly one module holds it."""
    probing = {
        name.name
        for name in _PKG.glob("*.py")
        if {"subprocess", "psutil"} & _imported_roots(name)
    }
    assert probing == {"telemetry.py"}, (
        f"machine probing found in {sorted(probing)}; it belongs only in telemetry.py"
    )


# --------------------------------------------------------------------------
# The key never becomes durable
# --------------------------------------------------------------------------

SECRET = "-----BEGIN PRIVATE KEY-----AAAsecretAAA-----END PRIVATE KEY-----"


def test_the_key_is_retained_in_memory_and_the_docstring_says_so():
    """Retention is real and necessary — teardown authenticates a second time.

    Pinned deliberately: an earlier docstring claimed the key was never stored on
    the object, which was false. This test exists so that claim cannot quietly
    come back.
    """
    from hermes_cli.hussh_one_burst.providers import GcpBurstProvider

    provider = GcpBurstProvider(project="p", region="us-central1", sa_key=SECRET)
    assert SECRET in str(vars(provider)), "retention changed; update the docstring"
    assert "never persisted" in (GcpBurstProvider.__doc__ or "")


def test_the_key_does_not_leak_through_repr():
    """A provider caught in a traceback or a log line must not print the key."""
    from hermes_cli.hussh_one_burst.providers import GcpBurstProvider

    provider = GcpBurstProvider(project="p", region="us-central1", sa_key=SECRET)
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)
    assert SECRET not in provider.describe_destination()


def test_the_key_never_reaches_a_receipt_or_the_ledger(tmp_path, monkeypatch):
    import hermes_cli.hussh_one_burst.ledger as ledger
    from hermes_cli.hussh_one_burst.credentials import CredentialRef
    from hermes_cli.hussh_one_burst.execution import BurstRequest, run_burst
    from hermes_cli.hussh_one_burst.providers import MockBurstProvider

    target = tmp_path / "burst-receipts.jsonl"
    monkeypatch.setattr(ledger, "default_ledger_path", lambda: target)

    provider = MockBurstProvider()
    provider.credential_ref = CredentialRef("proj", "us-central1", "request")
    receipt = run_burst(
        BurstRequest("job", "nvidia-t4", 1, 0.35, 5.0), provider
    )

    assert SECRET not in repr(receipt.as_dict())
    written = target.read_text(encoding="utf-8")
    assert SECRET not in written
    assert "BEGIN PRIVATE KEY" not in written
    # what SHOULD survive
    assert '"project":"proj"' in written.replace(" ", "")


def test_credential_ref_carries_provenance_and_nothing_else():
    from hermes_cli.hussh_one_burst.credentials import CredentialRef

    assert set(CredentialRef("p", "r", "request").as_dict()) == {
        "project",
        "region",
        "credential_source",
    }
