# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The burst lifecycle, and the teardown guarantee.

One rule governs this module: **an instance that was provisioned is always
released.** Success, failure, deadline, or an exception nobody predicted — the
release runs. An orphaned accelerator bills by the hour, and the first real
burst is the most expensive possible moment to discover that teardown was
best-effort.

Scope, stated plainly: this implements *provision → execute → release* with a
receipt. The ``execute`` step is a declared seam that currently performs no
payload transfer — shipping a person's workload to a remote machine is the one
step that genuinely moves their information off the device, and it needs its own
consent design rather than being folded in here. :func:`run_burst` is therefore
a complete and honest lifecycle, not a complete product.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .credentials import CredentialRef
from .providers import BurstProvider, InstanceHandle, InstanceSpec


@dataclass(frozen=True)
class BurstRequest:
    """What the person approved.  Carries no workload contents by construction."""

    label: str
    accelerator_id: str
    chip_count: int
    usd_per_hour: float
    deadline_minutes: float = 60.0


@dataclass
class BurstReceipt:
    """The durable record of one burst.  Safe to persist and to show.

    Contains no credential material — only a :class:`CredentialRef`, which names
    the project and how the credential was found, never the credential itself.
    """

    label: str
    status: str
    """``completed``, ``failed``, ``deadline_exceeded``, or ``provision_failed``."""

    accelerator_id: str
    chip_count: int
    elapsed_seconds: float
    estimated_cost_usd: float
    instance_id: Optional[str] = None
    destination: Optional[str] = None
    torn_down: bool = False
    teardown_error: Optional[str] = None
    error: Optional[str] = None
    credential: Optional[CredentialRef] = None
    events: list[str] = field(default_factory=list)

    @property
    def leaked_instance(self) -> bool:
        """True when something was provisioned and could not be released.

        This is the condition that costs money. It is a property rather than a
        log line so a caller can assert on it.
        """
        return self.instance_id is not None and not self.torn_down

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workload": self.label,
            "status": self.status,
            "success": self.status == "completed",
            "accelerator": self.accelerator_id,
            "chip_count": self.chip_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "instance_id": self.instance_id,
            "destination": self.destination,
            "torn_down": self.torn_down,
            "events": list(self.events),
        }
        if self.credential is not None:
            payload.update(self.credential.as_dict())
        if self.error:
            payload["error"] = self.error
        if self.teardown_error:
            payload["teardown_error"] = self.teardown_error
        if self.leaked_instance:
            payload["warning"] = (
                f"Instance {self.instance_id} may still be running and billing. "
                "Check your cloud console."
            )
        return payload


def run_burst(
    request: BurstRequest,
    provider: BurstProvider,
    execute: Optional[Callable[[InstanceHandle], None]] = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> BurstReceipt:
    """Provision, run, and always release.

    ``execute`` is the payload seam. When omitted the instance is provisioned and
    immediately released, which is exactly what the lifecycle tests exercise.

    Teardown runs in a ``finally``, so it survives an exception in ``execute``,
    a deadline overrun, and a ``KeyboardInterrupt``.
    """
    started = clock()
    receipt = BurstReceipt(
        label=request.label,
        status="failed",
        accelerator_id=request.accelerator_id,
        chip_count=request.chip_count,
        elapsed_seconds=0.0,
        estimated_cost_usd=0.0,
        credential=getattr(provider, "credential_ref", None),
    )
    spec = InstanceSpec(
        accelerator_id=request.accelerator_id,
        chip_count=request.chip_count,
        label=request.label,
        deadline_minutes=request.deadline_minutes,
    )

    handle: Optional[InstanceHandle] = None
    try:
        try:
            handle = provider.provision(spec)
        except Exception as exc:
            receipt.status = "provision_failed"
            receipt.error = str(exc)
            receipt.events.append("provision failed — nothing to release")
            return receipt

        receipt.instance_id = handle.id
        receipt.destination = handle.destination
        receipt.events.append(f"provisioned {handle.id}")

        deadline_s = request.deadline_minutes * 60.0
        if execute is not None:
            execute(handle)
            receipt.events.append("workload finished")
        else:
            receipt.events.append("no payload seam supplied — lifecycle only")

        if clock() - started > deadline_s:
            receipt.status = "deadline_exceeded"
            receipt.error = f"Exceeded its {request.deadline_minutes:g} minute deadline."
            receipt.events.append("deadline exceeded")
        else:
            receipt.status = "completed"
    except BaseException as exc:  # noqa: BLE001 - receipt is finalized in `finally`
        receipt.status = "failed"
        receipt.error = str(exc) or exc.__class__.__name__
        receipt.events.append(f"failed: {exc.__class__.__name__}")
        # KeyboardInterrupt and SystemExit must still reach the caller — but only
        # after `finally` below has released the instance.
        if not isinstance(exc, Exception):
            raise
    finally:
        if receipt.credential is None:
            receipt.credential = getattr(provider, "credential_ref", None)
        _release(provider, handle, receipt, started, request, clock)

    return receipt


def _release(
    provider: BurstProvider,
    handle: Optional[InstanceHandle],
    receipt: BurstReceipt,
    started: float,
    request: BurstRequest,
    clock: Callable[[], float],
) -> None:
    """Release the instance and finalize the receipt.  Never raises."""
    if handle is not None and not receipt.torn_down:
        try:
            receipt.torn_down = bool(provider.teardown(handle))
            receipt.events.append(f"released {handle.id}")
        except Exception as exc:
            receipt.torn_down = False
            receipt.teardown_error = str(exc)
            receipt.events.append(f"TEARDOWN FAILED for {handle.id} — may still be billing")
    receipt.elapsed_seconds = max(0.0, clock() - started)
    receipt.estimated_cost_usd = request.usd_per_hour * (receipt.elapsed_seconds / 3600.0)
