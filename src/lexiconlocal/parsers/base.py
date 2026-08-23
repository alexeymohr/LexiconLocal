"""Shared parser output type."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..chunk import Chunk


@dataclass
class ParsedDoc:
    """One indexable document, already chunked.

    ``path`` is the identity used for incremental reindexing and for
    ``lexicon_read``. For session-grouped transcripts it is synthetic (a
    ``#session=`` suffix), because 229 Claude Code files hold only 57 sessions
    and the session -- not the file -- is the unit a human means.
    """

    path: str
    source_type: str
    content_hash: str
    chunks: list[Chunk]
    project: str | None = None
    root: str | None = None
    doc_date: str | None = None
    mtime: float | None = None
    size: int | None = None
    title: str | None = None
    extra: dict = field(default_factory=dict)
    used_encoding_fallback: bool = False
    redactions: list[str] = field(default_factory=list)
