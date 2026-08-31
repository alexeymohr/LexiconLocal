"""Hybrid search: FTS5/BM25 fused with vector similarity, then static boosts.

Reciprocal rank fusion is used rather than score blending because BM25 scores
and vector distances are not on comparable scales, and RRF only needs the
orderings. The static boosts encode the ranking DESIGN.md §6.2 asks for:
curated notes above in-repo docs above transcripts above tool events.

Exact identifiers must never depend on embeddings (DESIGN.md §6.1), so the FTS
leg runs on every query and quoted/path-like/symbol-like terms are routed to it
verbatim.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbmod
from .chunk import KIND_PROSE, KIND_TOOL_EVENT
from .config import Config
from .embed import Embedder
from .projects import ProjectIndex, load_project_index

RRF_K = 60
CANDIDATES = 50

#: Candidates fetched per leg when a *broad* filter is supplied -- source_type,
#: kind, or a date range, but no project (amendment A1).
#:
#: These filters partition the corpus coarsely: `source_type=transcript` selects
#: most of it. An exact scan there would be a full table scan for no ranking
#: benefit, so the pool is enlarged instead and the filter still applied
#: afterwards. Sized by measurement, not by guess -- see the Phase 5 completion
#: notes. sqlite-vec caps KNN `k` at 4096, so this can grow but not without
#: bound.
#:
#: 1600 is where measurement lands. KNN cost plateaus there (k=50 57 ms, k=400
#: 89 ms, k=1600 141 ms, k=4096 139 ms) and it is the first size at which a
#: *narrow* source_type fills a page: `source_type=lexicon` returns 3 results at
#: 400, 7 at 800, and 10 at 1600. Worst measured end-to-end: 260 ms.
BROAD_CANDIDATES = 1600

#: Candidates fetched per leg when a *project* filter is supplied.
#:
#: A project is a small slice of the corpus (39-364 documents of 9,077), so both
#: legs are scoped to it and the vector leg becomes an exact scan over that
#: slice -- no `k` cap, and nothing can be starved by the global ranking. This
#: is the D1 fix proper: before it, a project filter was a post-filter over
#: a global top-50, and returned nothing at all for three natural questions
#: about a project with 39 indexed documents and 16 recorded decisions.
SCOPED_CANDIDATES = 50

#: Above this many chunks, a project stops being a small slice and the exact
#: scan stops being affordable.
#:
#: Retrieving a vector by rowid from the `vec0` table costs ~85 us regardless of
#: what is done with it -- a bare COUNT(*) over the join costs the same as the
#: distance query -- so an exact scan is linear in project size: 283 vectors
#: 22 ms, 3,875 vectors 300 ms, 7,722 vectors 685 ms. The last of those blows
#: the 500 ms bar on its own.
#:
#: 3,000 leaves ~255 ms for the scan inside a ~500 ms budget, and covers 82 of
#: the 88 projects that have chunks. The six that exceed it are the corpus's
#: giants (66k, 14k, 9.6k, 6.1k, 3.6k and 3.4k chunks on the corpus this was
#: calibrated against) -- and those are precisely
#: the projects a global candidate pool never starves: at k=1600 each still
#: contributes between 4 and 653 chunks. So they fall back to the broad path,
#: while the lexical leg stays exactly scoped either way and on its own
#: guarantees a full page of results.
EXACT_SCAN_MAX_CHUNKS = 3000

#: Static ranking boosts, multiplied into the fused RRF score.
#:
#: The spread has to be wide. RRF compresses everything into a narrow band --
#: with K=60, rank 1 scores 0.0164 and rank 20 scores 0.0123, barely a third
#: of a decade apart -- while a document matching in *both* legs effectively
#: doubles. A 1.5x boost therefore loses to "matched twice", which is what the
#: first real index did: for a query answered by a curated note, that project's
#: mission docs landed at ranks 6-15 behind five transcripts, inverting the
#: hierarchy DESIGN.md 6.2 asks for. These values are chosen so the tiers
#: actually separate rather than merely tilt.
BOOSTS: dict[str, float] = {
    "lexicon": 3.0,          # curated Lexicon notes -- the distilled layer
    "codex-memory": 2.4,     # Codex's own distilled memory store
    "claude-memory": 2.4,    # Claude's distilled memory of the user
    "claude-project": 2.0,   # curated project briefs and attached docs
    "repo-doc:top": 2.0,     # repo root or docs/ -- overviews, mission briefs
    "repo-doc:deep": 0.7,    # buried inside a repo tree
    "archive-doc": 1.1,      # sidecar .md/.txt beside transcripts
    "transcript:prose": 1.0,
    "transcript:tool_event": 0.5,
    "chatgpt:abandoned": 0.3,  # abandoned edit branches, never above the real thread
}

_QUOTED = re.compile(r'"([^"]+)"')
_IDENTIFIERISH = re.compile(r"^[\w./\\-]*[/.\\_][\w./\\-]*$")

#: Reciprocal rank fusion is purely ordinal: the top hit for *any* query scores
#: the same whether it is a perfect match or the least-bad of fifty irrelevant
#: chunks. Left alone that means a query about a topic the corpus has never
#: covered still returns confident-looking scores -- measured on this index, an
#: absent topic scored 0.0503 while a genuinely relevant one scored 0.0308.
#:
#: Vector distance, unlike rank, is absolute. Measured over this corpus:
#: present topics land at 0.76-0.82, absent topics at 0.96-0.99. These bounds
#: turn that into a confidence factor. Lexical term coverage does the same job
#: for queries the vector leg misses entirely (exact identifiers).
#:
#: **The metric is L2, not cosine.** ``chunk_vecs`` is declared as
#: ``vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[768])`` with no distance
#: specified, so sqlite-vec's KNN returns L2 -- these constants were calibrated
#: empirically against that, and are correct. Only their name was wrong, which
#: was not harmless: writing ``vec_distance_cosine`` into a new query is the
#: obvious thing to do and returns roughly 0.34 where KNN returns 0.82 for the
#: same chunk. Every result would clamp to confidence 1.00 and the "we have
#: nothing" signal would die silently. For normalized vectors (which
#: nomic-embed-text returns) ``L2 = sqrt(2(1 - cos))``, so the two rank
#: identically -- it is the *scale* that differs, and confidence is a function
#: of scale. ``test_knn_distance_is_l2_not_cosine`` pins this.
VEC_CONFIDENT_L2 = 0.80
VEC_HOPELESS_L2 = 0.95

#: Superseded names, kept so an out-of-tree caller does not break silently.
VEC_CONFIDENT_DISTANCE = VEC_CONFIDENT_L2
VEC_HOPELESS_DISTANCE = VEC_HOPELESS_L2
#: Never scale a result to zero -- a weak hit is still worth showing, just not
#: worth showing confidently.
MIN_CONFIDENCE = 0.15

#: Above this, a result is worth treating as real coverage of the query.
#: Used only to explain the scale to agents, never as a filter.
CONFIDENCE_COVERED = 0.80

#: Below this *median* confidence across the returned results, the corpus
#: probably does not cover the query at all.
#:
#: The median, not the top result. A single document that happens to quote the
#: query wholesale scores 1.00 on lexical coverage alone and drags the top hit
#: with it -- and this corpus indexes its own repo, so every phrase written into
#: a doc or typed in an archived session becomes "present" within the hour. Two
#: of the four original absent probes had been compromised exactly that way.
#: The median survives one such spike; the maximum does not.
#:
#: Calibrated 2026-08-19 over ten uncontaminated absent probes and ten present
#: ones: absent medians reached 0.52, present medians bottomed at 0.72. 0.60
#: sits in that gap, deliberately nearer the absent side -- a false "we have
#: nothing" would send an agent away from material that exists, which is worse
#: than the status quo it replaces. `golden_queries.py` re-measures the
#: separation on every run, because an absolute number on a growing corpus
#: cannot be trusted to stay calibrated on its own.
CONFIDENCE_ABSENT_MEDIAN = 0.60

#: Said in a sentence rather than left implicit in a number, because the
#: judgement must not depend on the reader -- agent or human -- noticing a float
#: and knowing what it means. Defined here, beside the threshold that triggers
#: it, so the MCP server and the CLI cannot drift into two different warnings.
ABSENT_BANNER = (
    "LIKELY NOT COVERED: no strong match — the Lexicon probably holds nothing on "
    "this. Say so rather than stretching a weak result into an answer."
)

_TERM = re.compile(r"[A-Za-z0-9_]{3,}")


def _lexical_coverage(query: str, text: str) -> float:
    """Fraction of the query's distinct content terms present in *text*."""
    terms = {t.lower() for t in _TERM.findall(query)}
    if not terms:
        return 0.0
    low = text.lower()
    return sum(1 for t in terms if t in low) / len(terms)


