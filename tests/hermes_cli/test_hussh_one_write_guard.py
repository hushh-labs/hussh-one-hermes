# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Refusing to land a file a local model made unparseable.

The first test is the real incident, verbatim. On 2026-08-28 an on-device model
wrote a Python-style comment into bridge_helpers.js and WhatsApp was down for
about 42 hours across 483 failed reconnects, while the cron jobs that depended
on it reported ok and lost every message. A parser would have caught it in
milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import hussh_one_write_guard as guard


# Verbatim from scripts/whatsapp-bridge/bridge_helpers.js at the time it broke.
INCIDENT_JS = """\
function decryptPollVote(creationKey, updateKey, meId) {
  // Baileys poll decryption keys include both creator and voter JIDs.  On
  // WhatsApp LID chats, the poll creator can be the linked-device LID even
  // when sock.user.id is the classic @s.whatsapp.net JID.  Try the exact
  # candidates the live bridge knows before falling back to the generic helper.
  const creatorCandidates = [];
  return creatorCandidates;
}
"""

FIXED_JS = INCIDENT_JS.replace("  # candidates", "  // candidates")


class TestTheRealIncident:
    def test_the_exact_broken_file_is_rejected(self):
        verdict = guard.validate("bridge_helpers.js", INCIDENT_JS)
        assert verdict.ok is False
        assert verdict.language == "javascript"
        assert verdict.checked is True

    def test_the_error_tells_the_model_what_to_fix(self):
        # The message goes back to whatever wrote the file, so it has to name
        # the problem rather than just say "invalid".
        verdict = guard.validate("bridge_helpers.js", INCIDENT_JS)
        assert "syntax" in verdict.actionable.lower()

    def test_the_one_character_fix_passes(self):
        assert guard.validate("bridge_helpers.js", FIXED_JS).ok is True

    def test_the_write_never_lands(self, tmp_path):
        target = tmp_path / "bridge_helpers.js"
        verdict = guard.guard_write(target, INCIDENT_JS)
        assert verdict.ok is False
        # Validate-then-write: the broken content was never on disk at all, so
        # there is no window in which a launcher could pick it up.
        assert not target.exists()

    def test_a_valid_write_does_land(self, tmp_path):
        target = tmp_path / "bridge_helpers.js"
        assert guard.guard_write(target, FIXED_JS).ok is True
        assert target.read_text() == FIXED_JS

    def test_an_existing_good_file_is_not_clobbered_by_a_bad_write(self, tmp_path):
        # The failure mode of write-then-revert: deciding what to restore. Not
        # writing at all has no such problem.
        target = tmp_path / "bridge_helpers.js"
        target.write_text(FIXED_JS)
        guard.guard_write(target, INCIDENT_JS)
        assert target.read_text() == FIXED_JS


class TestPerLanguage:
    @pytest.mark.parametrize(
        "name,content",
        [
            ("a.py", "def f(:\n    pass\n"),
            ("a.json", '{"a": 1,,}'),
            ("a.js", "const x = ;"),
        ],
    )
    def test_broken_content_is_caught(self, name, content):
        assert guard.validate(name, content).ok is False

    @pytest.mark.parametrize(
        "name,content",
        [
            ("a.py", "def f():\n    return 1\n"),
            ("a.json", '{"a": 1}'),
            ("a.js", "const x = 1;\n"),
            ("a.mjs", "export const x = 1;\n"),
        ],
    )
    def test_valid_content_passes(self, name, content):
        assert guard.validate(name, content).ok is True

    def test_a_python_comment_in_python_is_fine(self):
        # The same character that broke the JS file is correct here. The guard
        # keys on the filename's language, not on a banned character.
        assert guard.validate("a.py", "# a comment\nx = 1\n").ok is True

    def test_a_js_comment_in_js_is_fine(self):
        assert guard.validate("a.js", "// a comment\nconst x = 1;\n").ok is True


