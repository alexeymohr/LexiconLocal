"""SQLite storage: FTS5 for lexical, sqlite-vec for vectors.

One file at ``~/Lexicon/index/lexicon.sqlite``. Entirely disposable -- every
row here is derivable from the Lexicon tree by ``lexicon index --full``.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import Iterable

import sqlite_vec

SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    source_type   TEXT NOT NULL,
    project       TEXT,
    root          TEXT,
    doc_date      TEXT,
    mtime         REAL,
    size          INTEGER,
    content_hash  TEXT NOT NULL,
    title         TEXT,
    extra_json    TEXT
);
CREATE INDEX IF NOT EXISTS documents_project  ON documents(project);
-- Project-scoped search matches case-insensitively, which defeats the plain
-- index above. Without this one SQLite drives the scoped vector query from the
-- 134k-row vector table inward instead of from the project outward, turning a
-- 22 ms scan into a 131 ms one (Phase 5 D1).
CREATE INDEX IF NOT EXISTS documents_project_lower ON documents(LOWER(project));
CREATE INDEX IF NOT EXISTS documents_source   ON documents(source_type);
CREATE INDEX IF NOT EXISTS documents_date     ON documents(doc_date);
CREATE INDEX IF NOT EXISTS documents_hash     ON documents(content_hash);

-- One row per UNIQUE chunk content. doc_id/ord record the first occurrence;
-- every occurrence (including that one) is listed in `occurrences`.
--
-- doc_id deliberately carries NO cascade. It records where the content was
-- first seen, not ownership: the same chunk is shared by every document with
-- identical text. A cascade here deletes a shared chunk when its first
-- document goes away, silently punching holes in every other document that
-- still references it through `occurrences`. Deletion is handled explicitly by
-- clear_document_chunks(), which checks occurrences first.
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES documents(id),
    ord           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    embedded      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_doc      ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS chunks_pending  ON chunks(embedded, kind);

CREATE TABLE IF NOT EXISTS occurrences (
    chunk_hash TEXT NOT NULL,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    PRIMARY KEY (chunk_hash, doc_id, ord)
);
CREATE INDEX IF NOT EXISTS occurrences_doc  ON occurrences(doc_id);
CREATE INDEX IF NOT EXISTS occurrences_hash ON occurrences(chunk_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id                INTEGER PRIMARY KEY,
    started           TEXT,
    finished          TEXT,
    mode              TEXT,
    files_seen        INTEGER DEFAULT 0,
    files_parsed      INTEGER DEFAULT 0,
    files_skipped     INTEGER DEFAULT 0,
    files_unchanged   INTEGER DEFAULT 0,
    docs_written      INTEGER DEFAULT 0,
    chunks_written    INTEGER DEFAULT 0,
    chunks_embedded   INTEGER DEFAULT 0,
    fallback_encoding TEXT,
    errors_json       TEXT,
    per_source_json   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def serialize_f32(vec: Iterable[float]) -> bytes:
    v = list(vec)
    return struct.pack(f"{len(v)}f", *v)


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection, embed_dims: int) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vecs USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{embed_dims}])"
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def check_embed_compatibility(conn: sqlite3.Connection, model: str, dims: int) -> None:
    """Refuse to serve queries against an index built with a different model.

    Silently mixing embedding spaces would degrade results in a way that looks
    like bad ranking rather than a configuration error, so this fails loud.
    """
    have_model = get_meta(conn, "embed_model")
    have_dims = get_meta(conn, "embed_dims")
    if have_model is None:
        return
    if have_model != model or (have_dims and int(have_dims) != dims):
        raise RuntimeError(
            f"Index was built with embed_model={have_model} dims={have_dims}, "
            f"but the configured model is {model} dims={dims}. "
            f"Vectors from different models are not comparable. "
            f"Run `lexicon index --full` to rebuild."
        )


def integrity_check(conn: sqlite3.Connection) -> dict:
    """Look for holes the index should never have.

    ``dangling_occurrences`` means a document references chunk text that is no
    longer stored: content present in the archive but unretrievable. That must
    be visible in `lexicon report`, not discovered by accident.
    """
    # One anti-join, not two. This scans 200k+ occurrences against the chunk
    # table and was the single most expensive query in the codebase (349 ms of
    # a 384 ms dashboard build); running it twice for two aggregates of the
    # same rows doubled that for nothing.
    dangling, affected = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT o.doc_id) FROM occurrences o "
        "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.content_hash = o.chunk_hash)"
    ).fetchone()
    orphan_vecs = conn.execute(
        "SELECT COUNT(*) FROM chunk_vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).fetchone()[0]
    return {
        "dangling_occurrences": dangling,
        "documents_with_holes": affected,
        "orphaned_vectors": orphan_vecs,
    }


def purge_orphaned_vectors(conn: sqlite3.Connection) -> int:
    """Drop vectors whose chunk no longer exists."""
    ids = [r[0] for r in conn.execute(
        "SELECT chunk_id FROM chunk_vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)")]
    for cid in ids:
        conn.execute("DELETE FROM chunk_vecs WHERE chunk_id=?", (cid,))
    return len(ids)


def reset_index(db_path: Path) -> None:
    """Drop the database entirely. The files remain the only truth."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    path: str,
    source_type: str,
    project: str | None,
    root: str | None,
    doc_date: str | None,
    mtime: float | None,
    size: int | None,
    content_hash: str,
    title: str | None,
    extra: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO documents(path, source_type, project, root, doc_date, mtime,
                              size, content_hash, title, extra_json)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            source_type=excluded.source_type,
            project=excluded.project,
            root=excluded.root,
            doc_date=excluded.doc_date,
            mtime=excluded.mtime,
            size=excluded.size,
            content_hash=excluded.content_hash,
            title=excluded.title,
            extra_json=excluded.extra_json
        RETURNING id
        """,
        (
            path, source_type, project, root, doc_date, mtime, size,
            content_hash, title, json.dumps(extra or {}, default=str),
        ),
    )
    return int(cur.fetchone()[0])


def clear_document_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
    """Remove a document's chunk occurrences, and any chunk left orphaned.

    Chunk rows are shared across documents by content hash, so a chunk is only
    deleted once no occurrence anywhere still refers to it. A surviving shared
    chunk whose ``doc_id`` points at the document being removed is re-homed to
    a document that still references it -- otherwise it would be left pointing
    at a row about to disappear, and (under the pre-v3 schema, which cascaded)
    would be deleted outright, leaving every other document that shares that
    text with an unretrievable hole.
    """
    hashes = [
        r["chunk_hash"]
        for r in conn.execute("SELECT chunk_hash FROM occurrences WHERE doc_id=?", (doc_id,))
    ]
    conn.execute("DELETE FROM occurrences WHERE doc_id=?", (doc_id,))
    for h in set(hashes):
        still = conn.execute(
            "SELECT doc_id, ord FROM occurrences WHERE chunk_hash=? LIMIT 1", (h,)
        ).fetchone()
        if still:
            conn.execute(
                "UPDATE chunks SET doc_id=?, ord=? WHERE content_hash=? AND doc_id=?",
                (still["doc_id"], still["ord"], h, doc_id),
            )
            continue
        row = conn.execute("SELECT id FROM chunks WHERE content_hash=?", (h,)).fetchone()
        if row:
            cid = row["id"]
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
            conn.execute("DELETE FROM chunk_vecs WHERE chunk_id=?", (cid,))
            conn.execute("DELETE FROM chunks WHERE id=?", (cid,))


def add_chunk(conn: sqlite3.Connection, doc_id: int, ch, ) -> tuple[int, bool]:
    """Insert a chunk if its content is new. Returns ``(chunk_id, is_new)``."""
    row = conn.execute(
        "SELECT id FROM chunks WHERE content_hash=?", (ch.content_hash,)
    ).fetchone()
    if row:
        chunk_id, is_new = int(row["id"]), False
    else:
        cur = conn.execute(
            "INSERT INTO chunks(doc_id, ord, kind, text, content_hash) VALUES(?,?,?,?,?)",
            (doc_id, ch.ord, ch.kind, ch.text, ch.content_hash),
        )
        chunk_id, is_new = int(cur.lastrowid), True
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (chunk_id, ch.text)
        )
    conn.execute(
        "INSERT OR IGNORE INTO occurrences(chunk_hash, doc_id, ord) VALUES(?,?,?)",
        (ch.content_hash, doc_id, ch.ord),
    )
    return chunk_id, is_new
