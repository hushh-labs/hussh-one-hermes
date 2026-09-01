# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""The playbook: where a model's learning lands, and how it is stopped from rotting.

Each class here corresponds to a named failure mode of self-editing context.
Without these guards the loop looks like it is working while the file fills with
platitudes and the model pays for them on every call.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing import playbook as P


def bullet(text, case_id="c1", suite="file_edit", oracle="parses"):
    return P.Bullet(text=text, case_id=case_id, suite=suite, oracle=oracle)


SPECIFIC = "When patching, check old_string is absent from new_string or a retry duplicates the block"


@pytest.fixture
def book(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return P.Playbook(model="qwen/qwen3.8-27b", suite="file_edit")


class TestBrevityBiasIsRejected:
    def test_a_specific_tactic_is_accepted(self, book):
        assert book.add(bullet(SPECIFIC)) is True

    @pytest.mark.parametrize(
        "vague",
        ["Be careful", "be accurate", "Always be precise", "Try to be thorough"],
    )
    def test_a_platitude_is_rejected(self, book, vague):
        # A model already knows to be careful. What it does not know is that its
        # own last patch left old_string inside new_string.
        assert book.add(bullet(vague)) is False

    def test_a_too_short_bullet_is_rejected(self, book):
        assert book.add(bullet("Use tabs")) is False

    def test_specificity_is_checked_directly(self):
        assert P.is_specific(SPECIFIC) is True
        assert P.is_specific("be careful") is False
        assert P.is_specific("") is False


class TestContextCollapseIsPrevented:
    def test_adding_never_rewrites_existing_bullets(self, book):
        book.add(bullet(SPECIFIC))
        second = "Parse the whole file after splicing, not the replacement fragment alone"
        book.add(bullet(second, case_id="c2"))
        assert [b.text for b in book.active_bullets] == [SPECIFIC, second]

    def test_a_duplicate_is_not_added_twice(self, book):
        assert book.add(bullet(SPECIFIC)) is True
        assert book.add(bullet(SPECIFIC, case_id="c9")) is False
        assert len(book.bullets) == 1

    def test_duplicate_detection_ignores_whitespace_and_case(self, book):
        book.add(bullet(SPECIFIC))
        assert book.add(bullet("  " + SPECIFIC.upper() + "  ")) is False

    def test_the_playbook_is_capped(self, book):
        for i in range(P.MAX_BULLETS + 5):
            book.add(bullet(f"{SPECIFIC} variant number {i} for this suite", case_id=f"c{i}"))
        assert len(book.active_bullets) == P.MAX_BULLETS


class TestEveryBulletCitesItsEvidence:
    def test_the_case_id_is_carried(self, book):
        book.add(bullet(SPECIFIC, case_id="self_chat_gate.js"))
        assert book.active_bullets[0].case_id == "self_chat_gate.js"

    def test_the_rendered_markdown_shows_provenance(self, book):
        # An uncited tactic is indistinguishable from a hallucinated one, and
        # the founder has to be able to read what the agent taught itself.
        book.add(bullet(SPECIFIC, case_id="self_chat_gate.js", oracle="idempotent"))
        rendered = P.render_markdown(book)
        assert "self_chat_gate.js" in rendered
        assert "idempotent" in rendered


class TestRetirementUsesHeldOutOnly:
    def _stocked(self, book):
        book.add(bullet(SPECIFIC))
        return book

    def test_a_bullet_that_never_helps_is_retired(self, book):
        self._stocked(book)
        for _ in range(P.RETIREMENT_ROUNDS):
            retired = book.record_round(held_out_score=0.5, improved=False)
        assert retired
        assert book.active_bullets == []

    def test_one_bad_round_is_noise_not_a_verdict(self, book):
        self._stocked(book)
        assert book.record_round(held_out_score=0.5, improved=False) == []
        assert len(book.active_bullets) == 1

    def test_improving_resets_the_idle_counter(self, book):
        self._stocked(book)
        book.record_round(held_out_score=0.5, improved=False)
        book.record_round(held_out_score=0.6, improved=True)
        book.record_round(held_out_score=0.6, improved=False)
        # Two idle rounds total but not consecutive, so nothing retires.
        assert len(book.active_bullets) == 1

    def test_a_retired_bullet_keeps_its_reason(self, book):
        self._stocked(book)
        for _ in range(P.RETIREMENT_ROUNDS):
            book.record_round(held_out_score=0.5, improved=False)
        assert "did not improve" in book.bullets[0].retired_reason

    def test_history_records_every_round(self, book):
        self._stocked(book)
        book.record_round(held_out_score=0.4, improved=False)
        book.record_round(held_out_score=0.7, improved=True)
        assert [h["held_out"] for h in book.history] == [0.4, 0.7]


class TestPersistence:
    def test_a_playbook_round_trips(self, book):
        book.add(bullet(SPECIFIC, case_id="c1"))
        book.record_round(held_out_score=0.6, improved=True)
        P.save(book)
        loaded = P.load(book.model, book.suite)
        assert [b.text for b in loaded.active_bullets] == [SPECIFIC]
        assert loaded.round_number == 1

    def test_retired_bullets_survive_the_round_trip(self, book):
        # Deleting them would let the loop re-learn and re-retire the same
        # tactic forever.
        book.add(bullet(SPECIFIC))
        for _ in range(P.RETIREMENT_ROUNDS):
            book.record_round(held_out_score=0.5, improved=False)
        P.save(book)
        loaded = P.load(book.model, book.suite)
        assert loaded.active_bullets == []
        assert len(loaded.bullets) == 1

    def test_a_missing_playbook_starts_empty_rather_than_failing(self, book):
        fresh = P.load("never/seen", "terminal")
        assert fresh.bullets == []
        assert fresh.round_number == 0

    def test_a_corrupt_playbook_does_not_take_the_run_down(self, book):
        path = P.path_for(book.model, book.suite)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert P.load(book.model, book.suite).bullets == []

    def test_a_model_id_with_a_slash_does_not_split_into_directories(self, book):
        # "qwen/qwen3.8-27b" would otherwise create a nested tree that a later
        # lookup by the same id would not find.
        path = P.path_for("qwen/qwen3.8-27b", "file_edit")
        assert "/" not in path.parent.name


class TestPlaybooksAreNeverShared:
    def test_two_models_keep_separate_files(self):
        a = P.path_for("qwen/qwen3.8-27b", "file_edit")
        b = P.path_for("google/gemma-4-31b-qat", "file_edit")
        assert a != b

    def test_two_suites_keep_separate_files(self):
        a = P.path_for("qwen/qwen3.8-27b", "file_edit")
        b = P.path_for("qwen/qwen3.8-27b", "terminal")
        assert a != b


class TestRendering:
    def test_an_empty_playbook_injects_nothing(self, book):
        # A heading with no content is pure context tax on every call.
        assert book.render() == ""

    def test_the_rendered_prompt_lists_the_tactics(self, book):
        book.add(bullet(SPECIFIC))
        rendered = book.render()
        assert SPECIFIC in rendered
        assert "your own previous mistakes" in rendered

    def test_retired_tactics_are_not_injected(self, book):
        book.add(bullet(SPECIFIC))
        for _ in range(P.RETIREMENT_ROUNDS):
            book.record_round(held_out_score=0.5, improved=False)
        assert book.render() == ""