#: How many candidate paths an ambiguous read lists. Enough to choose from,
#: bounded so that a one-character fragment cannot pour the corpus into an
#: agent's context window.
@dataclass(frozen=True)
class _Filters:
    """The active filters, represented once.

    They used to live as several near-duplicate predicates: one set scoping the
    project inside candidate generation, another re-checking everything after
    the fact. Two spellings of the same rule drift, and the after-the-fact copy
    was the only one `source_type`, `kind` and the date bounds ever had.
    """

    projects: list[str] | None = None
    source_type: str | None = None
    kind: str | None = None
    after: str | None = None
    before: str | None = None

    @property
    def any_active(self) -> bool:
        return any((self.projects, self.source_type, self.kind, self.after, self.before))

    @property
    def dated(self) -> bool:
        return bool(self.after or self.before)

    @property
    def needs_documents(self) -> bool:
        """Whether a query must join `documents` at all.

        `kind` lives on `chunks`. Joining `documents` to count tool-event chunks
        cost 115 ms; without the join the same count is 4 ms off a covering
        index. Join what the filter references, nothing more.
        """
        return bool(self.projects or self.source_type or self.dated)

    def sql(self) -> tuple[str, list]:
        """`(predicate, params)` — ``1=1`` when nothing is active."""
        parts, params = ["1=1"], []
        if self.projects:
            ph = ",".join("?" for _ in self.projects)
            parts.append(f"LOWER(d.project) IN ({ph})")
            params.extend(p.lower() for p in self.projects)
        if self.source_type:
            parts.append("d.source_type = ?")
            params.append(self.source_type)
        if self.kind:
            parts.append("c.kind = ?")
            params.append(self.kind)
        # An undated document has no date to compare, so it satisfies neither
        # bound. Comparing `COALESCE(doc_date,'')` made it fail `after` and pass
        # `before`, which is not a policy anyone chose.
        if self.dated:
            parts.append("d.doc_date IS NOT NULL AND d.doc_date <> ''")
        if self.after:
            parts.append("d.doc_date >= ?")
            params.append(self.after)
        if self.before:
            parts.append("d.doc_date <= ?")
            params.append(self.before)
        return " AND ".join(parts), params


