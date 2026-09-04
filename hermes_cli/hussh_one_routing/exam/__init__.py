# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The exam: what Puppy One is actually asked to do, graded deterministically.

Built from this owner's real session dumps rather than invented tasks, because
an exam that does not match the job measures something nobody does. Mining 122
dumps gave 1,503 tool calls with a distribution that reshaped the whole thing:

    terminal 877 (58%) | read_file 186 | search_files 147
    write_file 86 | patch 50 | execute_code 47

and, inside those terminal calls, ``python3`` is the head verb of **47.2%**.
The dominant job is not shell. It is authoring a Python program and wrapping it
in a shell call.

Four suites, scored separately and never averaged. A mean across them is what
hides "good at picking tools, unusable for code".

Every suite returns PASS / FAIL / **SKIP** rather than a boolean. Collapsing
SKIP into PASS restores exactly the silence the write guard was built to remove:
7 of 72 real file edits are ``.md``/``.ts``/``.tsx``, which no validator covers,
and calling those "passed" would be a lie of omission at 10% of the corpus.
"""

from __future__ import annotations

__all__ = [
    "COMPACTED",
    "HARNESS",
    "SUITES",
    "Case",
    "Outcome",
    "Verdict",
    "PASS",
    "FAIL",
    "SKIP",
]

from .model import (
    COMPACTED,
    FAIL,
    HARNESS,
    PASS,
    SKIP,
    Case,
    Outcome,
    Verdict,
)

SUITES = ("terminal", "tool_select", "file_edit", "long_context")
