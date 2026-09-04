# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Tell Hussh One what this machine is doing, when it changes.

A fixed poll pays the same price whether anything happened or not, and it is
wrong in both directions: too slow to show a model swap the owner just made,
and still billing every five minutes for a laptop asleep in a bag.

So this pushes on transitions -- connect, model loaded or ejected, session
start and end, seal -- and keeps a slow keepalive underneath, whose only job is
to distinguish "nothing changed" from "this machine is gone".

Two rules keep it from becoming a poll again:

  - Identical snapshots are not sent. Transitions are the signal; a repeat
    carries none, and the last-seen timestamp already moves on the keepalive.
  - A push never blocks the thing that triggered it. Emitting is best-effort
    and failure is silent: the dashboard showing a staler reading is a much
    smaller harm than a model load waiting on a network call.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Slow enough that an idle machine costs almost nothing, short enough that the
# UI can call a device stale without a long ambiguous window. The reader's
# freshness threshold must sit above this; see sync-display.ts.
KEEPALIVE_INTERVAL_SECONDS = 600.0

# A burst of transitions (a model load emits several) collapses into one push.
MIN_PUSH_INTERVAL_SECONDS = 5.0

#: One keeps at most this many characters of any text field (the service's
#: ``_HEARTBEAT_MAX_TEXT``). Capped here as well so a long LM Studio model id
#: can never be the reason a heartbeat is refused: the beat is the liveness
#: signal, and a device that cannot land one reads as gone while it is fine.
SERVER_TEXT_MAX = 120


def _text(value: Any) -> str:
    return str(value or "").strip()[:SERVER_TEXT_MAX]


class PresencePublisher:
    """Coalesce runtime transitions into pushes, with a keepalive underneath."""

    def __init__(
        self,
        *,
        publish: Callable[[dict[str, Any]], bool],
        snapshot: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
        keepalive_seconds: float = KEEPALIVE_INTERVAL_SECONDS,
        min_interval_seconds: float = MIN_PUSH_INTERVAL_SECONDS,
    ) -> None:
        self._publish = publish
        self._snapshot = snapshot
        self._clock = clock
        self._keepalive = float(keepalive_seconds)
        self._min_interval = float(min_interval_seconds)
        self._lock = threading.Lock()
        self._last_sent: Optional[dict[str, Any]] = None
        self._last_sent_at: float = 0.0
        self._pending = False

    def on_event(self, reason: str, *, force: bool = False) -> bool:
        """Record a transition and push if it says something new.

        ``force`` is for events that must land even when the snapshot is
        unchanged -- a seal, most importantly, where the point is to be heard
        rather than to report a value.
        """
        try:
            snapshot = dict(self._snapshot() or {})
        except Exception:
            logger.debug("presence snapshot failed for %s", reason, exc_info=True)
            return False
        snapshot["reason"] = reason
        return self._maybe_send(snapshot, force=force)

    def keepalive(self) -> bool:
        """Push only if the keepalive window has elapsed.

        Safe to call as often as the caller likes: the window, not the caller's
        cadence, decides whether anything is sent.
        """
        with self._lock:
            due = (self._clock() - self._last_sent_at) >= self._keepalive
        if not due:
            return False
        return self.on_event("keepalive", force=True)

    def _maybe_send(self, snapshot: dict[str, Any], *, force: bool) -> bool:
        with self._lock:
            now = self._clock()
            comparable = {k: v for k, v in snapshot.items() if k != "reason"}
            previous = (
                {k: v for k, v in self._last_sent.items() if k != "reason"}
                if self._last_sent
                else None
            )
            if not force:
                if previous == comparable:
                    # Nothing changed. Sending would turn this back into a poll
                    # with extra steps.
                    self._pending = False
                    return False
                if (now - self._last_sent_at) < self._min_interval:
                    # A burst is one transition as far as the owner is
                    # concerned. Mark it and let the next call carry it.
                    self._pending = True
                    return False
            self._last_sent = dict(snapshot)
            self._last_sent_at = now
            self._pending = False

        try:
            return bool(self._publish(snapshot))
        except Exception:
            # Never propagate: this runs off a model load or a session start,
            # and neither should fail because a telemetry push did.
            logger.debug("presence push failed", exc_info=True)
            return False

    def on_event_background(self, reason: str, *, force: bool = False) -> None:
        """Fire a push on a daemon thread and return immediately.

        The callers are the vault unlock and the revocation tick, both of which
        sit in the owner's critical path. A push is telemetry; it must never add
        network latency to an unlock, and a hung connection must never hold one
        open. Nothing waits on the result, so nothing here returns one.
        """
        thread = threading.Thread(
            target=self.on_event,
            args=(reason,),
            kwargs={"force": force},
            name="hussh-presence-push",
            daemon=True,
        )
        thread.start()

    def keepalive_background(self) -> None:
        """Keepalive on a daemon thread, for callers in the critical path.

        The window check is cheap and happens on the calling thread, so a
        keepalive that is not due costs nothing and starts no thread.
        """
        with self._lock:
            due = (self._clock() - self._last_sent_at) >= self._keepalive
        if not due:
            return
        self.on_event_background("keepalive", force=True)

    @property
    def has_pending_change(self) -> bool:
        """True when a change was coalesced away and not yet sent."""
        with self._lock:
            return self._pending

    def flush(self) -> bool:
        """Send a coalesced change that the rate limit held back."""
        if not self.has_pending_change:
            return False
        return self.on_event("flush", force=True)


