# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Refuse to land a file a local model just made unparseable.

The incident this exists for, 2026-08-28: an on-device model editing
`scripts/whatsapp-bridge/bridge_helpers.js` wrote a Python-style `#` comment
into a JavaScript file, mid-sentence inside a `//` block. `node --check` would
have rejected it in milliseconds. Nothing ran it. The bridge died two seconds
after every launch for 483 attempts and about 42 hours, and the cron jobs that
depended on it reported `ok` while losing every message they tried to deliver.

The lesson is not "use a bigger model". It is that **for structural validity, a
parser beats any judge**: it is faster, free, total for the failure class, and
cannot itself hallucinate. A model reviewing generated code might catch a stray
`#`; a parser cannot miss it.

So this is deliberately not an LLM check, and it is deliberately narrow. It
answers exactly one question -- does this text parse as the language its
filename claims -- and it answers it before the write is reported as successful.

Two honesty boundaries, stated because a guard that oversells itself is how the
next outage gets missed:

  * **Syntax is not correctness.** A local model can write perfectly parseable
    logic that is wrong, and this will pass it. This closes one failure class,
    not the category.
  * **It only covers writes that route through it.** A shell heredoc or `sed -i`
    from the terminal tool never touches this code. Coverage is a property of
    where it is installed, and claiming more than that is worse than claiming
    nothing.