class TestNotCheckedIsNotAPass:
    def test_an_unknown_extension_reports_it_was_not_checked(self):
        # A caller that reads `ok` alone would treat this as verified. The
        # `checked` flag is what stops "nobody looked" collapsing into "fine".
        verdict = guard.validate("notes.txt", "anything at all {{{")
        assert verdict.ok is True
        assert verdict.checked is False
        assert "no validator" in verdict.error

    def test_typescript_is_deliberately_unhandled(self):
        # node --check cannot parse TS. Running it anyway would reject every
        # valid .ts file, and a guard that blocks correct writes gets disabled.
        verdict = guard.validate("a.ts", "const x: number = 1;\n")
        assert verdict.checked is False

    def test_an_extension_less_file_is_not_checked(self):
        assert guard.validate("Makefile", "\tnot really valid anything").checked is False


class TestItNeverBlocksOnItsOwnFailure:
    def test_a_crashing_validator_does_not_block_the_write(self, monkeypatch, tmp_path):
        def _explode(_text):
            raise RuntimeError("validator is broken")

        monkeypatch.setitem(guard.VALIDATORS, ".js", _explode)
        target = tmp_path / "a.js"
        verdict = guard.guard_write(target, "const x = 1;\n")
        # Failing closed here would let a broken checker halt all editing --
        # a worse outage than the one being prevented.
        assert verdict.ok is True
        assert verdict.checked is False
        assert target.exists()

    def test_a_missing_binary_is_reported_as_unchecked(self, monkeypatch):
        monkeypatch.setattr(guard.shutil, "which", lambda _b: None)
        verdict = guard.validate("a.js", "const x = ;")
        assert verdict.checked is False
        assert "not installed" in verdict.error


class TestScanning:
    def test_it_audits_files_already_on_disk(self, tmp_path):
        good = tmp_path / "good.js"
        bad = tmp_path / "bad.js"
        good.write_text("const x = 1;\n")
        bad.write_text(INCIDENT_JS)
        results = dict(guard.scan_paths([good, bad]))
        assert results[str(good)].ok is True
        assert results[str(bad)].ok is False

    def test_an_unreadable_file_is_reported_not_crashed(self, tmp_path):
        results = dict(guard.scan_paths([tmp_path / "nope.js"]))
        assert results[str(tmp_path / "nope.js")].checked is False


class TestJsoncIsNotABug:
    """Found by running the guard over this repo: 3 tsconfig files, all valid.

    A guard with false positives gets switched off, and then it protects
    nothing. tsconfig and friends are legitimately JSON-with-comments.
    """

    TSCONFIG = (
        "{\n"
        "  // This file is not used in compilation.\n"
        '  "extends": "@docusaurus/tsconfig",\n'
        '  "exclude": [".docusaurus", "build"]\n'
        "}\n"
    )

    @pytest.mark.parametrize(
        "name",
        ["tsconfig.json", "tsconfig.app.json", "jsconfig.json", "devcontainer.json"],
    )
    def test_comments_are_accepted_where_they_are_legal(self, name):
        verdict = guard.validate(name, self.TSCONFIG)
        assert verdict.ok is True
        assert verdict.language == "jsonc"

    def test_comments_are_still_rejected_in_a_strict_json_file(self):
        # package.json must not grow comments just because tsconfig may.
        assert guard.validate("package.json", self.TSCONFIG).ok is False

    def test_a_genuinely_broken_jsonc_file_is_still_caught(self):
        broken = '{\n  // fine\n  "a": 1,,\n}\n'
        assert guard.validate("tsconfig.json", broken).ok is False

    def test_a_url_inside_a_string_is_not_mistaken_for_a_comment(self):
        # Why comments are stripped character-wise rather than by regex:
        # truncating at the // in a URL would turn a valid file invalid.
        content = '{\n  // note\n  "url": "https://example.com/a"\n}\n'
        assert guard.validate("tsconfig.json", content).ok is True

    def test_a_block_comment_is_handled(self):
        assert guard.validate("tsconfig.json", '{\n  /* b */\n  "a": 1\n}\n').ok is True
