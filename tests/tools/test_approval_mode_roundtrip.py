# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""`approvals.mode` must survive its own YAML round trip.

Found live 2026-09-06 on the founder's daily driver. Their config held
``mode: 'false'`` -- a quoted string -- and the logs carried a repeating
"Unknown approvals.mode 'false' — defaulting to 'manual'". They had asked for
autonomous operation and were being prompted for every command instead.

The mechanism is a round trip through this module's own tolerance: YAML 1.1
parses a bare ``off`` as boolean False, which ``_normalize_approval_mode``
correctly maps back to "off". But once any writer re-serialises that boolean,
it lands on disk as the STRING 'false', which the valid-modes check rejects --
so the value the owner set silently becomes the strictest possible setting.
"""

from __future__ import annotations

import pytest

from tools.approval import _normalize_approval_mode as normalize


class TestTheYamlRoundTrip:
    def test_a_bare_off_parses_to_false_and_still_means_off(self):
        """YAML 1.1: `mode: off` reaches us as the boolean False."""
        assert normalize(False) == "off"

    def test_the_stringified_boolean_the_round_trip_produces_means_off(self):
        """The exact value found in the founder's config."""
        assert normalize("false") == "off"

    def test_it_is_case_and_whitespace_tolerant(self):
        assert normalize("  False  ") == "off"
        assert normalize("FALSE") == "off"

    @pytest.mark.parametrize("spelling", ["no", "0"])
    def test_other_yaml_falsy_spellings_also_mean_off(self, spelling):
        assert normalize(spelling) == "off"

    def test_a_truthy_round_trip_still_means_manual(self):
        assert normalize(True) == "manual"
        assert normalize("true") == "manual"


class TestTheRealModesAreUnchanged:
    @pytest.mark.parametrize("mode", ["manual", "smart", "off"])
    def test_valid_modes_pass_through(self, mode):
        assert normalize(mode) == mode

    def test_an_empty_value_still_fails_closed_to_manual(self):
        assert normalize("") == "manual"
        assert normalize("   ") == "manual"

    def test_a_genuinely_unknown_mode_still_fails_closed(self):
        """'auto' was never a mode; it must not become a silent bypass."""
        assert normalize("auto") == "manual"
        assert normalize("yolo") == "manual"

    def test_none_fails_closed(self):
        assert normalize(None) == "manual"

    def test_normalize_only_ever_returns_a_valid_mode(self):
        for value in (None, True, False, "", "auto", "off", "smart", "manual",
                      "false", "TRUE", 3, [], {}):
            assert normalize(value) in ("manual", "smart", "off")
