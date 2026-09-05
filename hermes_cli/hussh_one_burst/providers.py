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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

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
class SupportsPreflight(Protocol):
    """Optional: a backend that can ask the person's project before it spends.

    Deliberately *not* part of :class:`BurstProvider`. A mock has no project to
    ask, and requiring the method would make the credential-free path implement
    a network call it cannot make. But it must be declared somewhere, because
    the alternative — the ``getattr(backend, "preflight", None)`` this replaces —
    is an interface nobody implementing a second cloud can discover. A provider
    written faithfully against the base Protocol would silently skip the check
    and let a person approve hardware their project cannot get, which is the
    exact failure ``preflight`` exists to prevent.
    """

    def preflight(self, accelerator_id: str, chip_count: int) -> "Preflight": ...


@runtime_checkable
class BurstProvider(Protocol):
    """The contract every cloud backend implements.

    A backend may additionally implement :class:`SupportsPreflight`, which the
    burst tools use to refuse an order the person's own project cannot fill
    before asking them to approve the spend.
    """

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
#:
#: ``zone_accelerator`` is the name the part goes by in a zone's
#: ``acceleratorTypes`` listing — needed even for the A2/A3/A4 families, where
#: the accelerator is baked into the machine type and is therefore *not* sent in
#: ``guestAccelerators``. It is what makes "does this zone even carry the part?"
#: answerable. Every one of the eight was read off the live aggregated listing
#: rather than recalled.
#:
#: ``quota_metric`` is the Compute v1 regional quota that governs a spot burst of
#: this part, where one exists. For H100 and newer, **Compute v1 publishes no
#: metric at all** — those quotas moved to the Cloud Quotas API — so the value is
#: ``None`` and the pre-flight says "cannot tell" rather than inventing a zero.
#: Verified across five regions on 2026-09-04: not one lists an H100, H200, B200
#: or GB200 metric.
_GCP_SHAPES: dict[str, dict[str, Any]] = {
    "nvidia-t4": {
        "machine": "n1-standard-8",
        "accelerator": "nvidia-tesla-t4",
        "zone_accelerator": "nvidia-tesla-t4",
        "quota_metric": "PREEMPTIBLE_NVIDIA_T4_GPUS",
    },
    "nvidia-l4": {
        "machine": "g2-standard-{lanes}",
        "lanes": {1: 8, 2: 24, 4: 48, 8: 96},
        "zone_accelerator": "nvidia-l4",
        "quota_metric": "PREEMPTIBLE_NVIDIA_L4_GPUS",
    },
    "a100-40": {
        "machine": "a2-highgpu-{n}g",
        "zone_accelerator": "nvidia-tesla-a100",
        "quota_metric": "PREEMPTIBLE_NVIDIA_A100_GPUS",
    },
    "a100-80": {
        "machine": "a2-ultragpu-{n}g",
        "zone_accelerator": "nvidia-a100-80gb",
        "quota_metric": "PREEMPTIBLE_NVIDIA_A100_80GB_GPUS",
    },
    "h100-80": {"machine": "a3-highgpu-8g", "zone_accelerator": "nvidia-h100-80gb"},
    "h200-141": {"machine": "a3-ultragpu-8g", "zone_accelerator": "nvidia-h200-141gb"},
    "b200-180": {"machine": "a4-highgpu-8g", "zone_accelerator": "nvidia-b200"},
    "gb200-186": {"machine": "a4x-highgpu-4g", "zone_accelerator": "nvidia-gb200"},
}


@dataclass(frozen=True)
class Preflight:
    """Whether a burst can actually be provisioned, asked *before* the money.

    Split deliberately into two lists. ``blockers`` are refusals backed by
    positive evidence — the zone's own catalogue does not carry the part, or a
    published quota is smaller than the order. ``warnings`` are the things that
    could not be established either way; they belong in front of the person
    making the decision, not in a refusal, because refusing on an absent reading
    is just a confident guess pointed the other way.
    """

    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

#: A GPU image, not a bare OS — a plain Debian instance has no CUDA driver and a
#: burst that boots without one has burned money to do nothing.
#:
#: **This family rots.** Google retires Deep Learning VM families as CUDA versions
#: age, and a retired family is a 404 at provision time — the first value written
#: here (``common-cu123-debian-11``) was already gone when it was first checked
#: against the live API. Verify before relying on it:
#:
#:     GET compute/v1/projects/deeplearning-platform-release/global/images/family/<family>
#:
#: Verified present 2026-08-08. Override per call with ``image=`` rather than
#: editing this when a specific CUDA version is needed.
_DEFAULT_IMAGE = (
    "projects/deeplearning-platform-release/global/images/family/"
    "common-cu129-ubuntu-2204-nvidia-580"
)


