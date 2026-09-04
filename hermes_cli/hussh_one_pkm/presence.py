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

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
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

#: The two list summaries are capped at ten rows each, matching what One
#: accepts. A summary is a glance, not a listing: the owner opens the device to
#: see the rest, and a heartbeat that grows with the machine's history stops
#: being a heartbeat.
SUMMARY_MAX_ROWS = 10

#: Per-field caps for the scheduled-work rows. Each field is cut to the width
#: One stores, so an over-long value is never the reason a whole beat is
#: refused (the same reasoning as ``SERVER_TEXT_MAX``).
SCHEDULED_NAME_MAX = 80
SCHEDULED_WHEN_MAX = 40
SCHEDULED_LAST_MAX = 16

#: Per-field caps for the conversation rows.
CONVERSATION_TITLE_MAX = 80
CONVERSATION_MESSAGES_MAX = 100000

#: A heartbeat must never wait on the session database. WAL readers do not
#: queue behind the writer, so this only bounds a pathological case (a
#: checkpoint holding the file), and expiring it just omits the key.
CONVERSATION_QUERY_TIMEOUT_SECONDS = 2.0

#: Roots only, freshest first, and a titled conversation only.
#:
#: ``parent_session_id is null`` keeps subagent runs and compression
#: continuations from each appearing as their own conversation. The cost is
#: that a compressed chain reports its root's own counters rather than the live
#: tip's; that is the honest trade for a summary, and the session list on the
#: device remains the place that projects a chain forward.
#:
#: The title filter is a privacy gate, not tidiness: an untitled conversation
#: has no permitted name, and the fallback every local lister uses for one is a
#: preview of the first message.
#: Title provenances that may leave the machine.
#:
#: ``hermes_state`` records three. ``derived`` is the FIRST LINE OF THE PERSON'S
#: OWN MESSAGE, verbatim, so carrying it would put message content in a stored
#: heartbeat; that is the whole reason this filter exists. ``llm`` is a topic
#: summary the agent wrote, which is the same kind of thing a person already
#: sees in their own chat list. ``user`` is a title they typed themselves.
#:
#: A NULL source is excluded too, and deliberately. ``_title_rank`` treats NULL
#: as ``user`` for the different purpose of deciding what may be overwritten,
#: but its own docstring says such rows "were almost always set by the old
#: auto-titler". Ambiguous provenance is not a licence to publish, so the
#: benefit of the doubt goes the other way here.
_CONVERSATION_TITLE_SOURCES = ("user", "llm")

_CONVERSATIONS_QUERY = (
    "select title, message_count, last_activity_at, started_at from sessions "
    "where archived = 0 and hidden = 0 and parent_session_id is null "
    "and title is not null and trim(title) <> '' "
    "and title_source in (?, ?) "
    "order by coalesce(last_activity_at, started_at) desc limit ?"
)


def _text(value: Any) -> str:
    return str(value or "").strip()[:SERVER_TEXT_MAX]


def _clip(value: Any, limit: int) -> str:
    """Trim a text field to the width the wire keeps for that field."""
    return str(value or "").strip()[:limit]


def _hermes_home(home: Any = None) -> Path:
    """The profile home whose stores this beat describes.

    The caller passes its own home (the bridge already resolved one, and cron
    and sessions are per-profile stores). The env fallback is for callers that
    have none; ``hermes_constants.get_hermes_home`` is deliberately not
    imported here because it can write a profile warning to errors.log, and a
    heartbeat has no business touching the disk on the way to reading it.
    """
    if home:
        return Path(home)
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _int_or_none(value: Any) -> Optional[int]:
    """An integer from a stored value, or None when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_seconds(value: Any) -> Optional[int]:
    """Epoch SECONDS from a stored timestamp; None when it cannot be read.

    The cron store writes offset-aware ISO strings and the session store writes
    epoch floats, so both spellings are accepted. A naive ISO stamp (a hand
    edit, or an older record) is read as local time, which is the clock the
    scheduler itself compares against.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds >= 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        seconds = int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    return seconds if seconds >= 0 else None


