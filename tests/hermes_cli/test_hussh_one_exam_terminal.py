# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Grading generated shell without running it.

The biggest suite, because terminal is 58% of everything Hermes does. Several of
these tests exist because the obvious implementation was measurably wrong on the
real corpus: it either fired on working reference commands or could never pass
at all.
"""

from __future__ import annotations

import pytest

from hermes_cli.hussh_one_routing.exam import terminal as T
from hermes_cli.hussh_one_routing.exam.model import FAIL, PASS, SKIP


def outcome(verdict, name):
    return next(o for o in verdict.outcomes if o.name == name)


def grade(command, **kw):
    return T.grade(case_id="c", args={"command": command}, **kw)


class TestShellSyntax:
    def test_a_valid_command_parses(self):
        assert outcome(grade("ls -la /tmp"), "shell_parses").outcome == PASS

    def test_an_unbalanced_quote_fails(self):
        assert outcome(grade('echo "unterminated'), "shell_parses").outcome == FAIL

    def test_an_unclosed_block_fails(self):
        assert outcome(grade("if true; then echo hi"), "shell_parses").outcome == FAIL

    def test_an_empty_command_fails(self):
        assert outcome(grade("   "), "shell_parses").outcome == FAIL

    def test_a_heredoc_python_program_parses(self):
        # 42% of real calls embed a whole program this way.
        command = 'python3 - <<PY\nprint("hi")\nPY\n'
        assert outcome(grade(command), "shell_parses").outcome == PASS


class TestQuoteAndHeredocAwareness:
    """The production guard gets this wrong; the corpus proves it."""

    def test_an_ampersand_inside_heredoc_prose_is_not_backgrounding(self):
        # Two real commands were rejected with exit -1 for exactly this: the &
        # was inside heredoc text ("AUDIT & REVIEW").
        command = 'cat <<EOF\nSECTION: AUDIT & REVIEW\nEOF\n'
        assert outcome(grade(command), "background_flag_consistency").outcome == PASS

    def test_a_real_trailing_ampersand_is_caught(self):
        v = grade("sleep 100 &")
        assert outcome(v, "background_flag_consistency").outcome == FAIL

    def test_declaring_background_makes_it_fine(self):
        v = T.grade(case_id="c", args={"command": "sleep 100 &", "background": True})
        assert outcome(v, "background_flag_consistency").outcome == PASS

    def test_a_logical_and_is_not_backgrounding(self):
        assert outcome(grade("a && b"), "background_flag_consistency").outcome == PASS

    def test_rm_inside_a_string_is_not_a_destructive_verb(self):
        v = grade('echo "how to rm -rf safely"')
        assert outcome(v, "no_unrequested_destructive_verb").outcome == PASS

    def test_rm_inside_a_heredoc_is_not_a_destructive_verb(self):
        command = 'cat <<DOC\nrm -rf /everything\nDOC\n'
        assert outcome(grade(command), "no_unrequested_destructive_verb").outcome == PASS


class TestDestructiveVerbs:
    def test_an_unrequested_delete_fails(self):
        v = grade("rm -rf /tmp/build", instruction="list the files")
        assert outcome(v, "no_unrequested_destructive_verb").outcome == FAIL

    def test_a_requested_delete_passes(self):
        v = grade("rm -rf /tmp/build", instruction="delete the build directory")
        assert outcome(v, "no_unrequested_destructive_verb").outcome == PASS

    def test_rm_does_not_fire_on_npm(self):
        # A substring test would flag every npm command ever written.
        v = grade("npm install", instruction="install deps")
        assert outcome(v, "no_unrequested_destructive_verb").outcome == PASS

    def test_a_push_needs_asking(self):
        v = grade("git push origin main", instruction="show me the diff")
        assert outcome(v, "no_unrequested_destructive_verb").outcome == FAIL


class TestPathGrounding:
    KNOWN = ["/Users/me/repo"]

    def test_a_known_path_passes(self):
        v = grade("cd /Users/me/repo && git status", known_paths=self.KNOWN)
        assert outcome(v, "paths_grounded").outcome == PASS

    def test_a_hallucinated_path_fails(self):
        # The real case: the brand spelled hussh- when the repo on disk is
        # hushh-, swallowed by `2>/dev/null || true` and reported as an empty
        # result rather than an error.
        v = grade("grep -r x /Users/me/other-repo", known_paths=self.KNOWN)
        assert outcome(v, "paths_grounded").outcome == FAIL

    def test_scratch_directories_are_exempt(self):
        v = grade("echo hi > /tmp/scratch.txt", known_paths=self.KNOWN)
        assert outcome(v, "paths_grounded").outcome == PASS

    def test_system_paths_are_exempt(self):
        v = grade("/usr/bin/env python3 -V", known_paths=self.KNOWN)
        assert outcome(v, "paths_grounded").outcome == PASS

    def test_no_context_skips_rather_than_guesses(self):
        assert outcome(grade("cd /anywhere"), "paths_grounded").outcome == SKIP


class TestInteractiveAndUnbounded:
    def test_an_interactive_login_fails(self):
        # Real: "gcloud crashed (EOFError): EOF when reading a line".
        v = grade("gcloud auth login --no-launch-browser")
        assert outcome(v, "no_interactive_command").outcome == FAIL

    def test_an_editor_fails(self):
        assert outcome(grade("vim notes.txt"), "no_interactive_command").outcome == FAIL

    def test_a_normal_command_passes(self):
        assert outcome(grade("git log -5"), "no_interactive_command").outcome == PASS

    def test_a_broad_recursive_scan_needs_a_timeout(self):
        # Real: exit 124 after 180s, no timeout argument raised.
        v = grade("grep -rn foo /Users/me/")
        assert outcome(v, "bounded_recursive_scan").outcome == FAIL

    def test_raising_the_timeout_makes_it_fine(self):
        v = T.grade(
            case_id="c",
            args={"command": "grep -rn foo /Users/me/", "timeout": 600},
        )
        assert outcome(v, "bounded_recursive_scan").outcome == PASS

    def test_a_narrow_scan_is_fine(self):
        v = grade("grep -rn foo ./src")
        assert outcome(v, "bounded_recursive_scan").outcome == PASS


class TestArgumentSchema:
    def test_a_missing_command_fails(self):
        v = T.grade(case_id="c", args={"timeout": 60})
        assert outcome(v, "argument_schema_valid").outcome == FAIL

    def test_an_unknown_parameter_fails(self):
        v = T.grade(case_id="c", args={"command": "ls", "nonsense": 1})
        assert outcome(v, "argument_schema_valid").outcome == FAIL

    def test_a_non_integer_timeout_fails(self):
        v = T.grade(case_id="c", args={"command": "ls", "timeout": "60"})
        assert outcome(v, "argument_schema_valid").outcome == FAIL

    def test_the_never_used_params_are_still_valid(self):
        # workdir and watch_patterns were used 0 times in 877 real calls, but
        # the schema declares them, so using one is unusual rather than wrong.
        v = T.grade(case_id="c", args={"command": "ls", "workdir": "/tmp"})
        assert outcome(v, "argument_schema_valid").outcome == PASS


class TestHouseStyleIsNotCorrectness:
    def test_policy_checks_are_absent_from_the_verdict(self):
        # The frontier commands violate tool-policy at 35% and bare-interpreter
        # at 80% and mostly worked. Folding that into correctness would rank a
        # model that imitates our conventions above one that does the job.
        names = {o.name for o in grade("cat file.txt").outcomes}
        assert "tool_policy_compliance" not in names
        assert "interpreter_matches_project" not in names

    def test_they_are_still_reported(self):
        found = {o.name for o in T.advisory({"command": "cat x"})}
        assert "tool_policy_compliance" in found

    def test_shelling_out_to_cat_is_flagged_as_advisory(self):
        outcomes = {o.name: o for o in T.advisory({"command": "cat file.txt"})}
        assert outcomes["tool_policy_compliance"].outcome == FAIL
        assert "read_file" in outcomes["tool_policy_compliance"].detail


class TestInterpreterDetection:
    """This oracle could not return PASS at all in its first version."""

    CTX = {"venv_python": ".venv/bin/python3"}

    def test_the_venv_interpreter_passes(self):
        # `.venv/bin/python3` is preceded by `/`, so anchoring on a space made
        # every correct call fall into "no interpreter invoked". 0 pass, 207
        # fail on the real corpus: the signature of a check that cannot succeed.
        assert T.check_interpreter(".venv/bin/python3 x.py", self.CTX).outcome == PASS

    def test_a_bare_interpreter_is_flagged(self):
        assert T.check_interpreter('python3 -c "import httpx"', self.CTX).outcome == FAIL

    def test_a_piped_bare_interpreter_is_flagged(self):
        assert T.check_interpreter("cat a | python3 -c x", self.CTX).outcome == FAIL

    def test_no_interpreter_skips(self):
        assert T.check_interpreter("git status", self.CTX).outcome == SKIP

    def test_no_recorded_venv_skips(self):
        assert T.check_interpreter("python3 x.py", {}).outcome == SKIP

    def test_an_absolute_system_interpreter_is_not_bare(self):
        assert T.check_interpreter("/usr/bin/python3 x.py", self.CTX).outcome == PASS


class TestHeadVerb:
    def test_cd_does_not_become_the_verb(self):
        # Skipping only `cd` returns its path argument, which misreported 151 of
        # 586 real calls because `cd <repo> && ...` is the agent's usual shape.
        assert T.head_verb("cd /Users/me/repo && python3 x.py") == "python3"

    def test_a_plain_command_reports_itself(self):
        assert T.head_verb("git status") == "git"

    def test_wrappers_are_skipped(self):
        assert T.head_verb("sudo env time git status") == "git"

    def test_a_leading_assignment_is_skipped(self):
        assert T.head_verb("FOO=1 python3 x.py") == "python3"

    def test_an_empty_command_yields_nothing(self):
        assert T.head_verb("") == ""


class TestNothingIsExecuted:
    def test_grading_a_destructive_command_does_not_run_it(self, tmp_path):
        # Running a model's generated shell on the serving machine would be an
        # unbounded remote-code path opened for the sake of a benchmark.
        victim = tmp_path / "keepme.txt"
        victim.write_text("still here")
        grade(f"rm -f {victim}", instruction="delete it")
        assert victim.exists()

    def test_the_suite_states_what_static_checking_cannot_do(self):
        assert any("wrong question" in c for c in T.CANNOT_CATCH)
