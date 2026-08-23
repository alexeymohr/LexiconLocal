"""JSON endpoints: thin wrappers over the modules that already do the work.

Deliberately thin. `Searcher` decides relevance, `ProjectIndex` resolves
aliases, `report.health` measures the index; if any of them is wrong, the fix
belongs there and every surface -- CLI, MCP, web -- gets it at once. The only
logic that lives here is the part that is genuinely about serving: admitting a
path, choosing a rendering for a document, and shaping a result for a client.

Every handler returns ``(status, payload)``. Nothing raises to the socket.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..search import Searcher, is_exact_query
from . import notes, paths as pathmod
from .dashboard import build_dashboard
from .render import render_markdown, render_plain, render_transcript

MAX_LIMIT = 50
DEFAULT_LIMIT = 20

#: Suffixes rendered as Markdown; everything else admitted is shown verbatim.
_MARKDOWN_SUFFIXES = {".md", ".markdown"}


def _base_path(raw: str) -> str:
    """The filesystem part of a document path.

    Transcript documents are keyed as ``<archive dir>#session=<uuid>`` or
    ``#conversation=<id>`` -- a real directory plus a synthetic fragment. The
    fragment is not part of any path, so admission is decided on the prefix.
    """
    return raw.split("#", 1)[0]


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------

def search(cfg: Config, searcher: Searcher, q: dict) -> tuple[int, dict]:
    query = (q.get("q") or "").strip()
    if not query:
        return 400, {"error": "q is required"}

    exact = q.get("exact") in ("1", "true", "yes", "on")
    effective = query
    if exact and '"' not in query:
        # `is_exact_query` already routes quoted phrases to lexical-first
        # ranking, so an explicit exact request is expressed the same way
        # rather than through a second, divergent code path.
        effective = f'"{query}"'

    try:
        results = searcher.search(
            effective,
            project=(q.get("project") or None),
            source_type=(q.get("source_type") or None),
            after=(q.get("after") or None),
            before=(q.get("before") or None),
            limit=_int(q.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT),
            kind=(q.get("kind") or None),
        )
    except RuntimeError as e:
        # Raised when the stored embeddings and the live model disagree --
        # a real refusal, not an empty result set.
        return 409, {"error": str(e)}

    return 200, {
        "query": query,
        "effective_query": effective,
        "exact_mode": exact or is_exact_query(query),
        "vector_leg": searcher.embedder is not None,
        "count": len(results),
        "results": [_result(r) for r in results],
    }


def _trim_leading_partial_word(text: str, chunk_ord: int) -> str:
    """Drop a word the chunker cut in half at the start of an excerpt.

    Chunks are split on paragraph and line boundaries where possible but fall
    back to a hard character split, which leaves excerpts opening mid-word
    ("ode wrote" for "Code wrote", "vidence:" for "Evidence:").

    The test is a **leading lowercase letter**, which is deliberately
    conservative rather than clever. A chunk that begins at a clean boundary
    starts with a capital, a heading marker, a bullet, a pipe or a digit, so
    this cannot eat a real first word; the cost is that a fragment beginning
    with punctuation (a cut "`-file`") is left alone. Trimming is cosmetic --
    getting it wrong in the safe direction is free, in the other it hides
    content.
    """
    if chunk_ord <= 0 or not text:
        return text
    if not (text[0].isalpha() and text[0].islower()):
        return text
    head, sep, rest = text.partition(" ")
    if not sep or len(head) > 24:
        return text
    return "…" + rest


def _result(r) -> dict:
    d = r.as_dict()
    d["excerpt"] = _trim_leading_partial_word(d["excerpt"], r.chunk_ord)
    d["locator"] = f"{r.path}#chunk={r.chunk_ord}"
    d["is_transcript"] = "#" in r.path
    d["file_path"] = _base_path(r.path)
    return d


# ---------------------------------------------------------------------------
# /api/doc
# ---------------------------------------------------------------------------

def doc(cfg: Config, searcher: Searcher, q: dict) -> tuple[int, dict]:
    raw = (q.get("path") or "").strip()
    if not raw:
        return 400, {"error": "path is required"}

    # Admission first, and on the canonical path -- before the index is asked
    # anything (D-2026-08-19-08). A refusal is 404, never 403: 403 would
    # confirm the file is there.
    verdict = pathmod.admit(cfg, _base_path(raw), require_file=False)
    if not verdict:
        return 404, {"error": "not found"}

    indexed = searcher.read(raw) if _is_indexed(searcher, raw) else None

    file_verdict = pathmod.admit(cfg, _base_path(raw), require_file=True)
    if file_verdict and "#" not in raw:
        text, truncated = pathmod.read_text(file_verdict.path)
        is_md = file_verdict.path.suffix.lower() in _MARKDOWN_SUFFIXES
        return 200, {
            "path": str(file_verdict.path),
            "kind": "file",
            "format": "markdown" if is_md else "text",
            "html": render_markdown(text) if is_md else render_plain(text),
            "truncated": truncated,
            "bytes": file_verdict.path.stat().st_size,
            "indexed": bool(indexed and "error" not in indexed),
            "meta": _doc_meta(indexed),
            "source_path": str(file_verdict.path),
        }

    if indexed is None or "error" in indexed:
        return 404, {"error": "not found"}

    chunks = _chunks(searcher, raw)
    return 200, {
        "path": raw,
        "kind": "transcript",
        "format": "chunks",
        "html": render_transcript(chunks),
        "chunk_count": len(chunks),
        "truncated": False,
        "indexed": True,
        "meta": _doc_meta(indexed),
        # The archive file this document was parsed out of. Citing it is what
        # makes anything shown here checkable against the raw dump.
        "source_path": _base_path(raw),
    }


def _is_indexed(searcher: Searcher, path: str) -> bool:
    row = searcher.conn.execute(
        "SELECT 1 FROM documents WHERE path=? LIMIT 1", (path,)
    ).fetchone()
    return row is not None


def _doc_meta(indexed: dict | None) -> dict:
    if not indexed or "error" in indexed:
        return {}
    return {
        "title": indexed.get("title"),
        "project": indexed.get("project"),
        "source_type": indexed.get("source_type"),
        "doc_date": indexed.get("doc_date"),
        "chunk_count": indexed.get("chunk_count"),
    }


def _chunks(searcher: Searcher, path: str) -> list[dict]:
    rows = searcher.conn.execute(
        """SELECT o.ord, c.text, c.kind
           FROM occurrences o
           JOIN chunks c ON c.content_hash = o.chunk_hash
           JOIN documents d ON d.id = o.doc_id
           WHERE d.path = ? ORDER BY o.ord""",
        (path,),
    ).fetchall()
    return [{"ord": r["ord"], "text": r["text"], "kind": r["kind"]} for r in rows]


# ---------------------------------------------------------------------------
# /api/project/{name}
# ---------------------------------------------------------------------------

def project(cfg: Config, searcher: Searcher, name: str) -> tuple[int, dict]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return 404, {"error": "not found"}

    base = cfg.lexicon_root / "projects"
    pdir = base / name
    resolved_from = None
    if not pdir.is_dir():
        # Try the alias map: history is recorded under names that no longer
        # exist as directories (a renamed repo), and a link from an old
        # transcript must still land somewhere.
        for candidate in searcher.projects.resolve(name):
            alt = base / candidate
            if alt.is_dir():
                pdir, resolved_from = alt, name
                break
    if not pdir.is_dir():
        return 404, {"error": f"no curated notes for project {name!r}"}

    # Containment check even here: `name` is attacker-controlled and a
    # symlinked project directory must not become a way out of the Lexicon.
    verdict = pathmod.admit(cfg, str(pdir), require_file=False)
    if not verdict:
        return 404, {"error": "not found"}

    overview = pdir / "overview.md"
    decisions = notes.parse_decisions(pdir / "decisions.md")
    log = notes.parse_log(pdir / "log.md")

    overview_html = None
    if overview.exists() and pathmod.admit(cfg, str(overview)):
        text, _ = pathmod.read_text(overview)
        overview_html = render_markdown(text)

    return 200, {
        "name": pdir.name,
        "resolved_from": resolved_from,
        "aliases": searcher.projects.resolve(pdir.name),
        "path": str(pdir),
        "overview_html": overview_html,
        "overview_path": str(overview) if overview.exists() else None,
        "open_questions": notes.open_questions(overview),
        "decisions": [d.as_dict() for d in decisions],
        "log": [_log_entry(e, pdir) for e in log],
        "files": _project_files(pdir),
        "indexed_documents": _project_doc_count(searcher, pdir.name),
    }


def _log_entry(e, pdir: Path) -> dict:
    """A log entry on a project page is the content, not a caption.

    The dashboard feed shows a one-line summary because it is answering "what
    happened lately". A project page is where the entry is actually read, so
    the body is rendered as the Markdown it was written as -- a bullet list
    flattened into one grey run-on line is unreadable, and these entries are
    the most valuable prose in the Lexicon.
    """
    d = e.as_dict()
    d["path"] = str(pdir / "log.md")
    d["body_html"] = render_markdown(e.body) if e.body.strip() else ""
    return d


def _project_files(pdir: Path) -> list[dict]:
    out = []
    for p in sorted(pdir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        out.append({"name": p.name, "path": str(p), "bytes": p.stat().st_size})
    return out


def _project_doc_count(searcher: Searcher, name: str) -> int:
    names = searcher.projects.resolve(name) or [name]
    marks = ",".join("?" for _ in names)
    row = searcher.conn.execute(
        f"SELECT COUNT(*) n FROM documents WHERE LOWER(project) IN ({marks})",
        [n.lower() for n in names],
    ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# /api/dashboard
# ---------------------------------------------------------------------------

def dashboard(cfg: Config, searcher: Searcher, q: dict) -> tuple[int, dict]:
    return 200, build_dashboard(cfg)