def _schedule_words(display: Any, schedule: Any) -> str:
    """The schedule as words ("every 15m", "0 5 * * 0"), or "" when there are none.

    Only named keys are read out of the schedule mapping. Stringifying the
    mapping itself would ship whatever key the scheduler gains next, which is
    the failure this whole summary is built to avoid.
    """
    text = str(display or "").strip()
    if text:
        return text
    if isinstance(schedule, dict):
        for key in ("display", "expr", "value", "run_at"):
            text = str(schedule.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(schedule or "").strip()


#: How many characters of a content field the cron store uses when it invents a
#: name for a job that has none (``cron.jobs._normalize_job_record``).
_DERIVED_NAME_PREFIX = 50


def _name_is_derived_from_content(record: dict[str, Any], name: str) -> bool:
    """Whether this "name" is really the job's prompt or script path.

    The cron store fills an empty name with ``label_source[:50].strip()``,
    where the source is the prompt first and the script path second. Both are
    content that must never leave the machine, and once such a record is saved
    the derived name is what the file holds, so reading the raw file does not
    avoid it: by then the prompt IS the name field.

    So the name is checked against what it could have been derived from. The
    store's other two fallbacks, a skill name and the job id, are safe and are
    deliberately not treated as leaks; refusing those would drop legitimate
    rows for no gain.

    Compared against the clipped name rather than the raw one, because the
    caller has already truncated it to the wire limit.
    """
    for source_key in ("prompt", "script"):
        source = str(record.get(source_key) or "").strip()
        if not source:
            continue
        derived = source[:_DERIVED_NAME_PREFIX].strip()
        if not derived:
            continue
        # The name reaching here is already clipped to the wire cap, so compare
        # on the shorter of the two rather than requiring exact equality.
        cap = min(len(name), len(derived))
        if cap and name[:cap] == derived[:cap]:
            return True
    return False


def _job_is_paused(record: dict[str, Any]) -> bool:
    """Whether the scheduler will refuse to fire this job.

    Mirrors ``cron.jobs.is_job_runnable``: ``enabled`` is the flag the
    scheduler honours, and a pause marker is a second gate, so a half-paused
    record (enabled, with a ``paused_at``) reads as off here too. Deliberately
    NOT ``effective_job_state``, which is a display rule that calls such a
    record "scheduled": the owner is being told whether the work will run, and
    it will not.
    """
    if not record.get("enabled", True):
        return True
    if str(record.get("state") or "").strip() == "paused":
        return True
    return bool(record.get("paused_at"))


def _scheduled_order(row: dict[str, Any]) -> tuple[int, int, int]:
    """Soonest live job first; paused work sinks to the end.

    A paused job keeps the ``next_run_at`` it had when it was paused, which is
    in the past forever, so sorting on time alone would let stopped work take
    every slot and push the next real run off a truncated list.
    """
    next_at = row.get("next_at")
    return (
        1 if row.get("paused") else 0,
        1 if next_at is None else 0,
        int(next_at or 0),
    )


def scheduled_summary(*, home: Any = None, jobs_path: Any = None) -> list[dict[str, Any]]:
    """The scheduled work, in the five fields One's heartbeat accepts.

    Read straight off the cron store's JSON rather than through
    ``cron.jobs.load_jobs``: that path takes a cross-process lock, may rewrite
    the file to repair it, and pulls in the scheduler. None of that belongs
    under a heartbeat, whose whole job is to say "this machine is alive"
    without waiting on anything else on the machine.

    Every field is built from a NAMED source. The stored record also carries
    the job's prompt, script, workdir, model credentials and monitor URL, so
    copying a record and deleting the unwanted keys would ship whatever field
    is added upstream next. A row that cannot be built from the permitted
    fields alone is dropped, never filled in from something else.
    """
    path = Path(jobs_path) if jobs_path else _hermes_home(home) / "cron" / "jobs.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("jobs") if isinstance(payload, dict) else payload
    if isinstance(records, dict):
        # An id-keyed map is a hand edit or an external tool; the store itself
        # only ever writes a list. Read the values so such a file summarises
        # rather than silently reporting nothing scheduled.
        records = list(records.values())

    rows: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        name = _clip(record.get("name"), SCHEDULED_NAME_MAX)
        when = _clip(
            _schedule_words(record.get("schedule_display"), record.get("schedule")),
            SCHEDULED_WHEN_MAX,
        )
        if not name or not when or _name_is_derived_from_content(record, name):
            # Three ways a row is refused, and the third is the one that bites.
            #
            # A missing name or schedule is easy. The dangerous case is a name
            # that LOOKS present and is actually the job's prompt: the store's
            # own normaliser fills an empty name with the first 50 characters
            # of the prompt (or of the script path), and if that record is then
            # saved, the file itself holds the derived name. Reading the raw
            # file is not enough to escape it, because the leak arrives
            # already-filled. So the name is compared against the sources it
            # could have been derived from, and a match is dropped.
            continue
        row: dict[str, Any] = {"name": name, "when": when, "paused": _job_is_paused(record)}
        next_at = _epoch_seconds(record.get("next_run_at"))
        if next_at is not None:
            row["next_at"] = next_at
        last = _clip(record.get("last_status"), SCHEDULED_LAST_MAX)
        if last:
            # ``last_status`` is one of the scheduler's own words ("ok",
            # "error", "blocked_config"), never free text. Its sibling
            # ``last_error`` IS free text, often carrying the job's output, and
            # is never carried.
            row["last"] = last
        rows.append(row)

    rows.sort(key=_scheduled_order)
    return rows[:SUMMARY_MAX_ROWS]


def conversations_summary(
    *, home: Any = None, db_path: Any = None, limit: int = SUMMARY_MAX_ROWS
) -> list[dict[str, Any]]:
    """The freshest conversations, as title, message count and last active.

    Opened read-only against the session database rather than through
    ``SessionDB``: constructing one runs migrations and flushes queued token
    counts, so the cheap side of a heartbeat would be writing to the owner's
    database. ``mode=ro`` cannot write, cannot migrate, and cannot repair.

    Three columns are read and three are sent. No message row is touched, so
    there is no path from here to the content of a conversation.
    """
    path = Path(db_path) if db_path else _hermes_home(home) / "state.db"
    if not path.exists():
        return []

    # Imported here rather than at module scope: a machine with no session
    # database never pays for the extension module, and this file is imported
    # to build every beat.
    import sqlite3

    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=CONVERSATION_QUERY_TIMEOUT_SECONDS
    )
    try:
        cursor = connection.execute(
            _CONVERSATIONS_QUERY,
            (*_CONVERSATION_TITLE_SOURCES, max(0, min(int(limit), SUMMARY_MAX_ROWS))),
        )
        rows: list[dict[str, Any]] = []
        for title, message_count, last_activity_at, started_at in cursor:
            clean_title = _clip(title, CONVERSATION_TITLE_MAX)
            at = _epoch_seconds(
                last_activity_at if last_activity_at is not None else started_at
            )
            if not clean_title or at is None:
                # No permitted title, or no readable timestamp. Both fields are
                # required by the wire, so the row is dropped rather than
                # invented.
                continue
            messages = _int_or_none(message_count)
            rows.append(
                {
                    "title": clean_title,
                    "messages": max(0, min(messages or 0, CONVERSATION_MESSAGES_MAX)),
                    "at": at,
                }
            )
        return rows
    finally:
        connection.close()


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
    home: Any = None,
) -> dict[str, Any]:
    """Assemble what this machine reports about itself.

    Every field is on the server's heartbeat allow-list; anything else would be
    dropped there anyway. Hardware is included so the owner sees the machine
    their agent runs on, and it is names only -- brand and processor, never a
    serial number, hostname, or MAC, none of which the dashboard needs and all
    of which would identify the machine rather than describe it.

    ``home`` names the profile whose cron and session stores are summarised.
    Callers that own a profile home pass it; the fallback is the process's own.
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

    # Each summary gets its own guard, like the hardware reads above: a corrupt
    # jobs.json or a locked database omits that one key and the beat still
    # lands. Liveness is the point of the beat; the summaries are a bonus.
    #
    # An empty summary is omitted rather than sent as []. "The device did not
    # report" and "the device has nothing scheduled" are different answers, and
    # only the reader can tell them apart, and only if we keep them apart here.
    try:
        scheduled = scheduled_summary(home=home)
        if scheduled:
            snapshot["scheduled"] = scheduled
    except Exception:
        logger.debug("scheduled work unavailable", exc_info=True)

    try:
        conversations = conversations_summary(home=home)
        if conversations:
            snapshot["conversations"] = conversations
    except Exception:
        logger.debug("conversations unavailable", exc_info=True)

    return {key: value for key, value in snapshot.items() if value != ""}
