"""Project attribution and alias resolution.

Two jobs:

1. Map a filesystem path (a transcript's ``cwd``, a document's location) to a
   project name.
2. Resolve a user-supplied project filter through ``INDEX.md`` aliases and the
   operator's ``historical_aliases`` from ``config.yaml``. A repo that was
   renamed leaves old transcripts recorded under the old name; those must stay
   findable when filtering on the new one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectIndex:
    """Alias map built from INDEX.md plus the historical-name table."""

    #: lowercased alias -> canonical project name
    aliases: dict[str, str] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)

    def resolve(self, name: str) -> list[str]:
        """Return every project name a filter for *name* should match."""
        if not name:
            return []
        key = name.strip().lower()
        canonical = self.aliases.get(key, name.strip())
        out = {canonical}
        # Include every alias that maps to the same canonical project, so a
        # filter on the current name also picks up historical attributions.
        for alias, target in self.aliases.items():
            if target.lower() == canonical.lower():
                out.add(alias)
        return sorted(out)


_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|")
_TICKED = re.compile(r"`([^`]+)`")


def load_project_index(
    index_md: Path, historical_aliases: dict[str, str] | None = None
) -> ProjectIndex:
    """Parse INDEX.md for project names, families, and aliases.

    INDEX.md is hand-editable, so this parser is deliberately forgiving:
    anything it cannot make sense of is skipped rather than raising. A missing
    alias degrades ranking, not results.

    ``historical_aliases`` (from ``config.yaml``) is folded in first, so an
    INDEX.md row can refine it but a renamed repo is covered even before anyone
    writes the row.
    """
    idx = ProjectIndex()
    for hist, target in (historical_aliases or {}).items():
        idx.aliases[hist.strip().lower()] = target

    if not index_md.exists():
        return idx

    section = ""
    for line in index_md.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("##"):
            section = line.lower()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower() in {"project", "family", "---"}:
            continue
        if set(cells[0]) <= {"-", ":"}:
            continue

        if "families" in section or "alias groups" in section:
            # Family rows: members are backticked in column 2.
            members = _TICKED.findall(cells[1]) if len(cells) > 1 else []
            notes = cells[2] if len(cells) > 2 else ""
            for hist in _TICKED.findall(notes):
                if members:
                    idx.aliases.setdefault(hist.lower(), members[0])
            for m in members:
                idx.known.add(m)
            continue

        name = cells[0].strip()
        if not name or name.startswith("**"):
            continue
        idx.known.add(name)
        idx.aliases.setdefault(name.lower(), name)
        # Alias column is last in the project tables.
        if len(cells) >= 5:
            for raw in re.split(r"[,;]", cells[-1]):
                alias = raw.strip().strip("*_`")
                if not alias or alias in {"—", "-", ""}:
                    continue
                if alias.lower().startswith("duplicates"):
                    continue
                idx.aliases.setdefault(alias.lower(), name)
    return idx


#: Containers that hold a project checkout without being a configured source
#: root. A session run inside one of these is still work on that project, and
#: dropping it would silently orphan real history: the Phase 2 index found 20
#: such sessions across Codex worktrees and ~/codex_projects.
_CONTAINER_PATTERNS: tuple[tuple[str, int], ...] = (
    # (marker directory, how many path components after it to skip)
    (".codex/worktrees", 1),   # ~/.codex/worktrees/<hash>/<Project>
    ("codex_projects", 0),     # ~/codex_projects/<Project>
)


def project_from_container(path: Path) -> str | None:
    """Recover a project name from a known non-root container path."""
    parts = Path(path).parts
    for marker, skip in _CONTAINER_PATTERNS:
        marker_parts = tuple(marker.split("/"))
        n = len(marker_parts)
        for i in range(len(parts) - n + 1):
            if parts[i:i + n] == marker_parts:
                idx = i + n + skip
                if idx < len(parts):
                    return parts[idx]
    return None


def project_for_path(
    path: Path, roots: list[tuple[Path, str]], *, is_file: bool = False
) -> tuple[str | None, str | None]:
    """Attribute *path* to ``(project, root_label)`` using the source roots.

    A transcript's ``cwd`` is a directory, so ``<root>/SomeRepo`` is the
    SomeRepo project. Only a *file* sitting directly at a root belongs to
    the ``_loose`` pseudo-project.

    Returns ``(None, None)`` when the path lies outside every configured root.
    """
    try:
        rp = Path(path)
    except (TypeError, ValueError):
        return None, None
    for root, label in roots:
        try:
            rel = rp.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            return "_loose", label
        if is_file and len(parts) == 1:
            return "_loose", label
        return parts[0], label
    # Outside every configured root: try the known container shapes before
    # giving up and leaving the document unattributed.
    name = project_from_container(rp)
    if name:
        return name, "container"
    return None, None
