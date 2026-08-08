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


class GcpBurstProvider:
    """Provisions a Compute Engine instance in the person's own project.

    The credential is resolved per call and never stored on this object; only
    :class:`~.credentials.CredentialRef` is retained, which is safe to put in a
    receipt.
    """

    _API_ROOT = "https://compute.googleapis.com/compute/v1"

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        region: Optional[str] = None,
        sa_key: Optional[str] = None,
    ) -> None:
        self._sa_key = sa_key
        self._project = project
        self._region = resolve_region(region)
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

    def provision(self, spec: InstanceSpec) -> InstanceHandle:
        session, ref = self._authed_session()
        zone = f"{ref.region}-a"
        name = f"hussh-burst-{spec.accelerator_id}-{spec.chip_count}".lower()[:62]
        url = f"{self._API_ROOT}/projects/{ref.project}/zones/{zone}/instances"
        body = {
            "name": name,
            "labels": {"app": "hussh-one-burst", "managed-by": "hermes"},
            "scheduling": {
                # Cheaper, and it self-terminates — a second brake behind teardown.
                "provisioningModel": "SPOT",
                "instanceTerminationAction": "DELETE",
                "maxRunDuration": {"seconds": int(spec.deadline_minutes * 60)},
            },
        }
        response = session.post(url, json=body, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"Could not provision the burst instance: {response.text[:200]}")
        return InstanceHandle(
            id=name,
            destination=f"{ref.project}/{zone}",
            detail={"accelerator": spec.accelerator_id, "chips": spec.chip_count, "zone": zone},
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