AMBIGUOUS_READ_LIMIT = 10


def _escape_like(text: str) -> str:
    """Make ``%`` and ``_`` literal inside a LIKE pattern.

    A path is user input, not a pattern. Unescaped, ``_`` matches any character
    and ``%`` matches any run of them, so a path fragment containing either --
    and real paths contain underscores constantly -- silently matched documents
    the caller never named.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def median_confidence(results: list["Result"]) -> float:
    """Median confidence across results -- the corpus-coverage signal.

    See CONFIDENCE_ABSENT_MEDIAN for why this is a median and not a maximum.
    """
    if not results:
        return 0.0
    vals = sorted(r.confidence for r in results)
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _confidence(distance: float | None, coverage: float) -> float:
    """Absolute relevance in [MIN_CONFIDENCE, 1], independent of rank."""
    if distance is None:
        vec = 0.0
    else:
        span = VEC_HOPELESS_L2 - VEC_CONFIDENT_L2
        vec = (VEC_HOPELESS_L2 - distance) / span
        vec = max(0.0, min(1.0, vec))
    best = max(vec, coverage)
    return MIN_CONFIDENCE + (1.0 - MIN_CONFIDENCE) * best


@dataclass
class Result:
    path: str
    project: str | None
    source_type: str
    doc_date: str | None
    title: str | None
    score: float
    chunk_ord: int
    chunk_kind: str
    excerpt: str
    chunk_id: int
    confidence: float = 1.0
    matched_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "project": self.project,
            "source_type": self.source_type,
            "doc_date": self.doc_date,
            "title": self.title,
            "score": round(self.score, 5),
            "chunk_ord": self.chunk_ord,
            "chunk_kind": self.chunk_kind,
            "excerpt": self.excerpt,
            "chunk_id": self.chunk_id,
            "confidence": round(self.confidence, 3),
            "matched_by": self.matched_by,
        }


def boost_for(source_type: str, kind: str, path: str, extra_branch: str | None) -> float:
    if source_type == "lexicon":
        return BOOSTS["lexicon"]
    if source_type == "codex-memory":
        return BOOSTS["codex-memory"]
    if source_type == "claude-memory":
        return BOOSTS["claude-memory"]
    if source_type == "claude-project":
        return BOOSTS["claude-project"]
    if source_type == "archive-doc":
        return BOOSTS["archive-doc"]
    if source_type == "repo-doc":
        p = Path(path)
        parts = p.parts
        # "top" == repo root, or anywhere under a docs/ directory.
        depth_from_project = len(parts)
        is_docs = "docs" in parts or "specs" in parts
        shallow = depth_from_project <= 6
        return BOOSTS["repo-doc:top"] if (is_docs or shallow) else BOOSTS["repo-doc:deep"]
    if source_type == "transcript":
        if extra_branch == "abandoned":
            return BOOSTS["chatgpt:abandoned"]
        return BOOSTS["transcript:tool_event"] if kind == KIND_TOOL_EVENT else BOOSTS["transcript:prose"]
    return 1.0


def is_exact_query(query: str) -> bool:
    """True when the query names something exact rather than describing it.

    A quoted phrase, a path, or a symbol-like token means the user is after a
    specific string. Those must surface from the lexical leg even when the
    embedding model has no idea what they mean, and even when a static boost
    would otherwise let a fuzzy hit outrank them.
    """
    if _QUOTED.search(query):
        return True
    for tok in re.split(r"\s+", query.strip()):
        cleaned = tok.strip(".,;:!?()[]{}")
        if len(cleaned) > 2 and _IDENTIFIERISH.match(cleaned):
            return True
    return False


def _fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression that preserves exact identifiers.

    Quoted phrases stay phrases. Bare tokens that look like paths or symbols
    are quoted so FTS5's tokenizer cannot split them into meaningless pieces.
    """
    phrases = _QUOTED.findall(query)
    rest = _QUOTED.sub(" ", query)
    terms: list[str] = [f'"{p}"' for p in phrases if p.strip()]
    for tok in re.split(r"\s+", rest):
        tok = tok.strip()
        if not tok:
            continue
        cleaned = tok.strip(".,;:!?()[]{}")
        if not cleaned:
            continue
        if _IDENTIFIERISH.match(cleaned):
            terms.append(f'"{cleaned}"')
        else:
            safe = re.sub(r'["*]', "", cleaned)
            if safe:
                terms.append(safe)
    return " OR ".join(terms) if terms else '""'


