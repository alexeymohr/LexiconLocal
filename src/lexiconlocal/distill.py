"""Which projects have raw material but no distilled notes, worst first.

DESIGN.md §7 makes distillation lazy on purpose: a project stays
raw-archive-only until it is touched again, and stays semantically searchable
the whole time. That is the right default and this module does not change it.

What it changes is visibility. Surveyed 2026-08-19, 16 of 54 repos had curated
notes and 35 had only raw material -- and nothing anywhere said so. An agent
landing in one of those 35 can *find* things but has to read session
transcripts to learn them, which is the difference between retrieval and
orientation. A backlog turns an implicit gap into a ranked list someone can
decide about.

No target is proposed for how many to distil. That is the operator's call about
where the knowledge is worth the pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pathlib import Path

from . import db as dbmod
from .config import Config
from .projects import load_project_index

#: Half-life, in days, of a project's claim on attention.
#:
#: Volume alone ranks dead giants above live work: an abandoned repo with 9,000
#: archived chunks is not more worth distilling than an active one with 300.
#: Ninety days means a project touched last week outranks an equal-sized one
#: untouched for a quarter, without ever driving an old project's score to zero.
RECENCY_HALF_LIFE_DAYS = 90.0

#: Sources that constitute *distilled* knowledge rather than raw material. A
#: project whose only documents are transcripts has nothing anyone has read.
DISTILLED_SOURCES = {"lexicon", "codex-memory", "claude-memory", "claude-project"}


@dataclass
class BacklogEntry:
    project: str
    documents: int
    chunks: int
    last_activity: str | None
    repo_docs: int
    transcripts: int
    score: float

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "documents": self.documents,
            "chunks": self.chunks,
            "last_activity": self.last_activity,
            "repo_docs": self.repo_docs,
            "transcripts": self.transcripts,
            "score": round(self.score, 1),
        }


def _days_since(iso: str | None) -> float:
    if not iso:
        return 3650.0
    try:
        t = time.mktime(time.strptime(iso[:10], "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return 3650.0
    return max(0.0, (time.time() - t) / 86400.0)


def project_note_dirs(lexicon_root: Path) -> list[Path]:
    """Curated `projects/<name>/` directories.

    Duplicated from `web.notes` rather than imported: `web` is the presentation
    layer and importing it from here inverts the dependency -- and did in fact
    produce a circular import through `web.dashboard`.
    """
    base = lexicon_root / "projects"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def distilled_projects(cfg: Config) -> set[str]:
    """Projects that already have `projects/<name>/` notes, plus their aliases.

    Alias resolution matters here: a project indexed under a historical name
    may be distilled under its current one. Without this it
    would appear in the backlog forever, and the backlog would be wrong in the
    one way that makes people stop reading it.
    """
    index = load_project_index(cfg.index_md, cfg.historical_aliases)
    have: set[str] = set()
    for pdir in project_note_dirs(cfg.lexicon_root):
        have.add(pdir.name.lower())
        for alias in index.resolve(pdir.name):
            have.add(alias.lower())
    return have


def distillation_backlog(cfg: Config, limit: int | None = None) -> list[BacklogEntry]:
    conn = dbmod.connect(cfg.db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT d.project                                   AS project,
                   COUNT(DISTINCT d.id)                        AS documents,
                   COUNT(c.id)                                 AS chunks,
                   MAX(d.doc_date)                             AS last_activity,
                   -- COUNT(DISTINCT ... CASE) and not SUM(): the join to
                   -- chunks multiplies each document by its chunk count, so a
                   -- SUM here counts chunks while calling them documents.
                   COUNT(DISTINCT CASE WHEN d.source_type = 'repo-doc'
                                       THEN d.id END)          AS repo_docs,
                   COUNT(DISTINCT CASE WHEN d.source_type = 'transcript'
                                       THEN d.id END)          AS transcripts,
                   COUNT(DISTINCT CASE WHEN d.source_type IN ({distilled})
                                       THEN d.id END)          AS distilled
            FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id
            WHERE d.project IS NOT NULL AND d.project <> ''
            GROUP BY d.project
            """.format(distilled=", ".join(f"'{s}'" for s in sorted(DISTILLED_SOURCES)))
        ).fetchall()
    finally:
        conn.close()

    have = distilled_projects(cfg)
    out: list[BacklogEntry] = []
    for r in rows:
        name = r["project"]
        if name.lower() in have or (r["distilled"] or 0) > 0:
            continue
        days = _days_since(r["last_activity"])
        score = (r["documents"] or 0) * (0.5 ** (days / RECENCY_HALF_LIFE_DAYS))
        out.append(BacklogEntry(
            project=name,
            documents=int(r["documents"] or 0),
            chunks=int(r["chunks"] or 0),
            last_activity=r["last_activity"],
            repo_docs=int(r["repo_docs"] or 0),
            transcripts=int(r["transcripts"] or 0),
            score=score,
        ))
    out.sort(key=lambda e: (-e.score, e.project))
    return out[:limit] if limit else out


DISTILL_PROMPT = """\
Distill this project's history into the Lexicon. Start with the repo's own
docs (PROJECT_OVERVIEW.md, CONTEXT.md, docs/ mission briefs) — link to them,
don't restate them. Then search lexicon_search and ~/Lexicon/archive/ for
everything related to {project}{aliases}. Create/update
projects/{project}/overview.md, decisions.md, and a backfill entry in log.md
summarizing the project's history: goals, evolution, key decisions and why,
failed approaches and why they failed, current state, open questions. Cite
archive sources. Flag anything uncertain rather than guessing. Then commit."""


def distill_prompt(cfg: Config, project: str) -> str:
    """DESIGN.md §7's prompt, with this project's real aliases filled in."""
    index = load_project_index(cfg.index_md, cfg.historical_aliases)
    others = [a for a in index.resolve(project) if a.lower() != project.lower()]
    aliases = f" (aliases: {', '.join(sorted(others))})" if others else ""
    return DISTILL_PROMPT.format(project=project, aliases=aliases)
