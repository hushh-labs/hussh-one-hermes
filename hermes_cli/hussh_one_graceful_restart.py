# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Restart without cutting anyone off mid-answer.

Hermes has no hot reload, and that is deliberate rather than missing: config is
memoised per process, so the canonical way to apply a change is to replace the
process. The same semantics as the pod, which is replaced and never patched.

The gap is that "replace the process" says nothing about the turns running
inside it. A restart mid-turn loses the owner's answer, and the transcript is
left describing a question nobody answered.

So this drains first:

  1. **Quiesce.** Stop accepting new turns. Without this the drain never ends on
     a busy machine, because a finishing turn is replaced by a starting one.
  2. **Drain.** Wait for in-flight turns to finish, bounded by a deadline.
  3. **Report.** Publish state the whole time, because a silent thirty-second
     wait is indistinguishable from a hang, and someone will kill it.
  4. **Restart**, through the existing supervisor path rather than a new one.

The deadline is the interesting decision. A drain that waits forever turns one
stuck turn into an agent that can never be updated; a drain that gives up
instantly is the abrupt restart it was meant to replace. So it waits, then
escalates visibly and states what it is about to interrupt.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Long enough for a slow local model to finish a turn -- a 31B answering on
# battery can take tens of seconds -- and short enough that an update is not
# hostage to one wedged session.
DEFAULT_DRAIN_TIMEOUT_S = 90.0

# How often the drain re-checks and republishes state.
POLL_INTERVAL_S = 0.5

PHASE_IDLE = "idle"
PHASE_QUIESCING = "quiescing"
PHASE_DRAINING = "draining"
PHASE_READY = "ready"
PHASE_RESTARTING = "restarting"
PHASE_ABANDONED = "abandoned"

# Where the phase is published for the app and the Puppy One surface to read.
STATUS_FILENAME = "restart-status.json"


@dataclass
class RestartStatus:
    """What the app and Puppy One show while a restart is in progress."""

    phase: str = PHASE_IDLE
    active_turns: int = 0
    waited_s: float = 0.0
    deadline_s: float = DEFAULT_DRAIN_TIMEOUT_S
    reason: str = ""
    # Populated only when the drain gave up, naming what it interrupted, so an
    # abandoned drain is legible afterwards instead of just a restarted process.
    interrupted_sessions: list[str] = field(default_factory=list)

    def message(self) -> str:
        """One line a person can read, not a status code."""
        if self.phase == PHASE_IDLE:
            return "Running."
        if self.phase == PHASE_QUIESCING:
            return "Finishing current work before restarting."
        if self.phase == PHASE_DRAINING:
            if self.active_turns == 1:
                return "Waiting for 1 reply to finish before restarting."
            return f"Waiting for {self.active_turns} replies to finish before restarting."
        if self.phase == PHASE_READY:
            return "Ready to restart."
        if self.phase == PHASE_RESTARTING:
            return "Restarting now."
        if self.phase == PHASE_ABANDONED:
            count = len(self.interrupted_sessions)
            return (
                f"Restarting after {self.waited_s:.0f}s; "
                f"{count} reply{'' if count == 1 else 'ies'} did not finish."
            )
        return self.phase


def status_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "health" / STATUS_FILENAME


def publish(status: RestartStatus, *, hermes_home: Optional[Path | str] = None) -> None:
    """Write the phase where other processes can see it.

    Best-effort and never raises. A restart must not fail because it could not
    describe itself, but a silent wait is indistinguishable from a hang, so the
    attempt is always made.

    Written atomically: a reader that catches a half-written file would show
    the owner a parse error in place of a status.
    """
    try:
        path = status_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(status)
        payload["message"] = status.message()
        payload["at"] = int(time.time())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("could not publish restart status", exc_info=True)


def read_status(hermes_home: Optional[Path | str] = None) -> dict[str, Any]:
    """Current phase, or idle when nothing has published one."""
    try:
        return json.loads(status_path(hermes_home).read_text(encoding="utf-8"))
    except Exception:
        return {"phase": PHASE_IDLE, "message": "Running.", "active_turns": 0}


def count_active_turns(registry: Any) -> tuple[int, list[str]]:
    """In-flight turns, and the sessions holding them.

    Reads the lease registry rather than a separate counter. A second counter
    would drift from the leases, and a drain that trusted it would restart in
    the middle of a turn while reporting that none were running.
    """
    sessions: list[str] = []
    leases = getattr(registry, "_leases", None)
    if not isinstance(leases, dict):
        return 0, []
    for session_id, lease in list(leases.items()):
        try:
            if not lease.idle:
                sessions.append(str(session_id))
        except Exception:
            # An unreadable lease is counted as busy. Guessing "free" here
            # would restart through the turn this function exists to protect.
            sessions.append(str(session_id))
    return len(sessions), sorted(sessions)


