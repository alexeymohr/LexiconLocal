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


def _renames(cfg: Config) -> dict[str, set[str]]:
    """Current name (lowered) -> the historical names that mean the same project."""
    out: dict[str, set[str]] = {}
    for old, current in (cfg.historical_aliases or {}).items():
        out.setdefault(str(current).strip().lower(), set()).add(str(old).strip().lower())
    return out


def distilled_projects(cfg: Config) -> set[str]:
    """Projects that already have `projects/<name>/` notes, plus their old names.

    Only *declared renames* are folded in: the `historical_aliases` table in
    `config.yaml`, which exists precisely to record "this is the same project
    under a name it used to have". A project indexed under an old name really
    is distilled once its current name has notes, and listing it forever would
    make the backlog wrong in the one way that makes people stop reading it.

    What is deliberately **not** folded in is `INDEX.md` alias resolution. That
    map also carries *lineage* -- predecessors, spikes, POCs, false starts and
    sub-missions -- named in a family row's prose because they are related
    work, not the same work. Resolving through it let one distilled sibling
    swallow its whole family, and did so silently: projects with real material
    simply never appeared, including the spikes that sit between a rebuild and
    the thing it replaced.

    That is backwards for how the material is actually produced. A rebuild from
    scratch is a different body of learning from its predecessor -- the reason
    it was rebuilt is itself the knowledge -- and a sub-mission spun out of a
    parent project has its own dead ends. Each earns its own notes. Where a
    judgement to merge two names is genuinely made, it is declared in
    `historical_aliases` and reported by :func:`alias_suppressions`.
    """
    renames = _renames(cfg)
    have: set[str] = set()
    for pdir in project_note_dirs(cfg.lexicon_root):
        name = pdir.name.lower()
        have.add(name)
        have |= renames.get(name, set())
    return have


@dataclass
class Suppression:
    """A project the backlog omits, and the declared rename that omits it."""

    project: str
    documents: int
    distilled_as: str

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "documents": self.documents,
            "distilled_as": self.distilled_as,
        }


def alias_suppressions(cfg: Config) -> list[Suppression]:
    """Projects with indexed material that the backlog omits as declared renames.

    Suppression is a judgement -- someone decided two names are one project --
    and a judgement that cannot be seen cannot be corrected. This is the
    backlog's version of the rule that `report` must distinguish "nothing new"
    from "importer broke": a wrong entry in `historical_aliases` would
    otherwise show up as a project that simply never appears, which is
    indistinguishable from having nothing to say about it.

    The list is short by construction -- it can only contain declared renames
    -- so it stays checkable at a glance.
    """
    renames = {
        str(old).strip().lower(): str(current).strip()
        for old, current in (cfg.historical_aliases or {}).items()
    }
    own = {p.name.lower() for p in project_note_dirs(cfg.lexicon_root)}
    have = distilled_projects(cfg)

    conn = dbmod.connect(cfg.db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT project, COUNT(DISTINCT id) AS documents
            FROM documents
            WHERE project IS NOT NULL AND project <> ''
            GROUP BY project
            """
        ).fetchall()
    finally:
        conn.close()

    out = [
        Suppression(
            project=r["project"],
            documents=int(r["documents"] or 0),
            distilled_as=renames.get(r["project"].lower(), r["project"]),
        )
        for r in rows
        if r["project"].lower() not in own and r["project"].lower() in have
    ]
    out.sort(key=lambda s: (-s.documents, s.project))
    return out


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
