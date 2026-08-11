# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, bounded extraction for already-materialized source files."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from tools.read_extract import ExtractionError, extract_document_bytes

from .contracts import ReadLimits


class SourceExtractionError(RuntimeError):
    pass


_PLAIN_TEXT = frozenset({".txt", ".md", ".markdown"})
_STRUCTURED_TEXT = frozenset({".json", ".csv"})
_DOCUMENTS = frozenset({".docx", ".xlsx", ".ipynb"})


def _bounded_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        data = os.read(fd, maximum + 1)
    finally:
        os.close(fd)
    if len(data) > maximum:
        raise SourceExtractionError("The source exceeds the bounded-read size limit.")
    return data


def extract_bounded_text(path: Path, limits: ReadLimits) -> tuple[str, bool]:
    limits.validate()
    suffix = path.suffix.lower()
    if suffix not in _PLAIN_TEXT | _STRUCTURED_TEXT | _DOCUMENTS:
        raise SourceExtractionError("This source type is metadata-only in V1.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SourceExtractionError("The source is unavailable.") from exc
    if size > limits.max_source_bytes:
        raise SourceExtractionError("The source exceeds the bounded-read size limit.")
    raw = _bounded_bytes(path, limits.max_source_bytes)
    return extract_bounded_bytes(raw, suffix=suffix, limits=limits)


def extract_bounded_bytes(
    raw: bytes, *, suffix: str, limits: ReadLimits
) -> tuple[str, bool]:
    limits.validate()
    suffix = suffix.lower()
    if suffix not in _PLAIN_TEXT | _STRUCTURED_TEXT | _DOCUMENTS:
        raise SourceExtractionError("This source type is metadata-only in V1.")
    if len(raw) > limits.max_source_bytes:
        raise SourceExtractionError("The source exceeds the bounded-read size limit.")
    try:
        if suffix in _DOCUMENTS:
            text = extract_document_bytes(raw, suffix)
        else:
            decoded = raw.decode("utf-8", errors="replace")
            if suffix == ".json":
                parsed = json.loads(decoded)
                text = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
            elif suffix == ".csv":
                rows = csv.reader(io.StringIO(decoded))
                text = "\n".join("\t".join(cell for cell in row) for row in rows)
            else:
                text = decoded
    except (ExtractionError, json.JSONDecodeError, csv.Error, OSError) as exc:
        raise SourceExtractionError("The source could not be extracted safely.") from exc
    truncated = len(text) > limits.max_text_chars
    return text[: limits.max_text_chars], truncated
