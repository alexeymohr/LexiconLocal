"""Filesystem walking: prune-first, never follows symlinks.

Phase 1 measured ~600k non-Markdown files remaining under ``~/programming``
even after pruning, so filtering after a full walk is not viable -- excluded
directories must never be descended into. It also found 57 symlinks, including
``.build/release`` self-references and Chrome ``Singleton*`` sockets, so
symlinks are never followed and never yielded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Config, MARKDOWN_SUFFIXES


@dataclass
class FoundFile:
    path: Path
    project: str
    root_label: str


def iter_files(
    cfg: Config,
    root: Path,
    root_label: str,
    *,
    suffixes: set[str] | None = None,
    project_of_top_level: bool = True,
    loose_project: str = "_loose",
) -> Iterator[FoundFile]:
    """Yield indexable files beneath *root*, pruning excluded directories.

    ``project`` is the top-level directory name under *root*. Files sitting
    loose at the root (a stray ``HANDOFF-*.md`` and friends) are
    attributed to the pseudo-project ``_loose`` rather than being dropped.
    """
    suffixes = suffixes if suffixes is not None else MARKDOWN_SUFFIXES
    if not root.exists():
        return

    def walk(directory: Path, project: str) -> Iterator[FoundFile]:
        try:
            entries = list(os.scandir(directory))
        except (PermissionError, OSError):
            return
        for entry in entries:
            # is_symlink() first: never follow, never yield.
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if cfg.is_excluded_dir(entry.name):
                    continue
                child = Path(entry.path)
                if cfg.is_never_indexed(child):
                    continue
                nxt = entry.name if (project_of_top_level and directory == root) else project
                yield from walk(child, nxt)
            else:
                if cfg.is_excluded_file(entry.name):
                    continue
                p = Path(entry.path)
                if suffixes and p.suffix.lower() not in suffixes:
                    continue
                if cfg.is_never_indexed(p):
                    continue
                yield FoundFile(path=p, project=project, root_label=root_label)

    yield from walk(root, loose_project)


def read_text_with_fallback(path: Path) -> tuple[str, bool]:
    """Read *path* as text. Returns ``(text, used_fallback)``.

    Phase 1 found 14 non-UTF-8 ``.md`` files, three of them ``CLAUDE.md``.
    A bare strict read would raise and, if unhandled, silently drop repo
    instruction files from the index -- so the fallback is recorded, not hidden.
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True