class Searcher:
    def __init__(self, cfg: Config, embedder: Embedder | None = None) -> None:
        self.cfg = cfg
        self.embedder = embedder
        if not cfg.db_path.exists():
            raise FileNotFoundError(
                f"No index at {cfg.db_path}. Run `lexicon index --full` first."
            )
        self.conn = dbmod.connect(cfg.db_path, read_only=True)
        self.projects: ProjectIndex = load_project_index(cfg.index_md, cfg.historical_aliases)

    def close(self) -> None:
        self.conn.close()

    # ---- legs --------------------------------------------------------------

    def _fts_leg(
        self, query: str, limit: int, filters: "_Filters | None" = None
    ) -> list[tuple[int, int]]:
        """BM25 candidates, scoped to *filters* before the ranking and the LIMIT.

        Scoping here rather than afterwards is the whole point: a filtered query
        used to take the best `limit` rows of the entire corpus and then discard
        the ones that did not match, so anything below that cutoff could not be
        returned however well it fitted the filter.
        """
        expr = _fts_query(query)
        filters = filters or _Filters()
        try:
            if filters.any_active:
                pred, params = filters.sql()
                join = "JOIN documents d ON d.id = c.doc_id" if filters.needs_documents else ""
                rows = self.conn.execute(
                    f"""
                    SELECT f.rowid AS rowid
                    FROM chunks_fts f
                    JOIN chunks c    ON c.id = f.rowid
                    {join}
                    WHERE chunks_fts MATCH ? AND {pred}
                    ORDER BY bm25(chunks_fts) LIMIT ?
                    """,
                    (expr, *params, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts) LIMIT ?",
                    (expr, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(r["rowid"]), i) for i, r in enumerate(rows)]

    def _eligible_chunk_count(self, filters: "_Filters") -> int:
        """How many chunks the filter admits — the selectivity decision.

        Joins `documents` only when the filter references it. With the join, a
        `kind` count measured 115 ms; without, 4 ms off a covering index.
        """
        pred, params = filters.sql()
        join = "JOIN documents d ON d.id = c.doc_id" if filters.needs_documents else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM chunks c {join} WHERE {pred}", params
        ).fetchone()
        return int(row["n"]) if row else 0

    def _project_chunk_count(self, projects: list[str]) -> int:
        """How many chunks a project holds, without touching the vector table.

        Deliberately counts `chunks`, not the join to `chunk_vecs`: the join is
        the expensive thing being decided about (a bare COUNT over it costs the
        same ~85 us per row as the distance query itself), while this probe
        measures 0.0-0.3 ms. Chunk count is also an over-estimate of vector
        count -- tool-event chunks are never embedded -- so the decision errs
        toward the cheaper path.
        """
        ph = ",".join("?" for _ in projects)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM chunks c JOIN documents d ON d.id = c.doc_id "
            f"WHERE LOWER(d.project) IN ({ph})",
            [p.lower() for p in projects],
        ).fetchone()
        return int(row["n"]) if row else 0

    def _query_vector(self, query: str) -> bytes | None:
        if self.embedder is None:
            return None
        model = dbmod.get_meta(self.conn, "embed_model")
        dims = dbmod.get_meta(self.conn, "embed_dims")
        if model:
            dbmod.check_embed_compatibility(self.conn, self.embedder.model, int(dims or 0))
        try:
            vec = self.embedder.embed([query])[0]
        except Exception:  # noqa: BLE001 - lexical results still stand alone
            return None
        return dbmod.serialize_f32(vec)

    def _vec_leg(
        self, query: str, limit: int, filters: "_Filters | None" = None
    ) -> list[tuple[int, int, float]]:
        """Nearest chunks, either by KNN over everything or exactly within a project.

        The scoped branch computes the distance directly rather than using the
        `embedding MATCH ... AND k = ?` KNN, because our `vec0` table declares
        no metadata columns and so cannot be pre-filtered -- a KNN would have to
        take the global top-k and hope the project appears in it, which is the
        very failure being fixed. An exact scan over one project's vectors has
        no `k` cap and cannot starve.

        `vec_distance_l2` and not `vec_distance_cosine`: the KNN branch returns
        L2, and the two legs must produce distances on the same scale or the
        confidence factor built on them means different things in each. See D4.
        """
        q = self._query_vector(query)
        if q is None:
            return []
        try:
            if filters is not None and filters.any_active:
                pred, params = filters.sql()
                join = "JOIN documents d ON d.id = c.doc_id" if filters.needs_documents else ""
                rows = self.conn.execute(
                    f"""
                    SELECT c.id AS chunk_id,
                           vec_distance_l2(v.embedding, ?) AS distance
                    FROM chunks c
                    {join}
                    JOIN chunk_vecs v ON v.chunk_id = c.id
                    WHERE {pred}
                    ORDER BY distance LIMIT ?
                    """,
                    (q, *params, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT chunk_id, distance FROM chunk_vecs "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (q, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(r["chunk_id"]), i, float(r["distance"])) for i, r in enumerate(rows)]

    # ---- search ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        source_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 10,
        kind: str | None = None,
    ) -> list[Result]:
        # Retrieval is scoped to the filter rather than filtered after the fact
        # (D1), and how it is scoped depends on how selective the filter is
        # (amendment A1).
        projects = self.projects.resolve(project) if project else None
        filters = _Filters(
            projects=projects, source_type=source_type, kind=kind,
            after=after, before=before,
        )
        if filters.any_active:
            # The lexical leg is always exactly scoped: it is cheap at any size
            # and on its own guarantees a full page, which is what makes the
            # vector fallback below safe.
            pool = SCOPED_CANDIDATES if projects else BROAD_CANDIDATES
            fts = self._fts_leg(query, pool, filters)
            if filters.kind == KIND_TOOL_EVENT:
                # Those chunks are FTS-only by design; a vector leg here can
                # only return rows the filter is about to discard.
                vec = []
            elif self._eligible_chunk_count(filters) <= EXACT_SCAN_MAX_CHUNKS:
                vec = self._vec_leg(query, SCOPED_CANDIDATES, filters)
            else:
                # Above the cutoff an exact scan stops being affordable, so the
                # vector leg stays global. Lexical starvation is fixed; a
                # vector-only match outside the global top-k still cannot
                # surface for a broad filter. Stated, not hidden.
                vec = self._vec_leg(query, BROAD_CANDIDATES)
        else:
            fts = self._fts_leg(query, CANDIDATES)
            vec = self._vec_leg(query, CANDIDATES)

        fused: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        for cid, rank in fts:
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            matched.setdefault(cid, []).append("fts")
        distances: dict[int, float] = {}
        for cid, rank, dist in vec:
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            matched.setdefault(cid, []).append("vector")
            distances[cid] = dist
        if not fused:
            return []

        placeholders = ",".join("?" for _ in fused)
        rows = self.conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.ord, c.kind, c.text,
                   d.path, d.project, d.source_type, d.doc_date, d.title, d.extra_json
            FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE c.id IN ({placeholders})
            """,
            list(fused.keys()),
        ).fetchall()

        results: list[Result] = []
        for r in rows:
            if source_type and r["source_type"] != source_type:
                continue
            if kind and r["kind"] != kind:
                continue
            # Defensive only -- the filter above is the mechanism now. An
            # undated document satisfies neither bound: `COALESCE(date,'')` made
            # it fail `after` and pass `before`, which nobody chose.
            if (after or before) and not r["doc_date"]:
                continue
            if after and r["doc_date"] < after:
                continue
            if before and r["doc_date"] > before:
                continue
            if projects is not None:
                have = (r["project"] or "").lower()
                if have not in {p.lower() for p in projects}:
                    continue
            branch = None
            try:
                import json as _json
                branch = (_json.loads(r["extra_json"] or "{}") or {}).get("branch")
            except ValueError:
                branch = None
            text = r["text"]
            base = fused[r["chunk_id"]]
            confidence = _confidence(
                distances.get(r["chunk_id"]), _lexical_coverage(query, text)
            )
            score = base * boost_for(r["source_type"], r["kind"], r["path"], branch) * confidence
            results.append(
                Result(
                    path=r["path"],
                    project=r["project"],
                    source_type=r["source_type"],
                    doc_date=r["doc_date"],
                    title=r["title"],
                    score=score,
                    chunk_ord=int(r["ord"]),
                    chunk_kind=r["kind"],
                    excerpt=text[:600] + ("..." if len(text) > 600 else ""),
                    chunk_id=int(r["chunk_id"]),
                    confidence=confidence,
                    matched_by=matched.get(r["chunk_id"], []),
                )
            )
        # One hit per document keeps a single verbose transcript from
        # monopolising the result list. Within a document, a chunk that
        # matched lexically beats one that only matched by vector -- otherwise
        # a fuzzy prose hit can hide the exact identifier match that lives in
        # a tool_event header, and exact identifiers must never depend on
        # embeddings (DESIGN.md 6.1).
        best: dict[str, Result] = {}
        for r in results:
            cur = best.get(r.path)
            if cur is None:
                best[r.path] = r
                continue
            key_new = ("fts" in r.matched_by, r.score)
            key_cur = ("fts" in cur.matched_by, cur.score)
            if key_new > key_cur:
                best[r.path] = r

        if is_exact_query(query):
            # The user named an exact string. Lexical hits come first, ordered
            # among themselves by score; semantic near-misses follow. Without
            # this, a pile of weak vector hits carrying a higher static boost
            # can push the one true match off the end of the list.
            deduped = sorted(
                best.values(),
                key=lambda x: ("fts" in x.matched_by, x.score),
                reverse=True,
            )
        else:
            deduped = sorted(best.values(), key=lambda x: x.score, reverse=True)
        return deduped[:limit]

    # ---- read --------------------------------------------------------------

    def read(self, path: str, chunk_ord: int | None = None, context_chunks: int = 2) -> dict:
        doc = self.conn.execute(
            "SELECT * FROM documents WHERE path=?", (path,)
        ).fetchone()
        if doc is None:
            # The fallback used to be `LIKE '%path%' LIMIT 1` with no ordering:
            # given several matches it returned whichever row SQLite happened to
            # reach first and said nothing. Silently reading the wrong document
            # is worse than refusing -- the caller cannot tell it happened, and
            # everything downstream inherits the mistake.
            rows = self.conn.execute(
                "SELECT * FROM documents WHERE path LIKE ? ESCAPE '\\' "
                "ORDER BY path LIMIT ?",
                (f"%{_escape_like(path)}%", AMBIGUOUS_READ_LIMIT + 1),
            ).fetchall()
            if not rows:
                return {"error": f"No indexed document matching {path!r}"}
            if len(rows) > 1:
                shown = [r["path"] for r in rows[:AMBIGUOUS_READ_LIMIT]]
                more = len(rows) > AMBIGUOUS_READ_LIMIT
                listing = "\n".join(f"  {c}" for c in shown)
                return {
                    "error": (
                        f"{path!r} matches more than one indexed document; refusing to "
                        f"guess. Re-read with one of these exact paths"
                        f"{f' (first {AMBIGUOUS_READ_LIMIT} of more)' if more else ''}:"
                        f"\n{listing}"
                    ),
                    "candidates": shown,
                }
            doc = rows[0]

        occ = self.conn.execute(
            """SELECT o.ord, c.text, c.kind FROM occurrences o
               JOIN chunks c ON c.content_hash = o.chunk_hash
               WHERE o.doc_id = ? ORDER BY o.ord""",
            (doc["id"],),
        ).fetchall()

        if chunk_ord is None:
            body = "\n\n".join(r["text"] for r in occ)
            truncated = len(body) > 20000
            return {
                "path": doc["path"],
                "project": doc["project"],
                "source_type": doc["source_type"],
                "doc_date": doc["doc_date"],
                "title": doc["title"],
                "chunk_count": len(occ),
                "text": body[:20000] + ("\n\n[...truncated; request a chunk_ord for more]" if truncated else ""),
            }

        lo, hi = chunk_ord - context_chunks, chunk_ord + context_chunks
        window = [r for r in occ if lo <= r["ord"] <= hi]
        return {
            "path": doc["path"],
            "project": doc["project"],
            "source_type": doc["source_type"],
            "doc_date": doc["doc_date"],
            "title": doc["title"],
            "chunk_count": len(occ),
            "chunk_ord": chunk_ord,
            "context_chunks": context_chunks,
            "text": "\n\n".join(r["text"] for r in window),
        }
