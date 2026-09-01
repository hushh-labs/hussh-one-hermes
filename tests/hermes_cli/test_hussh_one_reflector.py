# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The teacher half of the loop.

The reflector is the only part that leaves the machine, so its guards matter
more than its output: it must not be local, it must not grade, and it must not
be able to take down a round that already cost hours.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.hussh_one_routing import reflector as R

FAILURES = [
    {
        "case_id": "gate.js",
        "suite": "file_edit",
        "fault": "execution",
        "oracles": ["parses"],
        "asi": "parses: line 2: SyntaxError: Invalid or unexpected token",
    },
    {
        "case_id": "self_chat_gate.js",
        "suite": "file_edit",
        "fault": "execution",
        "oracles": ["idempotent"],
        "asi": "idempotent: old_string survives inside new_string",
    },
]

GOOD_REPLY = json.dumps(
    {
        "tactics": [
            {
                "text": "JavaScript comments are // or /* */; a # begins a comment "
                        "in Python and is a syntax error here.",
                "case_id": "gate.js",
                "oracle": "parses",
            }
        ]
    }
)


class TestTheReflectorMustNotBeLocal:
    def test_an_on_device_model_is_refused(self):
        # A small model reflecting on a small model's failures produces exactly
        # the generic advice ACE calls brevity bias, and it would be
        # indistinguishable from the loop working.
        from hermes_cli.hussh_one_pkm.judge import JudgeIsOnDevice

        with pytest.raises(JudgeIsOnDevice):
            R.make_reflector(model="google/gemma-4-31b-qat")

    def test_a_local_provider_is_refused_even_with_a_neutral_name(self):
        from hermes_cli.hussh_one_pkm.judge import JudgeIsOnDevice

        with pytest.raises(JudgeIsOnDevice):
            R.make_reflector(model="some-model", provider="lmstudio")

    def test_a_frontier_model_is_accepted(self):
        assert callable(R.make_reflector(model="claude-opus-5"))

    def test_an_injected_asker_needs_no_model(self):
        reflect = R.make_reflector(ask=lambda prompt: GOOD_REPLY)
        assert len(reflect(FAILURES, "")) == 1


class TestThePromptCarriesDiagnosesNotScores:
    def test_the_error_text_is_included(self):
        # A score tells the reflector something went wrong; the diagnostic tells
        # it what to write a tactic about.
        prompt = R.build_prompt(FAILURES, "file_edit")
        assert "Invalid or unexpected token" in prompt
        assert "old_string survives inside new_string" in prompt

    def test_the_oracle_names_are_included(self):
        prompt = R.build_prompt(FAILURES, "file_edit")
        assert "parses" in prompt and "idempotent" in prompt

    def test_existing_tactics_are_shown_to_avoid_repeats(self):
        prompt = R.build_prompt(FAILURES, "file_edit", "- always close braces")
        assert "always close braces" in prompt
        assert "do not repeat" in prompt.lower()

    def test_an_empty_playbook_is_stated_plainly(self):
        assert "currently empty" in R.build_prompt(FAILURES, "file_edit", "")

    def test_the_failure_list_is_capped(self):
        many = [dict(FAILURES[0], case_id=f"c{i}") for i in range(80)]
        prompt = R.build_prompt(many, "file_edit")
        assert prompt.count("failed [") <= R.MAX_FAILURES_SHOWN


class TestParsingIsTolerantButNotCredulous:
    def test_plain_json_parses(self):
        assert len(R.parse_tactics(GOOD_REPLY, "file_edit")) == 1

    def test_a_fenced_reply_parses(self):
        # A reflector that wraps its JSON has still done the work; discarding a
        # whole round over formatting makes the loop look broken when it is not.
        assert len(R.parse_tactics(f"```json\n{GOOD_REPLY}\n```", "file_edit")) == 1

    def test_json_after_prose_parses(self):
        assert len(R.parse_tactics(f"Here you go:\n{GOOD_REPLY}", "file_edit")) == 1

    def test_an_uncited_tactic_is_dropped(self):
        # An uncited tactic cannot be audited or retired, and is
        # indistinguishable from one the reflector invented.
        reply = json.dumps({"tactics": [{"text": "Close every brace properly."}]})
        assert R.parse_tactics(reply, "file_edit") == []

    def test_an_empty_tactic_is_dropped(self):
        reply = json.dumps({"tactics": [{"text": "  ", "case_id": "a"}]})
        assert R.parse_tactics(reply, "file_edit") == []

    def test_unparseable_output_yields_nothing_rather_than_raising(self):
        assert R.parse_tactics("I could not do that", "file_edit") == []
        assert R.parse_tactics("", "file_edit") == []

    def test_the_suite_is_stamped_on_every_bullet(self):
        bullets = R.parse_tactics(GOOD_REPLY, "terminal")
        assert bullets[0].suite == "terminal"

    def test_the_citation_survives(self):
        bullets = R.parse_tactics(GOOD_REPLY, "file_edit")
        assert bullets[0].case_id == "gate.js"
        assert bullets[0].oracle == "parses"


class TestItCannotTakeDownARound:
    def test_an_unreachable_reflector_returns_no_tactics(self):
        # A round with no new tactics still reports its held-out score; a
        # crashed run loses hours of generation.
        def _explode(prompt):
            raise RuntimeError("provider is down")

        reflect = R.make_reflector(ask=_explode)
        assert reflect(FAILURES, "") == []

    def test_no_failures_means_no_call_at_all(self):
        def _must_not_run(prompt):
            raise AssertionError("reflector called with nothing to reflect on")

        assert R.make_reflector(ask=_must_not_run)([], "") == []

    def test_a_non_string_reply_is_coerced(self):
        reflect = R.make_reflector(ask=lambda p: json.loads(GOOD_REPLY))
        # A dict reply is stringified; it will not parse as JSON with single
        # quotes, so the round simply gains nothing rather than crashing.
        assert isinstance(reflect(FAILURES, ""), list)


class TestItProposesButNeverGrades:
    def test_the_reflector_returns_only_bullets(self):
        # Nothing it returns can mark a case correct. The oracles decided that
        # before this ran, which is what stops it rubber-stamping.
        from hermes_cli.hussh_one_routing.playbook import Bullet

        bullets = R.make_reflector(ask=lambda p: GOOD_REPLY)(FAILURES, "")
        assert all(isinstance(b, Bullet) for b in bullets)

    def test_its_task_is_separate_from_the_judge_task(self):
        from hermes_cli.hussh_one_pkm.judge import JUDGE_TASK

        assert R.REFLECT_TASK != JUDGE_TASK


class TestBothTasksAreRegistered:
    def test_the_reflect_task_has_config_defaults(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert R.REFLECT_TASK in DEFAULT_CONFIG["auxiliary"]

    def test_the_judge_task_has_config_defaults(self):
        # It worked before (the getter is a plain dict lookup) but was invisible
        # to `hermes model`, the dashboard, and reset-to-auto.
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        from hermes_cli.hussh_one_pkm.judge import JUDGE_TASK

        assert JUDGE_TASK in DEFAULT_CONFIG["auxiliary"]

    def test_both_appear_in_the_model_picker(self):
        from hermes_cli.main import _AUX_TASKS
        from hermes_cli.hussh_one_pkm.judge import JUDGE_TASK

        names = {task for task, _label, _help in _AUX_TASKS}
        assert JUDGE_TASK in names
        assert R.REFLECT_TASK in names
