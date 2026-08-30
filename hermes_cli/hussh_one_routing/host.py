# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Wire the ladder's injected I/O to the real LM Studio on this machine.

``walk`` takes its host operations as callables so it can be tested without a
server and without evicting anything. This is the other half: the adapter that
supplies the real ones.

**Loading is explicit, and the context is read back.** Just-in-time loading is
what produced the worst comparability bug in this harness so far: a ladder ran
one model at 262144 and another at 16384, a 16x difference in KV cache and in
how much of a prompt survives, and nothing in the output showed it. JIT loads at
the server's per-model default, and those defaults differ. So a rung is loaded
deliberately at a pinned context, and the value that goes into the manifest is
the one the server reports afterwards, not the one the load asked for.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_SERVER_ROOT = "http://127.0.0.1:1234"

# Floor, not the target. The context to run at is `common_max_context()` of the
# actual ladder, read from the server, because it changes with the membership:
# every current model reaches 262144 except gemma-4-e2b at 131072, so including
# e2b halves the whole ladder's context. This value is only the point below
# which a run stops being a fair test of long-context behaviour at all.
MINIMUM_LADDER_CONTEXT = 98304

# Kept for callers that want a safe default without querying the server first.
LADDER_CONTEXT_LENGTH = 131072

LOAD_TIMEOUT_S = 900.0


def _lms() -> str:
    from hermes_cli.hussh_one_lmstudio import _lms_binary

    binary = _lms_binary()
    if not binary:
        raise RuntimeError("lms CLI not found; cannot pin a context length")
    return binary


def model_catalog(
    *, base_url: str = DEFAULT_SERVER_ROOT, timeout: float = 20.0
) -> list:
    """Every model the server knows about, with its context fields."""
    request = urllib.request.Request(f"{base_url}/api/v0/models")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return [m for m in payload.get("data", []) if m.get("type") in ("llm", "vlm")]


def max_context(model: str, **kwargs: Any) -> Optional[int]:
    """The largest context this model accepts, as the server reports it."""
    for entry in model_catalog(**kwargs):
        if entry.get("id") == model:
            return entry.get("max_context_length")
    return None


def common_max_context(models: list, **kwargs: Any) -> Optional[int]:
    """The largest context *every* listed model supports.

    A ladder is only as wide as its narrowest rung. Pinning to the widest model
    would either drop the narrow ones or, worse, let them load at something
    else and be compared anyway.
    """
    caps = []
    for model in models:
        cap = max_context(model, **kwargs)
        if cap:
            caps.append(cap)
    if not caps:
        return None
    common = min(caps)
    if common < MINIMUM_LADDER_CONTEXT:
        logger.warning(
            "ladder context %s is below the %s floor; one model is dragging the "
            "whole comparison down to a window that does not test long context",
            common,
            MINIMUM_LADDER_CONTEXT,
        )
    return common


def loaded_context(
    model: str, *, base_url: str = DEFAULT_SERVER_ROOT, timeout: float = 20.0
) -> Optional[int]:
    """The context a loaded model is actually running at."""
    for entry in model_catalog(base_url=base_url, timeout=timeout):
        if entry.get("id") == model and entry.get("state") == "loaded":
            return entry.get("loaded_context_length")
    return None


def load_at_context(
    model: str,
    context_length: int,
    *,
    base_url: str = DEFAULT_SERVER_ROOT,
    timeout: float = LOAD_TIMEOUT_S,
) -> Optional[int]:
    """Load one model at a pinned context and report what actually loaded.

    The return value is read back from the server. A load that succeeds at a
    clamped context is not the run that was asked for, and the walk refuses it
    rather than recording the requested number.
    """
    result = subprocess.run(
        [_lms(), "load", model, "-c", str(context_length), "-y"],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "lms load failed").strip()[:300]
        )
    return loaded_context(model, base_url=base_url, timeout=30.0)


def resident() -> list:
    """Models currently holding weights."""
    from hermes_cli.hussh_one_lmstudio import loaded_models

    return loaded_models()


def unload(identifier: str) -> bool:
    from hermes_cli.hussh_one_lmstudio import unload_model

    return unload_model(identifier)


def available_gb() -> Optional[float]:
    from hermes_cli.hussh_one_lmstudio import host_memory

    reading = host_memory()
    value = reading.get("available_gb")
    return float(value) if isinstance(value, (int, float)) else None


def ladder_io(context_length: int = LADDER_CONTEXT_LENGTH) -> dict:
    """The keyword arguments ``walk`` needs to drive the real host."""
    return {
        "unload": unload,
        "resident": resident,
        "available_gb": available_gb,
        "load": load_at_context,
        "context_length": context_length,
    }
