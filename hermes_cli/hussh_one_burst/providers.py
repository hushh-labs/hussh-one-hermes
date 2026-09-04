# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The provider seam — one cloud behind an interface, so a second is a drop-in.

Carried over from the husshone design, where ``ComputeBurstProvider`` existed for
the same reason: the burst path must not learn the shape of one vendor's API.

Two implementations ship:

* :class:`MockBurstProvider` — needs no credential and no network, which is what
  makes the whole execution path testable. Every test in this package runs
  against it.
* :class:`GcpBurstProvider` — provisions a real Compute Engine instance in the
  person's own project.

**Teardown is part of the interface, not an afterthought.** ``teardown`` must be
idempotent and must treat "already gone" as success, because the one thing worse
than a failed burst is an accelerator nobody remembers to switch off.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from .credentials import CredentialRef, resolve_credentials, resolve_region

_MOCK_IDS = itertools.count(1)


@dataclass(frozen=True)
class InstanceSpec:
    """What to provision.  Contains no workload information by construction."""

    accelerator_id: str
    chip_count: int
    label: str
    deadline_minutes: float


@dataclass
class InstanceHandle:
    """A provisioned instance.  ``torn_down`` flips once teardown succeeds."""

    id: str
    destination: str
    torn_down: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BurstProvider(Protocol):
    """The contract every cloud backend implements."""

    def describe_destination(self) -> str:
        """Human-readable destination, shown before the person approves."""
        ...

    def provision(self, spec: InstanceSpec) -> InstanceHandle: ...

    def teardown(self, handle: InstanceHandle) -> bool:
        """Release the instance.  Returns True when it is gone.

        Must be idempotent, and must return True — not raise — when the instance
        no longer exists.
        """
        ...


class MockBurstProvider:
    """An in-memory provider that needs no credentials and touches no network.

    This exists so the execution path — including every teardown guarantee — can
    be exercised in CI without a cloud account or a cent of spend.
    """

    def __init__(self, *, fail_on_provision: bool = False, fail_on_teardown: bool = False) -> None:
        self.fail_on_provision = fail_on_provision
        self.fail_on_teardown = fail_on_teardown
        self.provisioned: list[InstanceHandle] = []
        self.teardown_calls: list[str] = []

    def describe_destination(self) -> str:
        return "a simulated project (mock provider — nothing is really provisioned)"

    def provision(self, spec: InstanceSpec) -> InstanceHandle:
        if self.fail_on_provision:
            raise RuntimeError("mock provisioning failure")
        handle = InstanceHandle(
            id=f"mock-instance-{next(_MOCK_IDS)}",
            destination="mock://local",
            detail={"accelerator": spec.accelerator_id, "chips": spec.chip_count},
        )
        self.provisioned.append(handle)
        return handle

    def teardown(self, handle: InstanceHandle) -> bool:
        self.teardown_calls.append(handle.id)
        if self.fail_on_teardown:
            raise RuntimeError("mock teardown failure")
        handle.torn_down = True
        return True

    @property
    def live_instances(self) -> list[InstanceHandle]:
        """Anything provisioned and not yet released — must be empty after a run."""
        return [h for h in self.provisioned if not h.torn_down]


#: How each catalog accelerator maps onto a real Compute Engine shape.
#:
#: ``machine`` is a format string over the chip count where the accelerator is
#: baked into the machine type (the A2/A3/A4 families), otherwise a fixed type
#: with ``accelerator`` attached separately via ``guestAccelerators``.
#: The valid chip counts are NOT repeated here — they live on
#: ``AcceleratorClass.sellable_chips`` in the catalog, so the price a person
#: approves and the shape that gets provisioned come from one source. Two lists
#: would drift, and the drift would be a wrong bill.
_GCP_SHAPES: dict[str, dict[str, Any]] = {
    "nvidia-t4": {"machine": "n1-standard-8", "accelerator": "nvidia-tesla-t4"},
    "nvidia-l4": {"machine": "g2-standard-{lanes}", "lanes": {1: 8, 2: 24, 4: 48, 8: 96}},
    "a100-40": {"machine": "a2-highgpu-{n}g"},
    "a100-80": {"machine": "a2-ultragpu-{n}g"},
    "h100-80": {"machine": "a3-highgpu-8g"},
    "h200-141": {"machine": "a3-ultragpu-8g"},
    "b200-180": {"machine": "a4-highgpu-8g"},
    "gb200-186": {"machine": "a4x-highgpu-4g"},
}

#: A GPU image, not a bare OS — a plain Debian instance has no CUDA driver and a
#: burst that boots without one has burned money to do nothing.
_DEFAULT_IMAGE = (
    "projects/deeplearning-platform-release/global/images/family/common-cu123-debian-11"
)


class UnsupportedAccelerator(ValueError):
    """The requested accelerator cannot be provisioned by this backend."""


