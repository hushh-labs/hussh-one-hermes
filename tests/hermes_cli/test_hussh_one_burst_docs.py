# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""The burst design record has to still resolve, or it is not a record.

KPI 1.4 claims the husshone design docs are "durable in Hermes". Migrating the
files is only half of that: a document whose cross-references point into a
repository this one cannot see is present but not usable, and nothing about the
file itself says so.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "hussh-one"
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: The only links allowed not to resolve, and why.
#:
#: ``design.md`` is migrated **verbatim** from husshone, and its header points at
#: three paths in that repository's tree. Rewriting them would buy working links
#: at the cost of the verbatim guarantee, which is the reason the file is kept at
#: all. They stay broken, named in the migration README, and pinned here so the
#: exemption cannot quietly grow.
_KNOWN_BROKEN = {
    ("reference/xtreme-burst/design.md", "./customer/getting-started.md"),
    ("reference/xtreme-burst/design.md", "../provisioning/README.md"),
    ("reference/xtreme-burst/design.md", "./specs/README.md"),
}


def _relative_links():
    for md in sorted(_DOCS.rglob("*.md")):
        for _label, target in _LINK.findall(md.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = target.partition("#")[0]
            if path:
                yield md, target, (md.parent / path)


def test_every_relative_link_resolves_except_the_three_named_ones():
    broken = {
        (str(md.relative_to(_DOCS)).replace("\\", "/"), target)
        for md, target, resolved in _relative_links()
        if not resolved.exists()
    }
    assert broken == _KNOWN_BROKEN, (
        "unresolvable links changed.\n"
        f"  new: {sorted(broken - _KNOWN_BROKEN)}\n"
        f"  fixed (drop from _KNOWN_BROKEN): {sorted(_KNOWN_BROKEN - broken)}"
    )


def test_the_migration_readme_names_the_broken_links_it_permits():
    """An exemption a reader cannot find is indistinguishable from a bug."""
    readme = (_DOCS / "reference" / "xtreme-burst" / "README.md").read_text(encoding="utf-8")
    for _doc, target in _KNOWN_BROKEN:
        assert target in readme, f"{target} is exempted in code but not explained to a reader"


def test_the_link_checker_is_actually_looking_at_something():
    """Guard against the whole test passing because the glob found nothing."""
    links = list(_relative_links())
    assert len(links) > 100, f"only {len(links)} relative links found — the scan has drifted"
