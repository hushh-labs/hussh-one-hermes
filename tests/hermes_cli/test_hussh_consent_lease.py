# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.hussh_consent_lease import (
    MAX_INFORMATION_BYTES,
    consume_lease,
    materialize_decrypted_export,
)


@pytest.fixture
def hermes_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def test_materialized_lease_is_consumed_exactly_once(hermes_home):
    receipt = materialize_decrypted_export(
        {"portfolio": {"cash": 42}},
        expected_scope="attr.financial.portfolio.*",
        granted_scope="attr.financial.portfolio.*",
        now=100,
    )

    path = hermes_home / "tmp" / "consent" / f"{receipt['lease_id']}.json"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600

    result = consume_lease(receipt["lease_id"], now=101)
    assert result["information"] == {"portfolio": {"cash": 42}}
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        consume_lease(receipt["lease_id"], now=101)


def test_empty_export_does_not_create_a_lease(hermes_home):
    receipt = materialize_decrypted_export(
        {},
        expected_scope="attr.financial.sources.*",
        granted_scope="attr.financial.sources.*",
    )

    assert receipt["status"] == "decrypted_export_empty"
    assert not list((hermes_home / "tmp" / "consent").glob("*.json"))


def test_oversized_export_requires_narrower_scope(hermes_home):
    receipt = materialize_decrypted_export(
        {"value": "x" * MAX_INFORMATION_BYTES},
        expected_scope="attr.financial.*",
        granted_scope="attr.financial.*",
    )

    assert receipt["status"] == "requires_narrower_scope"
    assert not list((hermes_home / "tmp" / "consent").glob("*.json"))


def test_expired_lease_is_deleted_before_error(hermes_home):
    receipt = materialize_decrypted_export(
        {"value": "approved"},
        expected_scope="attr.example",
        granted_scope="attr.example",
        now=100,
    )
    path = hermes_home / "tmp" / "consent" / f"{receipt['lease_id']}.json"

    with pytest.raises(TimeoutError):
        consume_lease(receipt["lease_id"], now=1000)
    assert not path.exists()


def test_lease_file_contains_no_ciphertext_fields(hermes_home):
    receipt = materialize_decrypted_export(
        {"value": "approved"},
        expected_scope="attr.example",
        granted_scope="attr.example",
    )
    path = hermes_home / "tmp" / "consent" / f"{receipt['lease_id']}.json"
    payload = json.loads(path.read_text())

    assert "ciphertext" not in payload
    assert "crypto" not in payload
