"""LM Studio model residency: what is resident, what it costs, what to evict.

Hermes can *load* an LM Studio model (``ensure_lmstudio_model_loaded`` in
:mod:`hermes_cli.models`) but until now had no way to give the memory back.
On a laptop or an edge box that is a one-way ratchet: the second large model
either fails to load or pushes the host into swap, and the first model — idle,
untouched for an hour — keeps its 15 GB.

This module supplies the missing half. It answers three questions and then
acts on them:

* what is resident right now, and how big is it (``loaded_models``)
* how much room does the host actually have (``host_memory``)
* which residents may be evicted to make room (``plan_eviction``)

:func:`plan_eviction` is pure and takes those three answers as arguments, so
the policy — the part that can lose someone their running session — is
unit-testable without a machine, a server, or a model.

Every I/O helper here degrades to an empty result rather than raising: a
residency probe that throws would take down the caller that merely wanted to
know whether a load would fit.

Sizes are **decimal** GB (bytes / 1e9) throughout. That is not arbitrary:
``lms ps`` prints ``15.64 GB`` for a model whose ``size_bytes`` is
15,641,333,028, i.e. it divides by 1e9, not 1024**3. Host memory is converted
the same way so the fit arithmetic compares like with like — mixing the two
conventions understates a 128 GiB host's headroom by ~7%.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Sequence

from hermes_cli import __version__ as _HERMES_VERSION
from hermes_cli._subprocess_compat import IS_WINDOWS, bounded_probe_run
from hermes_cli.urllib_security import open_credentialed_url

logger = logging.getLogger(__name__)

__all__ = [
    "list_models",
    "loaded_models",
    "parse_lms_ps",
    "host_memory",
    "plan_eviction",
    "unload_model",
    "ensure_capacity",
]

DEFAULT_SERVER_ROOT = "http://127.0.0.1:1234"

_USER_AGENT = f"hermes-cli/{_HERMES_VERSION}"
_BYTES_PER_GB = 1_000_000_000.0

# The only status `lms ps` reports for a model that is resident but not
# mid-request. Anything else (loading, generating, an unrecognized future
# state) is off limits: eviction mid-stream kills a live answer.
_IDLE_STATUS = "IDLE"

# Float slack for the fit comparisons. Sizes arrive as 2-decimal strings, so
# the only error to absorb is accumulated addition noise.
_FIT_EPSILON = 1e-9

# Above this many evictable models the exhaustive minimal-subset search stops
# being free (it is O(2**n)), and the plan falls back to LRU-first greedy.
# A host with more than a dozen resident models is already past the point
# where the difference between "fewest evictions" and "oldest first" matters.
_MAX_EXHAUSTIVE_CANDIDATES = 12

_SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$")

# Decimal and binary suffixes both appear in the wild across `lms` versions.
_SIZE_UNITS_GB = {
    "B": 1e-9,
    "KB": 1e-6,
    "MB": 1e-3,
    "GB": 1.0,
    "TB": 1000.0,
    "KIB": 1024 / 1e9,
    "MIB": 1024**2 / 1e9,
    "GIB": 1024**3 / 1e9,
    "TIB": 1024**4 / 1e9,
}

_FREE_PCT_PATTERN = re.compile(
    r"System-wide memory free percentage:\s*([0-9]+(?:\.[0-9]+)?)\s*%"
)


# ---------------------------------------------------------------------------
# LM Studio REST API
# ---------------------------------------------------------------------------


def _server_root(base_url: Optional[str] = None) -> Optional[str]:
    """Strip an API suffix off a base URL to reach LM Studio's server root.

    Same normalization as ``models._lmstudio_server_root``, widened to the
    other forms ``auth._normalize_lmstudio_runtime_base_url`` already accepts,
    because users paste whichever URL their client showed them. Falls back to
    ``LM_BASE_URL`` and then to the LM Studio default port.
    """
    raw = base_url or os.environ.get("LM_BASE_URL") or DEFAULT_SERVER_ROOT
    root = str(raw).strip().rstrip("/")
    for suffix in ("/api/v1", "/api/v0", "/api", "/v1", "/v0"):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
            break
    return root or None


def _request_headers(api_key: Optional[str] = None) -> dict:
    headers = {"User-Agent": _USER_AGENT}
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def list_models(
    base_url: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Return every model LM Studio knows about, resident or not.

    Reads ``/api/v0/models`` rather than the ``/api/v1/models`` catalog the
    rest of Hermes uses: v0 reports a flat ``state`` ("loaded"/"not-loaded")
    per model, which is the residency question this module asks. Returns
    ``[]`` on any failure — an unreachable server is indistinguishable from
    an empty one for capacity purposes, and neither is worth an exception.
    """
    root = _server_root(base_url)
    if not root:
        return []

    request = urllib.request.Request(
        root + "/api/v0/models", headers=_request_headers(api_key)
    )
    try:
        with open_credentialed_url(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("LM Studio model listing at %s failed: %s", root, exc)
        return []

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        logger.debug("LM Studio model listing at %s returned no `data` list", root)
        return []

    models: list[dict] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id") or "").strip()
        if not identifier:
            continue
        models.append(
            {
                "id": identifier,
                "state": str(raw.get("state") or "").strip(),
                "type": str(raw.get("type") or "").strip(),
                "max_context_length": _positive_int(raw.get("max_context_length")),
            }
        )
    return models


# ---------------------------------------------------------------------------
# Resident models (`lms ps`)
# ---------------------------------------------------------------------------


def _lms_binary() -> Optional[str]:
    """Locate the ``lms`` CLI, preferring LM Studio's own install location.

    LM Studio ships the binary at ``~/.lmstudio/bin/lms`` and only adds that
    directory to PATH if the user ran ``lms bootstrap``, so the explicit path
    is tried first and PATH is the fallback, not the other way around.
    """
    name = "lms.exe" if IS_WINDOWS else "lms"
    installed = Path.home() / ".lmstudio" / "bin" / name
    try:
        if installed.is_file() and os.access(installed, os.X_OK):
            return str(installed)
    except OSError:
        pass
    return shutil.which("lms")


def _column_spans(header: str) -> list[tuple[str, int, Optional[int]]]:
    """Derive (name, start, end) slices from a whitespace-aligned header row.

    Splitting the data rows on whitespace does not work: ``SIZE`` holds
    ``15.64 GB`` (one value, two tokens) and a trailing ``TTL`` is usually
    absent entirely, so token counts agree with the header by coincidence and
    disagree by one the moment a TTL is set. Slicing at the header's own
    column offsets is stable under both, because ``lms`` pads every column to
    the width of its widest cell.
    """
    tokens = [(m.group(0).upper(), m.start()) for m in re.finditer(r"\S+", header)]
    spans: list[tuple[str, int, Optional[int]]] = []
    for index, (name, start) in enumerate(tokens):
        end = tokens[index + 1][1] if index + 1 < len(tokens) else None
        spans.append((name, start, end))
    return spans


def _parse_size_gb(value: str) -> float:
    """Convert an ``lms ps`` SIZE cell ("15.64 GB", "532.00 MB") to GB."""
    match = _SIZE_PATTERN.match(str(value or "").strip())
    if not match:
        return 0.0
    factor = _SIZE_UNITS_GB.get(match.group(2).upper())
    if factor is None:
        return 0.0
    try:
        return round(float(match.group(1)) * factor, 4)
    except ValueError:
        return 0.0


def parse_lms_ps(output: str) -> list[dict]:
    """Parse ``lms ps`` table output into residency records.

    Pure and total: unrecognized output yields ``[]`` rather than a partial
    list of guesses, because a wrong size here becomes a wrong eviction.
    """
    lines = str(output or "").splitlines()
    header_index = None
    for index, line in enumerate(lines):
        upper = line.upper()
        if "IDENTIFIER" in upper and "STATUS" in upper:
            header_index = index
            break
    if header_index is None:
        return []

    spans = _column_spans(lines[header_index])
    records: list[dict] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        if not line.strip(" -=\t"):
            continue
        cells = {name: line[start:end].strip() for name, start, end in spans}
        identifier = cells.get("IDENTIFIER", "")
        if not identifier:
            continue
        records.append(
            {
                "identifier": identifier,
                "model": cells.get("MODEL", "") or identifier,
                "status": cells.get("STATUS", ""),
                "size_gb": _parse_size_gb(cells.get("SIZE", "")),
                "context": _parse_int(cells.get("CONTEXT", "")),
                "ttl": cells.get("TTL", ""),
            }
        )
    return records


def _parse_int(value: str) -> int:
    try:
        return int(str(value or "").strip().replace(",", ""))
    except ValueError:
        return 0


def loaded_models(*, timeout: float = 10.0) -> list[dict]:
    """Return the models currently resident in LM Studio.

    ``lms ps`` is the only source that reports both a per-instance status and
    a memory footprint; the REST catalog reports neither. Returns ``[]`` when
    the CLI is absent, times out, or exits non-zero.
    """
    binary = _lms_binary()
    if not binary:
        logger.debug("lms CLI not found; cannot enumerate resident models")
        return []
    completed = bounded_probe_run([binary, "ps"], timeout=timeout)
    if completed is None or completed.returncode != 0:
        logger.debug("`lms ps` did not complete successfully")
        return []
    return parse_lms_ps(completed.stdout or "")


# ---------------------------------------------------------------------------
# Host memory
# ---------------------------------------------------------------------------


def _psutil_memory() -> dict:
    try:
        import psutil  # type: ignore
    except Exception:
        return {}
    try:
        virtual = psutil.virtual_memory()
        total = float(virtual.total)
        available = float(virtual.available)
    except Exception:
        return {}
    if total <= 0:
        return {}
    return {
        "total_gb": round(total / _BYTES_PER_GB, 2),
        "available_gb": round(available / _BYTES_PER_GB, 2),
        "free_pct": round(available / total * 100.0, 1),
    }


def _sysctl_total_bytes() -> Optional[int]:
    completed = bounded_probe_run(["sysctl", "-n", "hw.memsize"], timeout=5.0)
    if completed is None or completed.returncode != 0:
        return None
    try:
        total = int((completed.stdout or "").strip())
    except ValueError:
        return None
    return total if total > 0 else None


def _memory_pressure_free_pct() -> Optional[float]:
    completed = bounded_probe_run(["memory_pressure"], timeout=15.0)
    if completed is None or completed.returncode != 0:
        return None
    match = _FREE_PCT_PATTERN.search(completed.stdout or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _darwin_memory() -> dict:
    """macOS memory without psutil: ``sysctl`` for the total, ``memory_pressure``
    for what is actually reclaimable.

    All-or-nothing on purpose. A total with no free reading cannot answer "does
    this model fit", and half a sample invites a caller to treat a missing key
    as zero and evict the whole host.
    """
    total_bytes = _sysctl_total_bytes()
    if total_bytes is None:
        return {}
    free_pct = _memory_pressure_free_pct()
    if free_pct is None:
        return {}
    total_gb = total_bytes / _BYTES_PER_GB
    return {
        "total_gb": round(total_gb, 2),
        "available_gb": round(total_gb * free_pct / 100.0, 2),
        "free_pct": free_pct,
    }


def host_memory() -> dict:
    """Return ``{"total_gb", "available_gb", "free_pct"}`` for this host.

    psutil when it is importable, macOS system tools otherwise. ``{}`` when
    neither works — see :func:`ensure_capacity` for why that is treated as
    "do not evict".

    The two sources disagree, and psutil is preferred because it disagrees in
    the safe direction: on the 128 GB test host psutil reported 70 GB
    available where ``memory_pressure`` reported 88% free (~120 GB), because
    psutil's ``available`` excludes memory the kernel would have to reclaim
    before handing it over. Loading a model against the optimistic figure is
    how a host ends up in swap.
    """
    sample = _psutil_memory()
    if sample:
        return sample
    if sys.platform == "darwin":
        return _darwin_memory()
    return {}


# ---------------------------------------------------------------------------
# Eviction policy (pure)
# ---------------------------------------------------------------------------


def plan_eviction(
    *,
    need_gb: float,
    loaded: Sequence[dict],
    available_gb: float,
    protect: Sequence[str] = (),
) -> list[str]:
    """Choose which resident models to unload to free ``need_gb``.

    Shares the shape of ``gateway.agent_cache_pressure.plan_pressure_evictions``
    — LRU-first ordering plus a protect set, no I/O, caller applies the plan —
    but the unit is a model, not a transcript, so the budget is memory rather
    than a count. ``loaded`` is taken in LRU→MRU order (``lms ps`` lists
    oldest instance first).

    Only ``IDLE`` entries are candidates and never one in ``protect``: the
    model backing the active session must survive making room for its
    successor, or the eviction takes the conversation with it.

    Returns the *fewest* models that cover the shortfall, tie-broken toward
    the least recently used, so a 4 GB gap does not cost a 40 GB resident when
    a 5 GB one would do. Returns ``[]`` when the request already fits, and the
    entire evictable set when even shedding all of it falls short — the caller
    decides whether a partial reclaim is worth taking.
    """
    try:
        deficit = float(need_gb) - float(available_gb)
    except (TypeError, ValueError):
        return []
    if deficit <= _FIT_EPSILON:
        return []

    # Case-insensitive, matching how the IDLE status is compared below. A
    # protect list is a safety instruction, so a caller who writes the model
    # name in a different case than LM Studio reports must still be obeyed --
    # the failure mode is unloading the model serving the active session.
    protected = {
        str(name).strip().casefold() for name in (protect or ()) if str(name).strip()
    }
    candidates: list[tuple[str, float]] = []
    for entry in loaded or ():
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("identifier") or "").strip()
        if not identifier or identifier.casefold() in protected:
            continue
        if str(entry.get("status") or "").strip().upper() != _IDLE_STATUS:
            continue
        size = entry.get("size_gb")
        # An unparsed size stays a candidate at 0 GB: it can never be picked to
        # close a gap it is not known to close, but it is still fair game when
        # the host is over budget and everything evictable has to go.
        usable = (
            float(size)
            if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0
            else 0.0
        )
        candidates.append((identifier, usable))

    if not candidates:
        return []

    identifiers = [identifier for identifier, _ in candidates]
    if sum(size for _, size in candidates) + _FIT_EPSILON < deficit:
        return identifiers

    if len(candidates) > _MAX_EXHAUSTIVE_CANDIDATES:
        return _greedy_plan(candidates, deficit)

    for count in range(1, len(candidates) + 1):
        # Fewest models first, then least memory surrendered, then LRU order.
        # Cardinality alone is not enough: with a 4 GB gap and both a 5 GB and
        # a 40 GB idle resident, every single-model combo "fits", and taking
        # the first one in LRU order can throw away 40 GB of warm weights to
        # free 4. Ranking by what is actually given up keeps the promise the
        # docstring makes.
        best: tuple[float, int, list[str]] | None = None
        for combo in itertools.combinations(range(len(candidates)), count):
            covered = sum(candidates[index][1] for index in combo)
            if covered + _FIT_EPSILON < deficit:
                continue
            ranked = (covered, combo[0], [candidates[index][0] for index in combo])
            if best is None or (ranked[0], ranked[1]) < (best[0], best[1]):
                best = ranked
        if best is not None:
            return best[2]
    return identifiers


def _greedy_plan(candidates: Sequence[tuple[str, float]], deficit: float) -> list[str]:
    """LRU-first accumulation, used once the exhaustive search stops being free."""
    plan: list[str] = []
    freed = 0.0
    for identifier, size in candidates:
        plan.append(identifier)
        freed += size
        if freed + _FIT_EPSILON >= deficit:
            break
    return plan


# ---------------------------------------------------------------------------
# Unloading
# ---------------------------------------------------------------------------


def _rest_unload(
    identifier: str,
    *,
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: float,
) -> Optional[bool]:
    """Unload via ``POST /api/v1/models/unload``.

    Probed live against LM Studio 0.3.x: the route takes ``{"instance_id":
    ...}`` (omitting it answers 400 ``missing_required_parameter``) and
    answers 404 ``{"error": {"type": "model_not_found"}}`` for an instance
    that is not resident. The instance id is the same string ``lms ps`` prints
    in its IDENTIFIER column.

    Returns ``True`` on success, ``False`` when the server refused
    authoritatively, and ``None`` when the route is not there — an older build
    answers a plain-string ``{"error": "Unexpected endpoint or method..."}``,
    which is the one case worth paying for the CLI fallback.
    """
    root = _server_root(base_url)
    if not root:
        return None

    headers = _request_headers(api_key)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        root + "/api/v1/models/unload",
        data=json.dumps({"instance_id": identifier}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with open_credentialed_url(request, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except urllib.error.HTTPError as exc:
        if _is_unknown_route(exc):
            logger.debug("LM Studio at %s has no REST unload route", root)
            return None
        logger.debug("LM Studio refused unload of %s: HTTP %s", identifier, exc.code)
        return False
    except Exception as exc:
        logger.debug("LM Studio unload request for %s failed: %s", identifier, exc)
        return None


def _is_unknown_route(error: urllib.error.HTTPError) -> bool:
    if error.code != 404:
        return False
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return False
    # A typed error object means the route ran and rejected the request; only
    # the router's own bare-string 404 means the route is missing.
    return isinstance(payload, dict) and isinstance(payload.get("error"), str)


def _cli_unload(identifier: str, *, timeout: float) -> bool:
    binary = _lms_binary()
    if not binary:
        return False
    completed = bounded_probe_run([binary, "unload", identifier], timeout=timeout)
    return completed is not None and completed.returncode == 0


def unload_model(
    identifier: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 60.0,
) -> bool:
    """Evict one resident model. ``True`` only when it actually came out.

    REST first, ``lms unload`` only when the server has no unload route.
    Never raises: a failed eviction has to leave the caller free to decide
    whether to load anyway, not blow up the load path.
    """
    name = str(identifier or "").strip()
    if not name:
        return False
    result = _rest_unload(name, base_url=base_url, api_key=api_key, timeout=timeout)
    if result is not None:
        return result
    return _cli_unload(name, timeout=timeout)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def ensure_capacity(
    *,
    need_gb: float,
    protect: Sequence[str] = (),
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Make room for a ``need_gb`` model, evicting idle residents if required.

    Returns ``{"evicted": [...], "fit": bool, "available_gb": float}``.
    ``fit`` is the state of the host *after* the evictions, so a caller can
    load on ``True`` and decide for itself on ``False``.
    """
    memory = host_memory()
    available = memory.get("available_gb")
    if not isinstance(available, (int, float)) or isinstance(available, bool):
        # No memory reading means no idea how much is free. Evicting on that
        # basis would trade a real running model for a guess, so the honest
        # answer is "did nothing, cannot promise it fits".
        logger.debug("host memory unreadable; declining to plan an eviction")
        return {"evicted": [], "fit": False, "available_gb": 0.0}

    available = float(available)
    resident = loaded_models()
    plan = plan_eviction(
        need_gb=need_gb,
        loaded=resident,
        available_gb=available,
        protect=protect,
    )

    sizes = {
        str(entry.get("identifier") or ""): entry.get("size_gb") or 0.0
        for entry in resident
        if isinstance(entry, dict)
    }

    # plan_eviction returns the whole evictable set when even shedding all of it
    # cannot close the gap, so a caller can see the ceiling. Acting on that plan
    # would unload every warm model and STILL not fit -- the worst outcome
    # available, paid for nothing. Execute a plan only when it actually achieves
    # the fit; otherwise report the ceiling and leave the host untouched.
    planned_relief = sum(float(sizes.get(identifier) or 0.0) for identifier in plan)
    if plan and available + planned_relief + _FIT_EPSILON < float(need_gb):
        logger.info(
            "declining to evict for %.2f GB: unloading all %d resident model(s) "
            "would still leave the host short, so nothing was unloaded",
            float(need_gb),
            len(plan),
        )
        return {"evicted": [], "fit": False, "available_gb": round(available, 2)}

    evicted: list[str] = []
    projected = 0.0
    for identifier in plan:
        if unload_model(identifier, base_url=base_url, api_key=api_key):
            evicted.append(identifier)
            projected += float(sizes.get(identifier) or 0.0)

    if evicted:
        # Re-measure rather than trust the projection: another process may
        # have taken the space we just freed. The projection is only the
        # fallback for when the second read fails.
        refreshed = host_memory().get("available_gb")
        if isinstance(refreshed, (int, float)) and not isinstance(refreshed, bool):
            available = float(refreshed)
        else:
            available += projected

    try:
        fit = available + _FIT_EPSILON >= float(need_gb)
    except (TypeError, ValueError):
        fit = False
    return {"evicted": evicted, "fit": fit, "available_gb": round(available, 2)}
