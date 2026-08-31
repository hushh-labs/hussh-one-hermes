# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade a ``write_file`` or ``patch`` the way the disk would.

This is the suite that matters most, because this is the failure class that has
already cost real time. Two incidents, both the same shape: a delimiter mangled
crossing the JSON tool-call boundary.

  * A local model wrote a Python-style ``#`` comment into ``bridge_helpers.js``
    and WhatsApp was down for about 42 hours.
  * A model wrote a 264-line Python file with twelve backslash-escaped ``\\"\\"\\"``
    and one stray raw ``\"\"\"`` at byte 0. One unterminated docstring swallowed the
    whole file. **It landed anyway**: the tool result says ``verified: true`` and
    ``bytes_written: 12680`` beside a lint status of ``error``.

Six checks, all deterministic, each measured against the real corpus of 72
unique calls. Three of them exist because the obvious implementation is wrong:

**Parse the post-image, never the fragment.** 15 of 22 real ``new_string``
values fail to parse on their own while the resulting file is perfectly valid,
so grading the fragment gives a 68% false-failure rate.

**Match the escaped delimiter in its triple form only.** A bare ``\\"`` is legal
inside a string literal and appears in 14 of the 30 intact files, so a
single-escape rule would be 47% false-positive. The triple form fired on exactly
the one broken file and nothing else.

**The patch format is search/replace, not a diff.** ``mode="replace"`` in 25 of
25 real calls. The schema also documents a ``*** Begin Patch`` block format; it
was used zero times, and every one of its 97 appearances in the dumps is inside
the tool's own schema text. An oracle expecting a unified diff scores 0/25.

What none of this catches is stated in ``CANNOT_CATCH`` and is not a detail: the
corpus contains a patch that parses, applies once, is confined and idempotent,
and still references a variable that does not exist. Every check here says PASS.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .model import FAIL, PASS, SKIP, Outcome, Verdict

SUITE_ID = "file_edit"

# Backslash-escaped triple-quote delimiters that survived the JSON boundary.
# Deliberately only the triple form; see the module docstring.
_ESCAPED_TRIPLE = re.compile(r'\\"\\"\\"' + "|" + r"\\'\\'\\'")

_TRUNCATION = re.compile(
    r"(\.\.\.\[truncated\]|\[\s*\.\.\.\s*\d[\d,]*\s*characters?[^\]]*\])\s*$"
)

CANNOT_CATCH = (
    "Undefined names, wrong types, inverted conditionals, off-by-one: all valid "
    "syntax. The corpus contains a real patch that passes every check here while "
    "Pyright reports '\"candidate\" is not defined'.",
    "A correct edit applied to the wrong file, or the wrong one of two similar "
    "functions. Anchor uniqueness proves the anchor was unambiguous, not right.",
    "Deleted logic: 3 of 25 real patches are net-shrinking, and a dropped guard "
    "clause parses fine.",
    "Anything written through the terminal tool with a heredoc or sed, which is "
    "58% of all tool calls and never passes through this code at all.",
)


def post_image(args: dict, pre: Optional[str]) -> Optional[str]:
    """The file as it would exist after this call, or None if unknowable.

    For ``patch`` this needs the pre-image, which is why recovering it from the
    session's earlier ``read_file`` results is worth the trouble: without it the
    only checkable thing is the fragment, and the fragment lies.
    """
    if "content" in args:
        return args.get("content")
    old, new = args.get("old_string"), args.get("new_string")
    if pre is None or old is None or new is None:
        return None
    if pre.count(old) != 1:
        return None
    return pre.replace(old, new, 1)


def check_parses(path: str, args: dict, pre: Optional[str]) -> Outcome:
    """Reuse the production write guard unmodified.

    Imported, never reimplemented and never vendored: a forked copy would let
    the exam and the guard that actually protects the disk drift apart, and then
    the exam would be grading a checker nobody runs.
    """
    from hermes_cli.hussh_one_write_guard import validate

    body = post_image(args, pre)
    if body is None:
        return Outcome("parses", SKIP, "no post-image; pre-image not recovered")
    verdict = validate(path, body)
    if not verdict.ok:
        return Outcome("parses", FAIL, verdict.error)
    if not verdict.checked:
        # No validator for this extension. Unknown is not verified. 7 of 72 real
        # calls land here (.md, .ts, .tsx).
        return Outcome("parses", SKIP, f"no validator for {path.rsplit('.', 1)[-1]}")
    return Outcome("parses", PASS)


