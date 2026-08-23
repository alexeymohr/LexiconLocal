"""Markdown document parser for in-place repo docs and curated Lexicon notes.

Files are read where they live and never copied (D-2026-08-18-02).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..chunk import KIND_PROSE, chunk_markdown, content_hash
from ..redact import redact
from ..walk import read_text_with_fallback
from .base import ParsedDoc

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def _title_of(text: str, path: Path) -> str:
    m = _H1.search(text)
    if m:
        return m.group(1).strip()
    return path.stem


def _doc_date(text: str, path: Path, mtime: float) -> str | None:
    m = _DATE_IN_NAME.search(path.name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # A leading `**Date:** 2026-08-18` or `Date: 2026-08-18` in the header.
    head = text[:2000]
    m2 = re.search(r"(?i)\bdate:?\*{0,2}\s*(20\d{2}-\d{2}-\d{2})", head)
    if m2:
        return m2.group(1)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()


def parse_markdown(
    path: Path,
    *,
    source_type: str,
    project: str | None,
    root: str | None,
) -> ParsedDoc | None:
    try:
        text, fallback = read_text_with_fallback(path)
        stat = path.stat()
    except OSError:
        return None
    if not text.strip():
        return None

    clean, kinds = redact(text)
    chunks = chunk_markdown(clean, KIND_PROSE, 0)
    if not chunks:
        return None

    return ParsedDoc(
        path=str(path),
        source_type=source_type,
        content_hash=content_hash(clean),
        chunks=chunks,
        project=project,
        root=root,
        doc_date=_doc_date(clean, path, stat.st_mtime),
        mtime=stat.st_mtime,
        size=stat.st_size,
        title=_title_of(clean, path),
        extra={"suffix": path.suffix.lower()},
        used_encoding_fallback=fallback,
        redactions=kinds,
    )


def parse_plain_text(
    path: Path,
    *,
    source_type: str,
    project: str | None,
    root: str | None,
    extra: dict | None = None,
) -> ParsedDoc | None:
    """Index a ``.txt`` attachment or similar as an ordinary document."""
    try:
        text, fallback = read_text_with_fallback(path)
        stat = path.stat()
    except OSError:
        return None
    if not text.strip():
        return None
    clean, kinds = redact(text)
    from ..chunk import chunk_plain

    chunks = chunk_plain(clean, KIND_PROSE, 0)
    if not chunks:
        return None
    return ParsedDoc(
        path=str(path),
        source_type=source_type,
        content_hash=content_hash(clean),
        chunks=chunks,
        project=project,
        root=root,
        doc_date=_doc_date(clean, path, stat.st_mtime),
        mtime=stat.st_mtime,
        size=stat.st_size,
        title=path.stem,
        extra=extra or {},
        used_encoding_fallback=fallback,
        redactions=kinds,
    )