class GcpBurstProvider:
    """Provisions a Compute Engine instance in the person's own project.

    The credential is resolved per call and never stored on this object; only
    :class:`~.credentials.CredentialRef` is retained, which is safe to put in a
    receipt.

    **Never executed against real GCP.** The request body below is built from
    the documented ``instances.insert`` contract and is typed and unit-tested,
    but no burst has been provisioned with it. Treat the first real run as the
    test, and expect to fix something.
    """

    _API_ROOT = "https://compute.googleapis.com/compute/v1"

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        region: Optional[str] = None,
        sa_key: Optional[str] = None,
        image: str = _DEFAULT_IMAGE,
        boot_disk_gb: int = 200,
    ) -> None:
        self._sa_key = sa_key
        self._project = project
        self._region = resolve_region(region)
        self._image = image
        self._boot_disk_gb = boot_disk_gb
        self._ref: Optional[CredentialRef] = None

    @property
    def credential_ref(self) -> Optional[CredentialRef]:
        return self._ref

    def _authed_session(self):
        creds, ref = resolve_credentials(
            sa_key=self._sa_key, project=self._project, region=self._region
        )
        # Retained deliberately: project/region/source only, never the key.
        self._ref = ref
        try:
            from google.auth.transport.requests import (  # type: ignore[import-not-found]
                AuthorizedSession,
            )
        except ImportError as exc:  # pragma: no cover - optional cloud extra
            raise RuntimeError("Cloud bursting needs google-auth's requests transport.") from exc
        return AuthorizedSession(creds), ref

    def describe_destination(self) -> str:
        project = self._project or "your connected project"
        return f"{project} ({self._region}) — your own cloud, billed to you"

    @staticmethod
    def resolve_shape(accelerator_id: str, chip_count: int) -> tuple[str, Optional[str], int]:
        """Map a catalog accelerator onto ``(machine_type, accelerator_type, chips)``.

        Raises :class:`UnsupportedAccelerator` rather than guessing. A TPU is not
        a Compute Engine instance at all — it lives behind ``tpu.googleapis.com``
        with its own node/queued-resource model — so asking this backend for one
        must fail loudly instead of quietly booting a GPU-less VM that bills by
        the hour while doing nothing.
        """
        if accelerator_id.startswith("tpu-"):
            raise UnsupportedAccelerator(
                f"{accelerator_id} is a Cloud TPU. TPUs are provisioned through the Cloud "
                "TPU API, not Compute Engine, and this backend does not implement it yet."
            )
        shape = _GCP_SHAPES.get(accelerator_id)
        if shape is None:
            raise UnsupportedAccelerator(
                f"No Compute Engine shape is mapped for '{accelerator_id}'."
            )
        from .hardware import ACCEL_CATALOG

        accel = next((c for c in ACCEL_CATALOG if c.id == accelerator_id), None)
        if accel is None:
            raise UnsupportedAccelerator(f"'{accelerator_id}' is not in the catalog.")
        allowed = accel.sellable_chips
        if chip_count not in allowed:
            raise UnsupportedAccelerator(
                f"{accelerator_id} is sold in {allowed} chip counts; {chip_count} is not one. "
                "Round up to the next valid count rather than under-provisioning."
            )
        template = shape["machine"]
        if "{lanes}" in template:
            machine = template.format(lanes=shape["lanes"][chip_count])
        else:
            machine = template.format(n=chip_count)
        return machine, shape.get("accelerator"), chip_count

    def provision(self, spec: InstanceSpec) -> InstanceHandle:
        machine, accelerator_type, chips = self.resolve_shape(
            spec.accelerator_id, spec.chip_count
        )
        session, ref = self._authed_session()
        zone = f"{ref.region}-a"
        # A short random suffix: two bursts of the same shape would otherwise
        # collide on name and the second would fail with 409 ALREADY_EXISTS.
        name = f"hussh-burst-{spec.accelerator_id}-{uuid.uuid4().hex[:8]}".lower()[:62]
        url = f"{self._API_ROOT}/projects/{ref.project}/zones/{zone}/instances"
        body: dict[str, Any] = {
            "name": name,
            "machineType": f"zones/{zone}/machineTypes/{machine}",
            "labels": {"app": "hussh-one-burst", "managed-by": "hermes"},
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {
                        "sourceImage": self._image,
                        "diskSizeGb": str(self._boot_disk_gb),
                    },
                }
            ],
            "networkInterfaces": [{"network": "global/networks/default"}],
            "scheduling": {
                # Cheaper, and it self-terminates — a second brake behind teardown.
                "provisioningModel": "SPOT",
                "instanceTerminationAction": "DELETE",
                "maxRunDuration": {"seconds": str(int(spec.deadline_minutes * 60))},
                # Both are required for accelerator instances, and SPOT forbids
                # automatic restart.
                "onHostMaintenance": "TERMINATE",
                "automaticRestart": False,
            },
        }
        if accelerator_type is not None:
            body["guestAccelerators"] = [
                {
                    "acceleratorType": f"zones/{zone}/acceleratorTypes/{accelerator_type}",
                    "acceleratorCount": chips,
                }
            ]
        response = session.post(url, json=body, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"Could not provision the burst instance: {response.text[:200]}")
        return InstanceHandle(
            id=name,
            destination=f"{ref.project}/{zone}",
            detail={
                "accelerator": spec.accelerator_id,
                "chips": chips,
                "machine_type": machine,
                "zone": zone,
            },
        )

    def teardown(self, handle: InstanceHandle) -> bool:
        session, ref = self._authed_session()
        zone = handle.detail.get("zone") or f"{ref.region}-a"
        url = f"{self._API_ROOT}/projects/{ref.project}/zones/{zone}/instances/{handle.id}"
        response = session.delete(url, timeout=60)
        # 404 means someone or something already released it. That is success:
        # the invariant is "not running", not "deleted by us".
        if response.status_code == 404 or response.status_code < 400:
            handle.torn_down = True
            return True
        raise RuntimeError(f"Could not tear down {handle.id}: {response.text[:200]}")


def resolve_provider(
    provider_id: str = "mock",
    *,
    project: Optional[str] = None,
    region: Optional[str] = None,
    sa_key: Optional[str] = None,
) -> BurstProvider:
    """Pick a backend by id.  Defaults to ``mock`` — real spend is opt-in."""
    normalized = (provider_id or "mock").strip().lower()
    if normalized in {"mock", "simulated", "none"}:
        return MockBurstProvider()
    if normalized in {"gcp", "google", "cloud"}:
        return GcpBurstProvider(project=project, region=region, sa_key=sa_key)
    raise ValueError(f"Unknown burst provider '{provider_id}'. Known: mock, gcp.")