"""

from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# A parse must never be the slow part of an edit loop.
VALIDATE_TIMEOUT_S = 10


@dataclass
class Verdict:
    """Whether the content parses, and why not when it does not."""

    ok: bool
    language: str = ""
    error: str = ""
    checked: bool = True

    @property
    def actionable(self) -> str:
        """The message handed back to the model so it can fix its own output."""
        if self.ok:
            return ""
        return f"{self.language} syntax error: {self.error}".strip()


def _skipped(reason: str) -> Verdict:
    # Not-checked is reported as its own state, never as a pass. A caller that
    # treats "no validator" as "valid" reintroduces exactly the silence this
    # module exists to remove.
    return Verdict(ok=True, checked=False, error=reason)


def _python(text: str) -> Verdict:
    try:
        ast.parse(text)
        return Verdict(ok=True, language="python")
    except SyntaxError as exc:
        return Verdict(
            ok=False,
            language="python",
            error=f"line {exc.lineno}: {exc.msg}",
        )


# Filenames that are conventionally JSONC -- JSON with comments -- and are
# genuinely valid that way. Running strict json.loads over these reports every
# one as broken, which is exactly how a guard earns a reputation for crying
# wolf and gets switched off. Found by running the guard over this repo: three
# tsconfig files, all correct, all flagged.
JSONC_FILENAMES = (
    "tsconfig",       # tsconfig.json, tsconfig.app.json, tsconfig.node.json
    "jsconfig",
    ".eslintrc.json",
    "devcontainer.json",
    "settings.json",
    "launch.json",
    "tasks.json",
)


def _is_jsonc(path: Path | str) -> bool:
    name = Path(path).name.casefold()
    return any(name.startswith(p) or name == p for p in JSONC_FILENAMES)


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments that sit outside string literals.

    Character-wise rather than by regex, because a regex cannot tell a comment
    from the same characters inside a string -- a URL like "https://x" would be
    truncated at the // and turn a valid file into an invalid one.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _json(text: str, path: Path | str = "") -> Verdict:
    try:
        json.loads(text)
        return Verdict(ok=True, language="json")
    except ValueError as strict_error:
        if _is_jsonc(path):
            try:
                json.loads(_strip_json_comments(text))
                return Verdict(ok=True, language="jsonc")
            except ValueError as exc:
                return Verdict(ok=False, language="jsonc", error=str(exc))
        return Verdict(ok=False, language="json", error=str(strict_error))


def _via_subprocess(
    text: str, *, language: str, suffix: str, argv: list[str]
) -> Verdict:
    """Parse by handing the content to a real parser on a temp file.

    A temp file, never the destination: validating in place would mean the
    broken content was already live for the duration of the check, which is the
    window this guard exists to close.
    """
    binary = argv[0]
    if not shutil.which(binary):
        return _skipped(f"{binary} not installed")
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
        temp_path = handle.name
    try:
        proc = subprocess.run(
            [*argv, temp_path],
            capture_output=True,
            text=True,
            timeout=VALIDATE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _skipped(f"{binary} failed to run: {type(exc).__name__}")
    finally:
        try:
            Path(temp_path).unlink()
        except OSError:
            pass

    if proc.returncode == 0:
        return Verdict(ok=True, language=language)
    detail = (proc.stderr or proc.stdout or "").strip()
    # Strip the temp path so the model sees its mistake, not our plumbing.
    detail = detail.replace(temp_path, "<file>")
    first = next((ln for ln in detail.splitlines() if ln.strip()), detail)
    return Verdict(ok=False, language=language, error=first[:400])


def _javascript(text: str) -> Verdict:
    return _via_subprocess(text, language="javascript", suffix=".js", argv=["node", "--check"])


def _shell(text: str) -> Verdict:
    return _via_subprocess(text, language="shell", suffix=".sh", argv=["bash", "-n"])


def _yaml(text: str) -> Verdict:
    try:
        import yaml  # noqa: PLC0415
    except Exception:
        return _skipped("pyyaml not installed")
    try:
        list(yaml.safe_load_all(text))
        return Verdict(ok=True, language="yaml")
    except Exception as exc:  # yaml raises several types
        return Verdict(ok=False, language="yaml", error=str(exc)[:400])


# Extension to parser. TypeScript is deliberately absent: `node --check` cannot
# parse TS, and running it anyway would reject every valid .ts file. A guard
# that blocks correct writes gets switched off, and then it protects nothing.
VALIDATORS: dict[str, Callable[[str], Verdict]] = {
    ".js": _javascript,
    ".mjs": _javascript,
    ".cjs": _javascript,
    ".py": _python,
    ".json": _json,
    ".yaml": _yaml,
    ".yml": _yaml,
    ".sh": _shell,
    ".bash": _shell,
}


def validate(path: Path | str, content: str) -> Verdict:
    """Does this content parse as the language its filename claims?

    Returns `checked=False` when no validator applies, so a caller can tell
    "verified good" from "nobody looked". Those must never collapse into one
    another.
    """
    suffix = Path(path).suffix.casefold()
    validator = VALIDATORS.get(suffix)
    if validator is None:
        return _skipped(f"no validator for {suffix or 'extension-less file'}")
    try:
        # JSON needs the filename to know whether comments are legal here.
        if validator is _json:
            return _json(content, path)
        return validator(content)
    except Exception as exc:  # noqa: BLE001
        # A crashing validator must not block a write. Failing closed here
        # would let a broken checker halt all editing, which is a worse outage
        # than the one being prevented.
        logger.debug("validator crashed for %s", path, exc_info=True)
        return _skipped(f"validator crashed: {type(exc).__name__}")


def guard_write(
    path: Path | str,
    content: str,
    *,
    write: Optional[Callable[[Path, str], None]] = None,
) -> Verdict:
    """Validate, then write only if it parses.

    Validate-then-write, never write-then-check-then-revert. A revert has to
    decide what to restore, and if the file changed underneath -- another
    process, the owner's editor -- restoring loses work that was never the
    guard's to touch. Not writing at all has no such failure mode.
    """
    verdict = validate(path, content)
    if not verdict.ok:
        logger.warning("refusing unparseable write to %s: %s", path, verdict.error)
        return verdict
    target = Path(path)
    if write is not None:
        write(target, content)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return verdict


def scan_paths(paths: list[Path | str]) -> list[tuple[str, Verdict]]:
    """Validate files already on disk. For auditing a tree after the fact."""
    results: list[tuple[str, Verdict]] = []
    for raw in paths:
        path = Path(raw)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            results.append((str(path), _skipped(f"unreadable: {type(exc).__name__}")))
            continue
        results.append((str(path), validate(path, content)))
    return results
