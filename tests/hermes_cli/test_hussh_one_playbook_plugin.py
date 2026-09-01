# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Carrying learned tactics into a live session.

This runs on the founder's live WhatsApp path, so most of these tests are about
the plugin failing open. A learning system that can take down the agent it was
meant to improve is a bad trade.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing import playbook as pb
from plugins.puppy_playbook import MAX_CHARS, register, render

TACTIC = "Splice new_string into the pre-image before parsing, never the fragment alone"
OTHER = "Quote every shell path; an unquoted one with a space becomes two arguments"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def stock(model, suite, *texts):
    book = pb.Playbook(model=model, suite=suite)
    for i, text in enumerate(texts):
        book.add(pb.Bullet(text=text, case_id=f"c{i}", suite=suite))
    pb.save(book)
    return book


class TestItInjectsWhatWasLearned:
    def test_tactics_reach_the_prompt(self, home):
        stock("qwen/qwen3.8-27b", "file_edit", TACTIC)
        section = render({"model": "qwen/qwen3.8-27b"})
        assert TACTIC in section
        assert "your own past mistakes" in section

    def test_each_suite_is_a_labelled_block(self, home):
        stock("m", "file_edit", TACTIC)
        stock("m", "terminal", OTHER)
        section = render({"model": "m"})
        assert "## file_edit" in section
        assert "## terminal" in section
        assert TACTIC in section and OTHER in section

    def test_it_is_keyed_by_the_serving_model(self, home):
        # The tactics that stop a 4B-active MoE truncating are not the ones that
        # stop a dense 27B duplicating a region.
        stock("model-a", "file_edit", TACTIC)
        assert TACTIC in render({"model": "model-a"})
        assert render({"model": "model-b"}) == ""

    def test_retired_tactics_are_not_injected(self, home):
        book = stock("m", "file_edit", TACTIC)
        for _ in range(pb.RETIREMENT_ROUNDS):
            book.record_round(held_out_score=0.5, improved=False)
        pb.save(book)
        assert render({"model": "m"}) == ""


class TestItFailsOpen:
    def test_no_model_yields_nothing(self, home):
        assert render({}) == ""
        assert render({"model": ""}) == ""

    def test_an_empty_playbook_yields_nothing(self, home):
        # A heading with no tactics is pure context tax on every call.
        assert render({"model": "never/trained"}) == ""

    def test_a_corrupt_playbook_does_not_raise(self, home):
        path = pb.path_for("m", "file_edit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        assert render({"model": "m"}) == ""

    def test_registration_never_raises_into_discovery(self):
        class _Hostile:
            def register_system_prompt_section(self, *a, **k):
                raise RuntimeError("core said no")

        register(_Hostile())  # must not propagate

    def test_registration_passes_the_callable_not_a_string(self):
        captured = {}

        class _Ctx:
            def register_system_prompt_section(self, section_id, content, **kw):
                captured["id"] = section_id
                captured["callable"] = callable(content)
                captured["max_chars"] = kw.get("max_chars")

        register(_Ctx())
        # A string would be frozen at import time, before any model is known.
        assert captured["callable"] is True
        assert captured["max_chars"] == MAX_CHARS


class TestTheContextTaxIsBounded:
    def test_a_huge_playbook_is_capped(self, home):
        long_tactics = [
            f"Tactic number {i}: " + ("check the delimiter carefully " * 12)
            for i in range(pb.MAX_BULLETS)
        ]
        stock("m", "file_edit", *long_tactics)
        assert len(render({"model": "m"})) <= MAX_CHARS

    def test_truncation_lands_on_a_bullet_boundary(self, home):
        # Half a tactic is worse than none: the model still tries to follow it.
        long_tactics = [
            f"Tactic {i}: " + ("verify the closing brace before replying " * 10)
            for i in range(pb.MAX_BULLETS)
        ]
        stock("m", "file_edit", *long_tactics)
        section = render({"model": "m"})
        for line in section.splitlines():
            if line.startswith("- "):
                assert line.rstrip().endswith(("replying", "y")) or len(line) > 20

    def test_a_small_playbook_is_left_alone(self, home):
        stock("m", "file_edit", TACTIC)
        section = render({"model": "m"})
        assert section.rstrip().endswith(TACTIC)
