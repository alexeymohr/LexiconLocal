"""Configuration loading for the Lexicon indexer.

The only source of truth is ``~/Lexicon/config.yaml``. Nothing here holds state
that could not be reconstructed from that file plus the Lexicon tree.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/Lexicon/config.yaml").expanduser()

# Extensions indexed from repo source roots and the curated Lexicon notes.
MARKDOWN_SUFFIXES = {".md", ".markdown"}


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


@dataclass
class SourceRoot:
    path: Path
    type: str
    #: Short stable label used to qualify project names. The same directory name
    #: exists under two different roots with different content, so a project
    #: identity is (root_label, dir_name), never dir_name alone.
    label: str


@dataclass
class Config:
    path: Path
    schema_version: int
    lexicon_root: Path
    source_roots: list[SourceRoot]
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    never_index: list[Path] = field(default_factory=list)
    #: Historical project names -> the current project they belong to, e.g. a
    #: repo that was renamed. Old transcripts recorded under the old name stay
    #: findable when filtering on the new one. Keys are matched case-insensitively.
    historical_aliases: dict[str, str] = field(default_factory=dict)

    # ---- derived locations -------------------------------------------------

    @property
    def index_dir(self) -> Path:
        return self.lexicon_root / "index"

    @property
    def db_path(self) -> Path:
        return self.index_dir / "lexicon.sqlite"

    @property
    def archive_dir(self) -> Path:
        return self.lexicon_root / "archive"

    @property
    def notes_dirs(self) -> list[Path]:
        """Curated Lexicon notes (DESIGN.md ingest root 1)."""
        return [self.lexicon_root / "projects", self.lexicon_root / "topics"]

    @property
    def index_md(self) -> Path:
        return self.lexicon_root / "INDEX.md"

    # ---- exclusion rules ---------------------------------------------------

    def is_excluded_dir(self, name: str) -> bool:
        """Match a single directory *name* (not a path) at any depth."""
        return any(fnmatch.fnmatch(name, pat) for pat in self.exclude_dirs)

    def is_excluded_file(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.exclude_files)

    def is_never_indexed(self, path: Path) -> bool:
        """True if *path* lies inside any ``never_index`` root (e.g. private/)."""
        rp = path.resolve() if path.exists() else path
        for banned in self.never_index:
            try:
                rp.relative_to(banned)
                return True
            except ValueError:
                continue
        return False


def load_config(path: Path | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Lexicon config not found at {cfg_path}. "
            "Phase 1 creates this; the indexer will not guess a layout."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    lexicon_root = _expand(raw.get("lexicon_root", "~/Lexicon"))

    roots: list[SourceRoot] = []
    used_labels: set[str] = set()
    for entry in raw.get("source_roots") or []:
        p = _expand(entry["path"])
        label = p.name.lower().replace(" ", "-")
        # ~/Documents/Claude/Projects -> "projects" collides conceptually with
        # the Lexicon's own projects/; disambiguate by parent when needed.
        if label in used_labels or label == "projects":
            label = f"{p.parent.name.lower()}-{label}"
        used_labels.add(label)
        roots.append(SourceRoot(path=p, type=entry.get("type", "repos"), label=label))

    return Config(
        path=cfg_path,
        schema_version=int(raw.get("schema_version", 1)),
        lexicon_root=lexicon_root,
        source_roots=roots,
        exclude_dirs=list(raw.get("exclude_dirs") or []),
        exclude_files=list(raw.get("exclude_files") or []),
        never_index=[_expand(p) for p in (raw.get("never_index") or [])],
        historical_aliases={
            str(k).strip().lower(): str(v).strip()
            for k, v in (raw.get("historical_aliases") or {}).items()
            if str(k).strip() and str(v).strip()
        },
    )