def current_model_from_config(cfg: Any) -> str:
    """The pinned model in a loaded config, read the way ``agent_section`` reads it.

    ``model.default`` wins, ``model.model`` is the legacy spelling, and anything
    unreadable is "" so the snapshot simply omits the field rather than
    reporting a model that is not there.
    """
    if not isinstance(cfg, dict):
        return ""
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return str(model_cfg.get("default") or model_cfg.get("model") or "").strip()


def current_model() -> str:
    """The model pinned in config right now; "" when the config cannot be read.

    Read at call time, never cached: the point of a transition push is to show
    the pin at the moment of the push, and a config problem must degrade the
    reading, never block the heartbeat that carries it.
    """
    try:
        from hermes_cli.config import load_config_readonly

        return current_model_from_config(load_config_readonly())
    except Exception:  # noqa: BLE001 - an unreadable config must not block a heartbeat
        logger.debug("current model unavailable", exc_info=True)
        return ""


def agent_version() -> str:
    """This Hermes build's version; "" when it cannot be determined."""
    try:
        from hermes_cli import __version__

        return str(__version__ or "").strip()
    except Exception:  # noqa: BLE001 - a missing version must not block a heartbeat
        return ""


def build_snapshot(
    *,
    current_model: str = "",
    active_sessions: int = 0,
    busy: bool = False,
    agent_version: str = "",
) -> dict[str, Any]:
    """Assemble what this machine reports about itself.

    Every field is on the server's heartbeat allow-list; anything else would be
    dropped there anyway. Hardware is included so the owner sees the machine
    their agent runs on, and it is names only -- brand and processor, never a
    serial number, hostname, or MAC, none of which the dashboard needs and all
    of which would identify the machine rather than describe it.
    """
    snapshot: dict[str, Any] = {
        "current_model": _text(current_model),
        "active_sessions": max(0, int(active_sessions)),
        "busy": bool(busy),
    }
    if agent_version:
        snapshot["agent_version"] = _text(agent_version)

    try:
        from hermes_cli.hussh_one_host_metrics import host_hardware

        hardware = host_hardware()
        for key in ("brand", "processor"):
            value = hardware.get(key)
            if value:
                snapshot[key] = _text(value)
        ram_total = hardware.get("ram_total_gb")
        if isinstance(ram_total, (int, float)) and ram_total > 0:
            snapshot["ram_total_gb"] = round(float(ram_total), 2)
    except Exception:
        logger.debug("host hardware unavailable", exc_info=True)

    try:
        from hermes_cli.hussh_one_host_metrics import host_battery

        battery = host_battery()
        # A desktop reports present: False and no percentage. Only a machine
        # with a battery gets battery fields, so a Mac Studio never shows 0%.
        if battery.get("present"):
            percent = battery.get("percent")
            if isinstance(percent, (int, float)):
                snapshot["battery_pct"] = round(float(percent))
            snapshot["battery_charging"] = bool(battery.get("charging"))
            snapshot["on_ac"] = bool(battery.get("on_ac"))
            minutes = battery.get("minutes_remaining")
            if isinstance(minutes, int) and minutes > 0:
                snapshot["battery_minutes_remaining"] = minutes
    except Exception:
        logger.debug("battery unavailable", exc_info=True)

    try:
        from hermes_cli.hussh_one_lmstudio import host_memory

        memory = host_memory()
        total = memory.get("total_gb")
        available = memory.get("available_gb")
        if (
            isinstance(total, (int, float))
            and isinstance(available, (int, float))
            and total > 0
        ):
            used_pct = (1.0 - (float(available) / float(total))) * 100.0
            # Clamped only against arithmetic drift at the boundaries, not to
            # rescue an implausible reading: the server drops out-of-range
            # values rather than inventing one.
            snapshot["ram_used_pct"] = round(min(100.0, max(0.0, used_pct)), 2)
    except Exception:
        logger.debug("host memory unavailable", exc_info=True)

    return {key: value for key, value in snapshot.items() if value != ""}
