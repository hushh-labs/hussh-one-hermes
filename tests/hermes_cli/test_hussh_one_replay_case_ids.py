# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Case ids must be unique per session, including cron sessions.

Found live 2026-09-02: 8 of 45 replay rows shared two ids, because cron
session ids start with the job id, not a timestamp, and the 12-character
prefix collapsed every run of one job onto one id.
"""

from hermes_cli.hussh_one_routing.exam.replay import case_prefix


def test_interactive_ids_keep_their_historical_12_char_prefix():
    # Artifacts from earlier runs are matched by case id; this must not move.
    assert case_prefix("20260619_144617_83059e") == "20260619_144"


def test_two_runs_of_one_cron_job_get_different_prefixes():
    a = case_prefix("cron_8a9375e7bf7c_20260623_040037")
    b = case_prefix("cron_8a9375e7bf7c_20260630_051615")
    assert a != b
    assert a.startswith("cron_8a9375e7bf7c")


def test_a_short_id_does_not_raise():
    assert case_prefix("cron_x") == "cron_x"
    assert case_prefix("") == ""
