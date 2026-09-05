# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Bring-your-own-cloud credential broker.

The accelerator is provisioned in the **person's own** project, with their own
credentials, and billed to them. Hermes never becomes the custodian of someone
else's cloud.

Precedence deliberately mirrors ``load_operator_credentials()`` in
``hushh-research`` (``consent-protocol/hushh_mcp/services/gcp_run_client.py``)
so the two repositories resolve credentials the same way:

1. a service-account JSON handed in for this one call,
2. a base64 service-account JSON in the environment,
3. Application Default Credentials.

**The key is never persisted.** It is held for the life of one request and what
survives is a :class:`CredentialRef` — project, region, and *which* source was
used. That is enough to write a receipt and not enough to impersonate anyone.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

#: Same scope set the research repo requests, so a key works identically in both.
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

#: Checked in order.  ``GCP_`` first: that is what this org actually names them.
_KEY_ENV_VARS = ("HUSSH_BURST_SA_KEY_B64", "GCP_DEPLOY_SA_KEY_B64")
_PROJECT_ENV_VARS = ("HUSSH_BURST_PROJECT", "GCP_DEPLOY_PROJECT", "GOOGLE_CLOUD_PROJECT")
_REGION_ENV_VARS = ("HUSSH_BURST_REGION", "GCP_DEPLOY_REGION")

DEFAULT_REGION = "us-central1"


class CredentialError(RuntimeError):
    """No usable credential was found, or the one supplied was malformed."""


@dataclass(frozen=True)
class CredentialRef:
    """What a receipt may record about a credential — never the credential.

    ``source`` is one of ``request``, ``environment`` or ``adc``.
    """

    project: Optional[str]
    region: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {"project": self.project, "region": self.region, "credential_source": self.source}


def _first_env(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _decode_sa_json(raw: str) -> dict[str, Any]:
    """Decode a service-account JSON that may or may not be base64-wrapped."""
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise CredentialError("The service-account key is not valid base64 JSON.") from exc
    try:
        info = json.loads(text)
    except ValueError as exc:
        raise CredentialError("The service-account key is not valid JSON.") from exc
    if not isinstance(info, dict) or "client_email" not in info:
        raise CredentialError("The service-account key is missing required fields.")
    return info


def resolve_region(region: Optional[str] = None) -> str:
    return region or _first_env(_REGION_ENV_VARS) or DEFAULT_REGION


def resolve_credentials(
    sa_key: Optional[str] = None,
    project: Optional[str] = None,
    region: Optional[str] = None,
) -> tuple[Any, CredentialRef]:
    """Resolve a credential and the reference that may be recorded about it.

    ``sa_key`` is accepted for exactly one call and is never written anywhere.
    Raises :class:`CredentialError` when nothing usable is available — a burst
    must fail closed rather than silently fall back to Hermes' own identity.
    """
    try:
        from google.auth import default as google_auth_default  # type: ignore[import-not-found]
        from google.oauth2 import service_account  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - optional cloud extra
        raise CredentialError(
            "Cloud bursting needs the google-auth libraries. Install the cloud extra."
        ) from exc

    resolved_region = resolve_region(region)

    raw = sa_key or _first_env(_KEY_ENV_VARS)
    source = "request" if sa_key else ("environment" if raw else "adc")

    if raw:
        info = _decode_sa_json(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
        resolved_project = project or info.get("project_id") or _first_env(_PROJECT_ENV_VARS)
        return creds, CredentialRef(resolved_project, resolved_region, source)

    try:
        creds, adc_project = google_auth_default(scopes=list(SCOPES))
    except Exception as exc:
        raise CredentialError(
            "No cloud credential is available. Connect a project before bursting."
        ) from exc
    resolved_project = project or adc_project or _first_env(_PROJECT_ENV_VARS)
    if not resolved_project:
        raise CredentialError("No cloud project could be determined for this burst.")
    return creds, CredentialRef(resolved_project, resolved_region, "adc")
