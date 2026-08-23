"""Ingest orchestration: enumerate, parse, store, then embed.

Two stages, deliberately separated:

* **Stage 1 (parse/store)** walks every source, writes documents and chunks,
  and leaves prose chunks flagged ``embedded=0``.
* **Stage 2 (embed)** drains that flag in batches, committing as it goes.

The split is what makes the run resumable. The initial corpus is roughly
0.8 GB of prose and takes hours; an interrupted run must continue, never
restart. Because ``embedded`` is persisted, resuming is simply "run again".
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from .chunk import KIND_PROSE
from .config import Config
from .embed import EmbedError, Embedder
from .parsers import chatgpt as chatgpt_parser
from .parsers import claude_code as cc_parser
from .parsers import claude_export as claude_parser
from .parsers import codex as codex_parser
from .parsers.base import ParsedDoc
from .parsers.markdown import parse_markdown, parse_plain_text
from .projects import project_for_path
from .walk import iter_files

#: Every source the indexer knows about. Sources that produce zero documents
#: are reported as UNTESTED rather than quietly omitted.
SOURCE_KEYS = (
    "lexicon", "repo-doc", "claude-code", "codex", "codex-memory",
    "chatgpt", "claude-export", "claude-memory", "claude-project",
)

#: nomic-embed-text's width. Only used when Ollama is down and there is no
#: existing index to read the real value from.
DEFAULT_EMBED_DIMS = 768

#: Bump whenever parsing, chunking, or redaction changes what gets stored for
#: identical input bytes. The incremental fast path compares mtime+size, so a
#: code change alone would otherwise never reach already-indexed documents --
#: a redaction fix would silently apply only to files edited afterwards.
#:
#: A bump forces a re-parse, NOT a re-embed: chunks are keyed by content hash,
#: so text that survives the change keeps its existing row and its vector, and
#: only genuinely changed chunks are embedded again.
#:
#: 1 - initial
#: 2 - high-entropy redaction no longer spans "/" (was eating file paths)
#: 3 - Claude export parser rewritten against the real dump (branch handling,
#:     flat-text preference, memories.json and projects/*.json)
#: 4 - Claude memories/projects keyed per export batch, so a later export no
#:     longer silently overwrites an earlier snapshot
#: 5 - repair pass: chunks shared between documents were being cascade-deleted
#:     when their first document was pruned, leaving other documents with
#:     unretrievable holes. Forces a re-parse so missing chunks are recreated.
#: 6 - ChatGPT export parser rewritten against the real dump: sharded
#:     conversations-NNN.json files, children reconstructed from parent edges,
#:     `thoughts` reasoning indexed at the tool_event tier, attachment
#:     filenames rendered inline
#: 7 - a ChatGPT content type the parser does not know is recorded in the
#:     transcript as unhandled instead of silently producing nothing
PIPELINE_VERSION = "7"


@dataclass
class SourceStats:
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    files_unchanged: int = 0
    docs_written: int = 0
    chunks_written: int = 0
    errors: list[str] = field(default_factory=list)
    encoding_fallbacks: list[str] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    #: Per-batch file classification for sources that ship as a bundle.
    extra_notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen,
            "files_parsed": self.files_parsed,
            "files_skipped": self.files_skipped,
            "files_unchanged": self.files_unchanged,
            "docs_written": self.docs_written,
            "chunks_written": self.chunks_written,
            "errors": self.errors[:200],
            "error_count": len(self.errors),
            "encoding_fallbacks": self.encoding_fallbacks[:200],
            "encoding_fallback_count": len(self.encoding_fallbacks),
            "redactions": self.redactions,
            "extra_notes": self.extra_notes,
        }


class Indexer:
    def __init__(self, cfg: Config, embedder: Embedder, *, verbose: bool = True) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.verbose = verbose
        self.stats: dict[str, SourceStats] = {k: SourceStats() for k in SOURCE_KEYS}
        self.seen_paths: set[str] = set()
        self._roots = [(r.path, r.label) for r in cfg.source_roots]
        #: Set in run(): when the stored pipeline version differs, the
        #: mtime+size fast path is disabled so the new logic reaches every
        #: already-indexed document.
        self.force_reparse = False
        #: Set when the index is found damaged: an unchanged document is then
        #: re-chunked rather than trusted, so holes heal without a full rebuild.
        self.repair_mode = False
        #: Set when preflight failed at startup; surfaced in the run summary.
        self._startup_embed_error: str | None = None

    # ---- helpers -----------------------------------------------------------

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _stat(self, key: str) -> SourceStats:
        return self.stats[key]

    def _unchanged(self, conn: sqlite3.Connection, path: str, mtime: float | None, size: int | None) -> bool:
        """Cheap change hint. mtime alone is untrustworthy on this machine
        (the 2026-07-13 Finder sweep), so a match here is only a fast path --
        content hash remains the authority whenever we do parse."""
        if self.force_reparse or mtime is None or size is None:
            return False
        row = conn.execute(
            "SELECT mtime, size FROM documents WHERE path=?", (path,)
        ).fetchone()
        if not row or row["mtime"] is None or row["size"] is None:
            return False
        return abs(float(row["mtime"]) - mtime) < 1e-6 and int(row["size"]) == size

    def _store(self, conn: sqlite3.Connection, doc: ParsedDoc, key: str) -> None:
        st = self._stat(key)
        existing = conn.execute(
            "SELECT id, content_hash FROM documents WHERE path=?", (doc.path,)
        ).fetchone()
        if existing and existing["content_hash"] == doc.content_hash:
            # The content is unchanged, but "unchanged" normally also assumes
            # the chunks are still there. In repair mode that assumption is
            # exactly what is in doubt, so verify before trusting it.
            intact = True
            if self.repair_mode:
                missing = conn.execute(
                    "SELECT COUNT(*) FROM occurrences o WHERE o.doc_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM chunks c WHERE c.content_hash=o.chunk_hash)",
                    (existing["id"],),
                ).fetchone()[0]
                intact = missing == 0
            if intact:
                conn.execute(
                    "UPDATE documents SET mtime=?, size=? WHERE id=?",
                    (doc.mtime, doc.size, existing["id"]),
                )
                st.files_unchanged += 1
                return

        doc_id = dbmod.upsert_document(
            conn,
            path=doc.path,
            source_type=doc.source_type,
            project=doc.project,
            root=doc.root,
            doc_date=doc.doc_date,
            mtime=doc.mtime,
            size=doc.size,
            content_hash=doc.content_hash,
            title=doc.title,
            extra=doc.extra,
        )
        if existing:
            dbmod.clear_document_chunks(conn, doc_id)
        for ch in doc.chunks:
            dbmod.add_chunk(conn, doc_id, ch)
        st.docs_written += 1
        st.chunks_written += len(doc.chunks)
        if doc.used_encoding_fallback:
            st.encoding_fallbacks.append(doc.path)
        for kind in doc.redactions:
            st.redactions[kind] = st.redactions.get(kind, 0) + 1

    # ---- sources -----------------------------------------------------------

    def index_lexicon_notes(self, conn: sqlite3.Connection) -> None:
        key = "lexicon"
        st = self._stat(key)
        for notes_dir in self.cfg.notes_dirs:
            if not notes_dir.exists():
                continue
            for found in iter_files(self.cfg, notes_dir, "lexicon"):
                st.files_seen += 1
                self.seen_paths.add(str(found.path))
                try:
                    stat = found.path.stat()
                except OSError as e:
                    st.errors.append(f"{found.path}: {e}")
                    continue
                if self._unchanged(conn, str(found.path), stat.st_mtime, stat.st_size):
                    st.files_unchanged += 1
                    continue
                try:
                    doc = parse_markdown(
                        found.path, source_type="lexicon",
                        project=found.project, root="lexicon",
                    )
                except Exception as e:  # noqa: BLE001 - recorded, never silent
                    st.errors.append(f"{found.path}: {type(e).__name__}: {e}")
                    continue
                if doc is None:
                    st.files_skipped += 1
                    continue
                st.files_parsed += 1
                self._store(conn, doc, key)

        # INDEX.md itself is the map from repo to project; index it.
        if self.cfg.index_md.exists():
            st.files_seen += 1
            self.seen_paths.add(str(self.cfg.index_md))
            doc = parse_markdown(
                self.cfg.index_md, source_type="lexicon", project="_index", root="lexicon"
            )
            if doc:
                st.files_parsed += 1
                self._store(conn, doc, key)

    def index_repo_docs(self, conn: sqlite3.Connection) -> None:
        key = "repo-doc"
        st = self._stat(key)
        for root in self.cfg.source_roots:
            if not root.path.exists():
                st.errors.append(f"source root missing: {root.path}")
                continue
            for found in iter_files(self.cfg, root.path, root.label):
                st.files_seen += 1
                self.seen_paths.add(str(found.path))
                try:
                    stat = found.path.stat()
                except OSError as e:
                    st.errors.append(f"{found.path}: {e}")
                    continue
                if self._unchanged(conn, str(found.path), stat.st_mtime, stat.st_size):
                    st.files_unchanged += 1
                    continue
                try:
                    doc = parse_markdown(
                        found.path, source_type="repo-doc",
                        project=found.project, root=found.root_label,
                    )
                except Exception as e:  # noqa: BLE001
                    st.errors.append(f"{found.path}: {type(e).__name__}: {e}")
                    continue
                if doc is None:
                    st.files_skipped += 1
                    continue
                st.files_parsed += 1
                self._store(conn, doc, key)
                if st.files_parsed % 500 == 0:
                    self.log(f"    repo-doc: {st.files_parsed} parsed / {st.files_seen} seen")
                    conn.commit()

    def index_claude_code(self, conn: sqlite3.Connection) -> None:
        key = "claude-code"
        st = self._stat(key)
        root = self.cfg.archive_dir / "claude-code"
        if not root.exists():
            return
        files = sorted(p for p in root.rglob("*.jsonl") if p.is_file())
        st.files_seen += len(files)
        groups = cc_parser.group_by_session(files)
        self.log(f"    claude-code: {len(files)} files -> {len(groups)} sessions")
        for sid, group in groups.items():
            for f in group:
                self.seen_paths.add(str(f))
            doc_path = f"{group[0].parent}#session={sid}"
            self.seen_paths.add(doc_path)
            try:
                agg_mtime = max(f.stat().st_mtime for f in group)
                agg_size = sum(f.stat().st_size for f in group)
            except OSError as e:
                st.errors.append(f"{doc_path}: {e}")
                continue
            if self._unchanged(conn, doc_path, agg_mtime, agg_size):
                st.files_unchanged += 1
                continue
            try:
                doc = cc_parser.parse_session(sid, group, self.cfg.archive_dir)
            except Exception as e:  # noqa: BLE001
                st.errors.append(f"{doc_path}: {type(e).__name__}: {e}")
                continue
            if doc is None:
                st.files_skipped += 1
                continue
            doc.mtime, doc.size = agg_mtime, agg_size
            cwd = doc.extra.get("cwd")
            if cwd:
                proj, rootlabel = project_for_path(Path(cwd), self._roots)
                doc.project, doc.root = proj, rootlabel
            st.files_parsed += 1
            self._store(conn, doc, key)

        # Sidecar .md / .txt files living beside the transcripts.
        self._index_side_files(conn, root, "archive-doc", "claude-code")

    def _index_side_files(
        self, conn: sqlite3.Connection, root: Path, source_type: str, key: str,
        suffixes: tuple[str, ...] = (".md", ".txt"),
    ) -> None:
        st = self._stat(key)
        for suffix in suffixes:
            for p in sorted(root.rglob(f"*{suffix}")):
                if not p.is_file() or self.cfg.is_excluded_file(p.name):
                    continue
                if any(self.cfg.is_excluded_dir(part) for part in p.parts):
                    continue
                st.files_seen += 1
                self.seen_paths.add(str(p))
                try:
                    stat = p.stat()
                except OSError as e:
                    st.errors.append(f"{p}: {e}")
                    continue
                if self._unchanged(conn, str(p), stat.st_mtime, stat.st_size):
                    st.files_unchanged += 1
                    continue
                parser = parse_markdown if suffix == ".md" else parse_plain_text
                try:
                    doc = parser(p, source_type=source_type, project=None, root="archive")
                except Exception as e:  # noqa: BLE001
                    st.errors.append(f"{p}: {type(e).__name__}: {e}")
                    continue
                if doc is None:
                    st.files_skipped += 1
                    continue
                st.files_parsed += 1
                self._store(conn, doc, key)

    def index_codex(self, conn: sqlite3.Connection) -> None:
        key = "codex"
        st = self._stat(key)
        root = self.cfg.archive_dir / "codex"
        if not root.exists():
            return
        titles = codex_parser.load_thread_titles(root / "session_index.jsonl")
        rollouts = list(codex_parser.iter_rollouts(root))
        st.files_seen += len(rollouts)
        self.log(f"    codex: {len(rollouts)} rollout files ({len(titles)} known thread titles)")
        t0 = time.time()
        for i, path in enumerate(rollouts, 1):
            self.seen_paths.add(str(path))
            try:
                stat = path.stat()
            except OSError as e:
                st.errors.append(f"{path}: {e}")
                continue
            if self._unchanged(conn, str(path), stat.st_mtime, stat.st_size):
                st.files_unchanged += 1
                continue
            try:
                doc = codex_parser.parse_rollout(path)
            except Exception as e:  # noqa: BLE001
                st.errors.append(f"{path}: {type(e).__name__}: {e}")
                continue
            if doc is None:
                st.files_skipped += 1
                continue
            sid = doc.extra.get("session_id")
            if sid and sid in titles:
                doc.title = titles[sid]
            doc.title = doc.title or path.stem
            cwd = doc.extra.get("cwd")
            if cwd:
                proj, rootlabel = project_for_path(Path(cwd), self._roots)
                doc.project, doc.root = proj, rootlabel
            st.files_parsed += 1
            self._store(conn, doc, key)
            if i % 25 == 0:
                elapsed = time.time() - t0
                self.log(f"    codex: {i}/{len(rollouts)} files, {elapsed:.0f}s elapsed")
                conn.commit()

        # Attachments (.txt pastes) and the memories snapshots.
        att = root / "attachments"
        if att.exists():
            self._index_side_files(conn, att, "archive-doc", "codex", (".txt", ".md"))
        for mem_dir in sorted(root.glob("memories-*")):
            if mem_dir.is_dir():
                self._index_side_files(conn, mem_dir, "codex-memory", "codex-memory", (".md",))

    def index_chatgpt(self, conn: sqlite3.Connection) -> None:
        """ChatGPT account export: conversations sharded across many files.

        Like the Claude export, documents are keyed by conversation id rather
        than by file path. That matters more here: the export splits its
        conversations across ``conversations-NNN.json`` shards and a later
        export will not split them the same way, so path-keyed documents would
        duplicate the whole corpus on every new batch.
        """
        key = "chatgpt"
        st = self._stat(key)
        root = self.cfg.archive_dir / "chatgpt"
        if not root.exists():
            return
        batches = chatgpt_parser.iter_batches(root)
        if not batches:
            return

        surveys = [
            chatgpt_parser.survey_batch(b, chatgpt_parser.batch_label(root, b))
            for b in batches
        ]
        st.extra_notes = surveys
        for sv in surveys:
            st.files_seen += len(sv["parsed"])
            st.files_skipped += len(sv["pii_excluded"])
            for u in sv["unknown"]:
                st.errors.append(
                    f"{sv['batch']}/{u}: unrecognised file in ChatGPT export -- "
                    f"not indexed. If the export format changed, teach the parser."
                )
        self.log(
            f"    chatgpt export: {len(batches)} batch(es), "
            f"{sum(len(s['parsed']) for s in surveys)} conversation shard(s), "
            f"{sum(len(s['pii_excluded']) for s in surveys)} PII file(s) excluded"
        )

        for doc in chatgpt_parser.iter_exports(root):
            self.seen_paths.add(doc.path)
            self._store(conn, doc, key)

        # `files_*` count FILES everywhere else in the report; keep that here.
        # This source is a handful of shards that expand into many documents,
        # and documents are counted by docs_written.
        st.files_parsed += sum(len(s["parsed"]) for s in surveys)

    def index_claude_export(self, conn: sqlite3.Connection) -> None:
        """Claude account export: conversations, memories, and projects.

        Documents are keyed by conversation/project UUID rather than file path,
        so a later batch that re-includes a conversation updates it instead of
        duplicating it. Batches are processed oldest-first so the newest wins.
        """
        root = self.cfg.archive_dir / "claude"
        if not root.exists():
            return
        batches = claude_parser.iter_batches(root)
        if not batches:
            return

        # Classify every file so PII exclusions and unrecognised files are
        # reported rather than silently skipped.
        surveys = [claude_parser.survey_batch(b) for b in batches]
        for key in ("claude-export", "claude-memory", "claude-project"):
            self._stat(key).extra_notes = surveys
        st_export = self._stat("claude-export")
        for sv in surveys:
            st_export.files_seen += len(sv["parsed"])
            st_export.files_skipped += len(sv["pii_excluded"])
            for u in sv["unknown"]:
                st_export.errors.append(
                    f"{sv['batch']}/{u}: unrecognised file in Claude export -- "
                    f"not indexed. If the export format changed, teach the parser."
                )
        self.log(
            f"    claude export: {len(batches)} batch(es), "
            f"{sum(len(s['parsed']) for s in surveys)} parsable file(s), "
            f"{sum(len(s['pii_excluded']) for s in surveys)} PII file(s) excluded"
        )

        for doc in claude_parser.iter_exports(root):
            key = {
                "transcript": "claude-export",
                "claude-memory": "claude-memory",
                "claude-project": "claude-project",
            }.get(doc.source_type, "claude-export")
            self.seen_paths.add(doc.path)
            self._store(conn, doc, key)

        # `files_seen`/`files_parsed` count FILES everywhere else in the report,
        # so keep that meaning here: this source is a small bundle of files that
        # expands into many documents, and documents are counted by docs_written.
        st_export.files_parsed += sum(len(s["parsed"]) for s in surveys)
        for key in ("claude-memory", "claude-project"):
            st = self._stat(key)
            st.files_seen += 1
            st.files_parsed += 1

    # ---- derived metadata --------------------------------------------------

    def refresh_attribution(self, conn: sqlite3.Connection) -> int:
        """Re-derive transcript project attribution from stored metadata.

        Project attribution comes from config and path rules, not from file
        content, so it can change while every source file stays byte-identical.
        Unchanged documents are skipped by the incremental fast path and would
        otherwise keep a stale project until the next full rebuild -- which
        costs hours of re-embedding for what is a metadata-only correction.
        ``cwd`` is already stored in ``extra_json``, so this needs no reparse.
        """
        updated = 0
        rows = conn.execute(
            "SELECT id, project, root, extra_json FROM documents WHERE source_type='transcript'"
        ).fetchall()
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except ValueError:
                continue
            cwd = extra.get("cwd")
            if not cwd:
                continue
            proj, rootlabel = project_for_path(Path(cwd), self._roots)
            if proj and (proj != row["project"] or rootlabel != row["root"]):
                conn.execute(
                    "UPDATE documents SET project=?, root=? WHERE id=?",
                    (proj, rootlabel, row["id"]),
                )
                updated += 1
        return updated

    # ---- deletions ---------------------------------------------------------

    def prune_missing(self, conn: sqlite3.Connection) -> int:
        """Drop documents whose source no longer exists.

        The index is disposable and the files are truth, so anything the walk
        did not see this run is gone.
        """
        removed = 0
        rows = conn.execute("SELECT id, path FROM documents").fetchall()
        for row in rows:
            if row["path"] in self.seen_paths:
                continue
            dbmod.clear_document_chunks(conn, row["id"])
            conn.execute("DELETE FROM documents WHERE id=?", (row["id"],))
            removed += 1
        return removed

    # ---- embedding ---------------------------------------------------------

    def embed_pending(self, conn: sqlite3.Connection, batch_size: int = 32) -> int:
        """Embed every prose chunk still flagged unembedded.

        Commits after each batch, so an interrupt costs at most one batch and
        a rerun picks up exactly where this left off.
        """
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedded=0 AND kind=?", (KIND_PROSE,)
        ).fetchone()["n"]
        if total == 0:
            self.log("  embeddings: nothing pending")
            return 0
        self.log(f"  embeddings: {total:,} prose chunks pending")

        done = 0
        t0 = time.time()
        while True:
            rows = conn.execute(
                "SELECT id, text FROM chunks WHERE embedded=0 AND kind=? LIMIT ?",
                (KIND_PROSE, batch_size),
            ).fetchall()
            if not rows:
                break
            ids = [r["id"] for r in rows]
            texts = [r["text"] for r in rows]
            vectors = self.embedder.embed(texts)
            for cid, vec in zip(ids, vectors):
                # sqlite-vec vec0 tables do not implement UPSERT, so replace
                # explicitly rather than relying on ON CONFLICT.
                conn.execute("DELETE FROM chunk_vecs WHERE chunk_id=?", (cid,))
                conn.execute(
                    "INSERT INTO chunk_vecs(chunk_id, embedding) VALUES(?,?)",
                    (cid, dbmod.serialize_f32(vec)),
                )
                conn.execute("UPDATE chunks SET embedded=1 WHERE id=?", (cid,))
            conn.commit()
            done += len(rows)
            if done % (batch_size * 10) == 0 or done >= total:
                elapsed = max(time.time() - t0, 1e-6)
                rate = done / elapsed
                remaining = max(total - done, 0)
                eta = remaining / rate if rate > 0 else 0
                pct = 100.0 * done / total if total else 100.0
                self.log(
                    f"    embedded {done:,}/{total:,} ({pct:.1f}%) "
                    f"{rate:.1f} chunks/s  ETA {eta/60:.1f} min"
                )
        self.log(f"  embeddings: {done:,} chunks in {(time.time()-t0)/60:.1f} min")
        return done

    # ---- driver ------------------------------------------------------------

    def run(self, *, full: bool = False, batch_size: int = 32, embed: bool = True) -> dict:
        cfg = self.cfg
        if full:
            self.log("Full rebuild: dropping the existing index (files remain truth).")
            dbmod.reset_index(cfg.db_path)

        try:
            dims = self.embedder.preflight() if embed else DEFAULT_EMBED_DIMS
        except EmbedError as e:
            # Carry on: stage 1 has no Ollama dependency (D-2026-08-18-17).
            # Reuse the dimension the existing index was built with, so the
            # vec0 table keeps its shape; fall back to the model's known width
            # when there is no index yet.
            self.log(f"  embedder unavailable at startup: {e}")
            self.log("  continuing with stage 1 only (parse + store + FTS).")
            dims = DEFAULT_EMBED_DIMS
            if cfg.db_path.exists():
                probe = dbmod.connect(cfg.db_path, read_only=True)
                try:
                    stored = dbmod.get_meta(probe, "embed_dims")
                    if stored:
                        dims = int(stored)
                except sqlite3.Error:
                    pass
                finally:
                    probe.close()
            embed = False
            self._startup_embed_error = str(e)
        conn = dbmod.connect(cfg.db_path)
        dbmod.init_schema(conn, dims)

        damage = dbmod.integrity_check(conn)
        if damage["dangling_occurrences"]:
            self.repair_mode = True
            self.force_reparse = True
            self.log(
                f"Index damage detected: {damage['dangling_occurrences']} occurrence(s) "
                f"across {damage['documents_with_holes']} document(s) reference missing "
                f"chunk text. Re-parsing and rebuilding those documents."
            )

        stored_pipeline = dbmod.get_meta(conn, "pipeline_version")
        if not full and stored_pipeline != PIPELINE_VERSION:
            # A missing version means the index predates this mechanism, so we
            # cannot know what logic produced it -- re-parse rather than assume.
            self.force_reparse = True
            self.log(
                f"Pipeline version changed ({stored_pipeline or 'unknown'} -> "
                f"{PIPELINE_VERSION}): re-parsing every document. Chunks whose "
                f"text is unchanged keep their existing embeddings."
            )
        dbmod.set_meta(conn, "pipeline_version", PIPELINE_VERSION)
        dbmod.set_meta(conn, "schema_version", dbmod.SCHEMA_VERSION)
        dbmod.set_meta(conn, "embed_model", self.embedder.model)
        dbmod.set_meta(conn, "embed_dims", str(dims))
        if full:
            dbmod.set_meta(conn, "last_full_rebuild", datetime.now(timezone.utc).isoformat())
        conn.commit()

        started = datetime.now(timezone.utc).isoformat()
        t0 = time.time()

        self.log("Stage 1/2: parse and store")
        for label, fn in (
            ("lexicon notes", self.index_lexicon_notes),
            ("repo docs", self.index_repo_docs),
            ("claude-code transcripts", self.index_claude_code),
            ("codex transcripts", self.index_codex),
            ("chatgpt export", self.index_chatgpt),
            ("claude export", self.index_claude_export),
        ):
            self.log(f"  {label} ...")
            fn(conn)
            conn.commit()

        reattributed = self.refresh_attribution(conn)
        if reattributed:
            self.log(f"  re-attributed {reattributed} transcripts to a project")

        purged = dbmod.purge_orphaned_vectors(conn)
        if purged:
            self.log(f"  purged {purged} vector(s) left behind by removed chunks")

        removed = self.prune_missing(conn)
        if removed:
            self.log(f"  pruned {removed} documents whose source no longer exists")
        conn.commit()

        # Stage 2 is the only part that needs Ollama (D-2026-08-18-17). If it is
        # unavailable, stage 1's work still stands: documents are parsed, chunked
        # and FTS-indexed, prose chunks sit at embedded=0, and `lexicon report`
        # exits non-zero naming the backlog. A week of Ollama being down must not
        # mean a week of transcripts missing from the index entirely.
        self.log("Stage 2/2: embed")
        embedded = 0
        embed_error: str | None = None
        if embed:
            try:
                embedded = self.embed_pending(conn, batch_size=batch_size)
            except EmbedError as e:
                embed_error = str(e)
                self.log(f"  EMBEDDING UNAVAILABLE: {e}")
                self.log("  Stage 1 results are stored; prose chunks remain pending.")

        finished = datetime.now(timezone.utc).isoformat()
        totals = {
            k: sum(getattr(s, k) for s in self.stats.values())
            for k in ("files_seen", "files_parsed", "files_skipped", "files_unchanged",
                      "docs_written", "chunks_written")
        }
        all_errors = {k: s.errors for k, s in self.stats.items() if s.errors}
        all_fallbacks = [f for s in self.stats.values() for f in s.encoding_fallbacks]

        conn.execute(
            """INSERT INTO ingest_runs(started, finished, mode, files_seen, files_parsed,
               files_skipped, files_unchanged, docs_written, chunks_written, chunks_embedded,
               fallback_encoding, errors_json, per_source_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                started, finished, "full" if full else "incremental",
                totals["files_seen"], totals["files_parsed"], totals["files_skipped"],
                totals["files_unchanged"], totals["docs_written"], totals["chunks_written"],
                embedded, json.dumps(all_fallbacks), json.dumps(all_errors),
                json.dumps({k: v.as_dict() for k, v in self.stats.items()}),
            ),
        )
        conn.commit()

        summary = {
            **totals,
            "chunks_embedded": embedded,
            "embed_error": embed_error or self._startup_embed_error,
            "documents_pruned": removed,
            "reattributed": reattributed,
            "elapsed_sec": round(time.time() - t0, 1),
            "errors": sum(len(v) for v in all_errors.values()),
            "encoding_fallbacks": len(all_fallbacks),
            "per_source": {k: v.as_dict() for k, v in self.stats.items()},
        }
        conn.close()
        return summary