def drain(
    *,
    registry: Any,
    quiesce: Optional[Callable[[], None]] = None,
    resume: Optional[Callable[[], None]] = None,
    timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    poll_s: float = POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    hermes_home: Optional[Path | str] = None,
    reason: str = "",
) -> RestartStatus:
    """Stop new turns, wait for running ones, and report throughout.

    Returns a status whose phase is READY (drained cleanly) or ABANDONED (the
    deadline passed with turns still running). The caller decides what to do
    with ABANDONED; this function never restarts on its own, because "drain"
    and "restart" failing together would be indistinguishable from here.
    """
    status = RestartStatus(phase=PHASE_QUIESCING, deadline_s=timeout_s, reason=reason)
    publish(status, hermes_home=hermes_home)

    if quiesce is not None:
        try:
            quiesce()
        except Exception:
            # A failed quiesce means new turns keep arriving, so the drain may
            # not converge. Say so rather than waiting out the full deadline
            # while appearing to make progress.
            logger.warning("quiesce failed; draining without it", exc_info=True)
            status.reason = (status.reason + " (quiesce failed)").strip()

    started = clock()
    try:
        while True:
            active, sessions = count_active_turns(registry)
            status.active_turns = active
            status.waited_s = round(clock() - started, 2)

            if active == 0:
                status.phase = PHASE_READY
                publish(status, hermes_home=hermes_home)
                return status

            if status.waited_s >= timeout_s:
                status.phase = PHASE_ABANDONED
                status.interrupted_sessions = sessions
                publish(status, hermes_home=hermes_home)
                logger.warning(
                    "drain deadline reached after %.1fs with %d turn(s) still "
                    "running: %s",
                    status.waited_s,
                    active,
                    ", ".join(sessions[:5]),
                )
                return status

            status.phase = PHASE_DRAINING
            publish(status, hermes_home=hermes_home)
            sleep(poll_s)
    except BaseException:
        # Any exit that is not a completed drain must lift the quiesce, or a
        # cancelled restart leaves the agent permanently refusing new turns --
        # a worse outcome than the interrupted turn this was avoiding.
        if resume is not None:
            try:
                resume()
            except Exception:
                logger.error("could not resume after failed drain", exc_info=True)
        status.phase = PHASE_IDLE
        publish(status, hermes_home=hermes_home)
        raise


def restart_now(
    *,
    status: RestartStatus,
    hermes_home: Optional[Path | str] = None,
    pid: Optional[int] = None,
    # Resolved at call time, not bound as a default. A default argument
    # captures os.kill at import, so a caller (or a test) that replaces os.kill
    # would be ignored and this would signal the real process -- which, for a
    # function whose whole job is sending a signal, is the worst possible thing
    # to get wrong.
    signaller: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """Hand off to the supervisor through the existing in-band path.

    SIGUSR1 is the established restart signal here; this adds the drain in
    front of it rather than a second mechanism beside it.
    """
    status.phase = PHASE_RESTARTING
    publish(status, hermes_home=hermes_home)
    target = pid if pid is not None else os.getpid()
    send = signaller if signaller is not None else os.kill
    # SIGUSR1 does not exist on Windows; a bare reference raises AttributeError
    # at call time there. The gateway supervisor this signals is a POSIX
    # arrangement anyway, so on a platform without the signal the honest answer
    # is "this handoff path is unavailable", not a crash.
    restart_signal = getattr(signal, "SIGUSR1", None)
    if restart_signal is None:
        logger.error("SIGUSR1 unavailable on this platform; cannot hand off restart")
        status.phase = PHASE_IDLE
        status.reason = "restart signal unsupported on this platform"
        publish(status, hermes_home=hermes_home)
        return False
    try:
        send(target, restart_signal)
        return True
    except Exception:
        logger.error("could not signal restart to pid %s", target, exc_info=True)
        status.phase = PHASE_IDLE
        status.reason = "restart signal failed"
        publish(status, hermes_home=hermes_home)
        return False


def graceful_restart(
    *,
    registry: Any,
    quiesce: Optional[Callable[[], None]] = None,
    resume: Optional[Callable[[], None]] = None,
    timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    force: bool = False,
    hermes_home: Optional[Path | str] = None,
    reason: str = "",
    **drain_kwargs: Any,
) -> RestartStatus:
    """Drain, then restart.

    A drain that hit its deadline does NOT restart unless `force` is set. The
    default protects the turn; the flag exists because an agent that can never
    be updated because one session is wedged is its own kind of broken, and
    that call belongs to the operator rather than to this function.
    """
    status = drain(
        registry=registry,
        quiesce=quiesce,
        resume=resume,
        timeout_s=timeout_s,
        hermes_home=hermes_home,
        reason=reason,
        **drain_kwargs,
    )
    if status.phase == PHASE_ABANDONED and not force:
        if resume is not None:
            try:
                resume()
            except Exception:
                logger.error("could not resume after abandoned drain", exc_info=True)
        return status
    restart_now(status=status, hermes_home=hermes_home)
    return status
