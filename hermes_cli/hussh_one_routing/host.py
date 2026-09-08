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
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

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


_LMSTUDIO_APP_PATTERN = r"/Applications/LM Studio\.app/Contents/MacOS/LM Studio$"

_LMSTUDIO_APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "LM Studio"
)

# Electron's single-instance guard is THREE files, not one. Removing only
# `SingletonLock` -- which is what this module did until 2026-09-04 -- leaves
# `SingletonSocket` as a dangling symlink into a scoped temp directory that
# SIGKILL never cleaned up, and `SingletonCookie` beside it. The next launch
# then finds a socket path it can neither connect to nor rebind, concludes it
# is a secondary instance, and exits 0 in milliseconds: no window, no port, no
# crash report, no line in the app's own main.log. From the outside that is
# indistinguishable from "LM Studio crashed", which is exactly how it was
# misread on 2026-09-01 and again on 2026-09-04.
#
# Observed on 2026-09-04: `SingletonCookie -> 7410081187091955945` and
# `SingletonSocket -> /var/.../T/scoped_dir6xduje/SingletonSocket`, both
# stamped 2026-09-01 16:12, i.e. surviving a full three days and every
# `open -a`, `open -n -a`, `launchctl asuser open`, direct-binary launch and
# `lms server start` attempted against them.
_LMSTUDIO_SINGLETON_FILES = (
    _LMSTUDIO_APP_SUPPORT / "SingletonLock",
    _LMSTUDIO_APP_SUPPORT / "SingletonSocket",
    _LMSTUDIO_APP_SUPPORT / "SingletonCookie",
)

# Backwards compatibility for anything importing the old single-file name.
_LMSTUDIO_SINGLETON_LOCK = _LMSTUDIO_SINGLETON_FILES[0]

# Measured cold start on this machine, after the lock-cleanup fix below: about
# 128s from a `kill -9` to the server accepting connections. Before that fix
# was found, three restart attempts this session were misdiagnosed as "LM
# Studio crashed" when the real cause was this timeout giving up seconds
# before a slow-but-healthy start would have finished. 4x the measured value,
# matching this codebase's own headroom convention elsewhere (BUDGET_HEADROOM
# in reasoning.py), rather than the bare measurement.
DEFAULT_RESTART_TIMEOUT_S = 512.0