class UnsupportedAccelerator(ValueError):
    """The requested accelerator cannot be provisioned by this backend."""


class OrphanedInstance(RuntimeError):
    """Provisioning failed *after* the request may already have created a machine.

    A POST that times out or has its connection dropped is not the same as a
    POST that was rejected: Compute Engine may have accepted it and started
    building. :meth:`GcpBurstProvider.provision` sweeps for the name it chose
    before the request, and raises this when something is there and could not be
    confirmed gone — carrying the id, so the receipt can report a leak instead of
    "nothing to release".
    """

    def __init__(self, message: str, instance_id: str) -> None:
        super().__init__(message)
        self.instance_id = instance_id


class GcpBurstProvider:
    """Provisions a Compute Engine instance in the person's own project.

    **Credential handling, stated accurately.** An earlier version of this
    docstring claimed the credential was "never stored on this object". That was
    false: when a caller passes ``sa_key``, it is held in ``self._sa_key`` for
    the life of the provider, and is visible through ``vars()``. It has to be —
    ``teardown`` authenticates a second time, often minutes after ``provision``,
    and a provider that forgot the key could not release the instance it created.

    What is actually guaranteed, and what the tests pin:

    * it is **never persisted** — not to a receipt, not to the ledger, not to any
      file this package writes;
    * only :class:`~.credentials.CredentialRef` (project, region, and *how* the
      credential was found) survives into anything durable;
    * ``repr()`` does not expose it, so a provider caught in a traceback or a log
      line does not leak the key.

    The exposure is therefore one process's memory for one burst's lifetime,
    which is the cost of being able to guarantee teardown.

    **Verified against real GCP on 2026-08-08.** A T4 spot instance was
    provisioned in ``hushh-pda-dev``/``us-central1`` through this code path,
    released, and independently confirmed absent (404) afterwards — 33.9s,
    $0.0033. What that run cost was worth: it exposed two defects no test had,
    the boot-image family being a 404 and ``teardown`` reporting success on an
    accepted-but-unfinished delete. Both are fixed.

    Exercised so far: ``nvidia-t4`` on ``n1-standard-8`` with a ``guestAccelerators``
    attachment. The A2/A3/A4 machine-type branch — where the accelerator is baked
    into the machine type rather than attached — has still only been unit-tested.
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
        teardown_confirm_seconds: float = 300.0,
        teardown_poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sa_key = sa_key
        self._project = project
        self._region = resolve_region(region)
        self._image = image
        self._boot_disk_gb = boot_disk_gb
        self._confirm_seconds = teardown_confirm_seconds
        self._poll_seconds = teardown_poll_seconds
        self._clock = clock
        self._sleep = sleep
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

    def preflight(self, accelerator_id: str, chip_count: int) -> Preflight:
        """Ask the person's own project whether this burst could actually run.

        Two questions the catalog cannot answer, because they are about *this*
        project and *this* zone rather than about what NVIDIA makes:

        1. **Does the zone carry the part at all?** The provider pins zone ``-a``
           of the configured region, and the answer is genuinely no for some of
           the catalog: on 2026-09-04 ``us-central1-a`` carried GB200 and H100
           but neither H200 nor B200, while the recommender will quote both at
           $88 and $110 an hour. Without this the burst is approved, billed, and
           *then* rejected for an invalid accelerator type.
        2. **Is there spot quota for the order?** A published limit below the
           chip count is a certain ``QUOTA_EXCEEDED``. ``hushh-pda-dev`` has a
           limit of 0 for A100-80GB while happily quoting it.

        Never raises: a pre-flight that fails closed on a network hiccup would
        block a burst the person could have run. Anything unreadable becomes a
        warning.
        """
        shape = _GCP_SHAPES.get(accelerator_id)
        if shape is None:
            return Preflight(blockers=(f"'{accelerator_id}' has no Compute Engine shape.",))

        blockers: list[str] = []
        warnings: list[str] = []
        try:
            session, ref = self._authed_session()
        except Exception as exc:
            return Preflight(warnings=(f"Could not check your project: {exc}",))

        zone = f"{ref.region}-a"
        part = str(shape["zone_accelerator"])
        try:
            url = f"{self._API_ROOT}/projects/{ref.project}/zones/{zone}/acceleratorTypes/{part}"
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                blockers.append(
                    f"{part} is not offered in {zone}. The burst would be rejected "
                    "after you approved it. Pick a different accelerator, or a "
                    "region whose first zone carries this part."
                )
            elif r.status_code >= 400:
                warnings.append(f"Could not confirm {part} is offered in {zone}.")
        except Exception as exc:
            warnings.append(f"Could not confirm {part} is offered in {zone}: {exc}")

        metric = shape.get("quota_metric")
        if metric is None:
            warnings.append(
                f"Compute Engine publishes no spot-quota figure for {part}, so the "
                "quota could not be checked. If your project has none, the burst "
                "will fail with QUOTA_EXCEEDED and nothing will be billed."
            )
        else:
            try:
                r = session.get(
                    f"{self._API_ROOT}/projects/{ref.project}/regions/{ref.region}", timeout=60
                )
                quotas = {q["metric"]: q for q in r.json().get("quotas", [])} if r.ok else {}
                found = quotas.get(str(metric))
                if found is None:
                    warnings.append(f"{metric} is not published for {ref.region}.")
                elif float(found.get("limit", 0)) < chip_count:
                    blockers.append(
                        f"Your spot quota for {part} in {ref.region} is "
                        f"{found.get('limit', 0):g}; this burst needs {chip_count}. "
                        "Request more quota, or choose a smaller part."
                    )
            except Exception as exc:
                warnings.append(f"Could not read your {metric} quota: {exc}")

        return Preflight(blockers=tuple(blockers), warnings=tuple(warnings))

    def _sweep_orphan(
        self, session: Any, project: str, zone: str, name: str, *, cause: BaseException
    ) -> None:
        """After a failed create, delete whatever that create may have started.

        Silent when nothing is there — the overwhelmingly common case is that the
        request never landed. Raises :class:`OrphanedInstance` when something is
        there and could not be confirmed gone, because that is the case where a
        person needs to be told a name and a zone.

        SPOT plus ``maxRunDuration`` bounds the damage either way; this is about
        the receipt not claiming "nothing to release" while something bills.
        """
        url = f"{self._API_ROOT}/projects/{project}/zones/{zone}/instances/{name}"
        try:
            found = session.get(url, timeout=60)
        except Exception:
            return  # cannot look; the deadline brake is what is left
        if found.status_code == 404:
            return
        if found.status_code >= 400:
            return
        handle = InstanceHandle(id=name, destination=f"{project}/{zone}", detail={"zone": zone})
        try:
            if self.teardown(handle):
                return
        except Exception:
            pass
        raise OrphanedInstance(
            f"Provisioning failed ({cause}) but instance {name} exists in {zone} and could "
            "not be confirmed released. It may be billing — check your cloud console.",
            name,
        )

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
        try:
            response = session.post(url, json=body, timeout=60)
        except Exception as exc:
            # The name was chosen before the request, which is what makes this
            # recoverable: a dropped connection tells us nothing about whether
            # Compute Engine accepted the create, so go and look.
            self._sweep_orphan(session, ref.project, zone, name, cause=exc)
            raise RuntimeError(f"Could not reach Compute Engine to provision: {exc}") from exc
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
        """Delete the instance and CONFIRM it is gone before saying so.

        ``instances.delete`` returns a long-running Operation: a 2xx means the
        request was accepted, not that anything has been released. Verified
        against real GCP on 2026-08-08 — the first live burst reported
        ``torn_down: true`` while the instance was still STAGING with a T4
        attached and billing. It did delete about ninety seconds later, but the
        receipt had already claimed a release that had not happened, which is
        exactly the state this whole design exists to make impossible.

        So the contract here is "confirmed absent", not "delete accepted". If it
        cannot be confirmed inside ``teardown_confirm_seconds`` this raises,
        which surfaces as ``leaked_instance`` on the receipt with the instance
        named — an honest "I do not know that this is off" rather than a
        comfortable lie.
        """
        session, ref = self._authed_session()
        zone = handle.detail.get("zone") or f"{ref.region}-a"
        url = f"{self._API_ROOT}/projects/{ref.project}/zones/{zone}/instances/{handle.id}"

        response = session.delete(url, timeout=60)
        # 404 means someone or something already released it. That is success:
        # the invariant is "not running", not "deleted by us".
        if response.status_code == 404:
            handle.torn_down = True
            return True
        if response.status_code >= 400:
            raise RuntimeError(f"Could not tear down {handle.id}: {response.text[:200]}")

        deadline = self._clock() + self._confirm_seconds
        while self._clock() < deadline:
            check = session.get(url, timeout=60)
            if check.status_code == 404:
                handle.torn_down = True
                return True
            self._sleep(self._poll_seconds)

        raise RuntimeError(
            f"Delete was accepted but {handle.id} is still present after "
            f"{self._confirm_seconds:g}s. It may still be billing — check "
            f"{ref.project}/{zone} in the Cloud console."
        )


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