def check_anchor_unique(args: dict, pre: Optional[str]) -> Outcome:
    """A patch anchor must match exactly once.

    Zero matches and two matches are different bugs and are reported as such.
    Two matches is the dangerous one: the tool's fuzzy matcher picks one
    silently, so the edit can land in the wrong place and still report success.
    """
    old = args.get("old_string")
    if old is None:
        return Outcome("anchor_unique", SKIP, "not a patch")
    if pre is None:
        return Outcome("anchor_unique", SKIP, "pre-image not recovered")
    if args.get("replace_all"):
        return Outcome("anchor_unique", SKIP, "replace_all requested")
    count = pre.count(old)
    if count == 0:
        return Outcome("anchor_unique", FAIL, "anchor not found; patch cannot apply")
    if count > 1:
        return Outcome(
            "anchor_unique",
            FAIL,
            f"anchor matches {count} times; the edit may land in the wrong one",
        )
    return Outcome("anchor_unique", PASS)


def check_confined(args: dict, pre: Optional[str], actual: Optional[str]) -> Outcome:
    """Only the intended region changed, compared byte-exact.

    Not a diff heuristic. Measured on the 22 real returned diffs, the ratio of
    diff lines to anchor lines ranges from 0.27 to 7.00 purely from context
    padding, so any threshold on it is noise. A byte comparison has no threshold
    and therefore no false positives.
    """
    if actual is None or pre is None:
        return Outcome("confined", SKIP, "no observed post-image to compare")
    expected = post_image(args, pre)
    if expected is None:
        return Outcome("confined", SKIP, "expected post-image not computable")
    if actual != expected:
        return Outcome(
            "confined", FAIL, "the file changed outside the replaced region"
        )
    return Outcome("confined", PASS)


def check_idempotent(args: dict) -> Outcome:
    """Applying the same edit twice must not duplicate the region.

    When ``old_string`` survives inside ``new_string``, a retry after a timeout
    re-matches and nests or duplicates the block. Measured: 2 of 25 real patches
    do this, and both are in ``scripts/whatsapp-bridge/`` -- the same directory
    as the 42-hour outage. Both would pass every parse check ever written.
    """
    old, new = args.get("old_string"), args.get("new_string")
    if old is None or new is None:
        return Outcome("idempotent", SKIP, "not a patch")
    if old and old in new:
        return Outcome(
            "idempotent",
            FAIL,
            "old_string survives inside new_string; a retry duplicates the region",
        )
    return Outcome("idempotent", PASS)


def check_no_escaped_delimiter(args: dict) -> Outcome:
    """No backslash-escaped triple-quote survived into the file body.

    This is the measured root cause of the only write in the corpus that
    actually broke, and the only check here that still fires on ``.md``,
    ``.ts`` and ``.tsx``, where no parser exists.
    """
    body = args.get("content") or args.get("new_string") or ""
    hits = len(_ESCAPED_TRIPLE.findall(body))
    if hits:
        return Outcome(
            "no_escaped_delimiter",
            FAIL,
            f"{hits} escaped triple-quote delimiters in the file body",
        )
    return Outcome("no_escaped_delimiter", PASS)


def check_no_truncation(args: dict) -> Outcome:
    """No elision sentinel was written into a real file."""
    body = args.get("content") or args.get("new_string") or ""
    if _TRUNCATION.search(body):
        return Outcome(
            "no_truncation", FAIL, "content ends in an elision sentinel"
        )
    return Outcome("no_truncation", PASS)


def check_fresh_read(context: dict) -> Outcome:
    """Do not overwrite a file whose last read was a partial view.

    Hermes already computes this and emits it as an advisory ``_warning`` while
    allowing the write. 11 of 72 real results carry it, across 6 files including
    ``scripts/whatsapp-bridge/bridge.js``. The oracle promotes the advisory to a
    verdict; it is a state check over the transcript, not a judgement.
    """
    if context.get("last_read_partial") is None:
        return Outcome("fresh_read", SKIP, "no prior read recorded")
    if context.get("last_read_partial"):
        return Outcome(
            "fresh_read",
            FAIL,
            "last read of this path was a partial view; re-read before overwriting",
        )
    return Outcome("fresh_read", PASS)


def grade(
    *,
    case_id: str,
    path: str,
    args: dict,
    pre: Optional[str] = None,
    actual: Optional[str] = None,
    context: Optional[dict] = None,
) -> Verdict:
    """Run every check against one edit."""
    ctx = context or {}
    verdict = Verdict(case_id=case_id, suite=SUITE_ID)
    verdict.outcomes = [
        check_no_truncation(args),
        check_no_escaped_delimiter(args),
        check_anchor_unique(args, pre),
        check_parses(path, args, pre),
        check_confined(args, pre, actual),
        check_idempotent(args),
        check_fresh_read(ctx),
    ]
    return verdict


def parse_arguments(raw: Any) -> dict:
    """Tool arguments, whether they arrived as a dict or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_LINE_PREFIX = re.compile(r"^\s*\d+\|", re.M)


def strip_line_numbers(content: str) -> str:
    """Undo ``read_file``'s ``N|`` gutter so the text can be used as a pre-image.

    Stripping this recovers a usable pre-image for 35 of 53 real patch
    occurrences, with the anchor matching exactly once in every one of them and
    zero ambiguous cases.
    """
    return _LINE_PREFIX.sub("", content)