def restart_app(
    *, base_url: str = DEFAULT_SERVER_ROOT, timeout: float = DEFAULT_RESTART_TIMEOUT_S
) -> None:
    """Quit and relaunch the LM Studio app, then wait for its server.

    **Why this exists, found 2026-09-01 by direct test on two models:** once a
    model has loaded at some context within one running LM Studio app process,
    reloading it at a DIFFERENT context does not take effect -- not through the
    ``lms load -c`` flag, not through the persisted per-model default config
    file, not through both together, not through a plain unload and reload.
    The model comes back at whatever context it first held in that process's
    lifetime. Proven directly: with the config file forced to a value distinct
    from both the currently-loaded context and the one requested, a reload
    still returned the currently-loaded value, unchanged. Only a fresh LM
    Studio process, loading the model for the first time, correctly honours
    the persisted config. This is macOS-only and touches the user's actual
    running app -- it is not something to call speculatively; see
    :func:`ensure_context`, which calls it at most once per request and only
    after a plain reload has already been tried and shown to not take effect.

    **The stale-lock trap, found the same day the hard way.** LM Studio
    ignores SIGTERM, so this has to SIGKILL it, and SIGKILL skips the normal
    shutdown path that removes Electron's `SingletonLock` file. Every
    subsequent launch attempt -- `open -a`, `open -n -a`, even the raw binary
    -- then silently no-ops: it sees the stale lock, assumes another instance
    already owns it, and exits without ever binding the server port or
    printing an error. Three restarts this session were misread as "LM
    Studio crashed" before this was traced to its actual cause: the SAME
    lock file being manually removed once, by hand, and never encoded into
    this function, so every kill after that first manual fix recreated the
    exact same trap for the next call. Removed here, every time, so the fix
    survives past the one session that found it.
    """
    if sys.platform != "darwin":
        raise RuntimeError("restart_app is only implemented for macOS")
    subprocess.run(
        ["pkill", "-9", "-f", _LMSTUDIO_APP_PATTERN],
        capture_output=True,
        check=False,
    )
    time.sleep(1.0)  # let the OS finish tearing the killed process down
    for stale in _LMSTUDIO_SINGLETON_FILES:
        # `unlink` on a DANGLING symlink still removes the link, which is the
        # case that matters here; `missing_ok` covers the already-clean case.
        try:
            stale.unlink(missing_ok=True)
        except OSError as exc:  # noqa: PERF203 - one file, one diagnosis
            logger.warning("could not remove stale %s: %s", stale.name, exc)
    subprocess.run(["open", "-a", "LM Studio"], capture_output=True, check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/v0/models", timeout=3)
            return
        except Exception:  # noqa: BLE001
            time.sleep(3)
    raise RuntimeError(
        f"LM Studio server did not come back within {timeout}s of restart. "
        "The singleton files were cleared, so this is NOT the stale-lock trap. "
        "Check whether an LM Studio process exists at all (`pgrep -f 'LM "
        "Studio'`): if none does and ~/Library/Logs/'LM Studio'/main.log has "
        "no entry for the launch, the app is refusing to start for a reason "
        "outside this process's reach and needs a human to open it from the "
        "Dock. Do not keep force-killing or deleting more of its local state."
    )


def ensure_context(
    model: str,
    context_length: int,
    *,
    unload: Callable[[str], bool],
    resident: Callable[[], list],
    load: Callable[..., Optional[int]] = load_at_context,
    restart: Optional[Callable[[], None]] = None,
    max_restarts: int = 1,
    current: Optional[Callable[[str], Optional[int]]] = None,
) -> Optional[int]:
    """Load ``model`` at ``context_length``, self-healing across the LM
    Studio session-stickiness described in :func:`restart_app`.

    **Idempotent first.** If ``model`` already holds ``context_length`` (read
    back through ``current``, by default :func:`loaded_context`), nothing is
    touched: no drain, no reload. This host also runs the founder's live
    gateway and its cron jobs against the same LM Studio, and on 2026-09-02
    a replay run drained an already-correct model out from under a running
    cron job, which died with "Model unloaded". A harness must never evict a
    model that is already what it asked for.

    A plain drain-and-load cannot tell "genuinely stuck at a stale context"
    apart from "the server clamped a request it cannot satisfy" -- both look
    like a mismatch on readback. This wraps exactly one restart-and-retry
    around the attempt, because restarting is the only known fix and should
    not be tried more than once per call: a second mismatch after a fresh
    process means the request itself cannot be met, not that the process is
    still stale.

    Without ``restart``, this is drain-then-load and nothing else -- a
    mismatch is returned rather than silently retried, exactly like a rung
    :func:`hermes_cli.hussh_one_routing.ladder.walk` already treats as a
    load error. Pass the real :func:`restart_app` to get the self-healing
    behaviour in production; tests pass a fake that flips a flag instead of
    touching the real app.
    """
    from .ladder import drain

    probe = current or loaded_context
    try:
        already = probe(model)
    except Exception:  # noqa: BLE001 - a failed readback just means "load it"
        already = None
    if already == context_length:
        return already

    loaded: Optional[int] = None
    for attempt in range(max_restarts + 1):
        drained = drain(unload=unload, resident=resident)
        if not drained["empty"]:
            logger.warning(
                "could not drain before loading %s; still resident: %s",
                model, ", ".join(drained["still_resident"]),
            )
            return None
        loaded = load(model, context_length)
        if loaded == context_length:
            return loaded
        if attempt >= max_restarts or restart is None:
            return loaded
        logger.warning(
            "%s loaded at %s instead of the requested %s; restarting LM "
            "Studio to clear a stale session context (attempt %s/%s)",
            model, loaded, context_length, attempt + 1, max_restarts,
        )
        restart()
    return loaded


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
