"""End-to-end: walk, store, search, incremental, rebuild, redaction."""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from lexiconlocal import db as dbmod
from lexiconlocal.config import load_config
from lexiconlocal.indexer import Indexer
from lexiconlocal.report import build_report
from lexiconlocal.search import Searcher
from lexiconlocal.walk import iter_files


class FakeEmbedder:
    """Deterministic offline embedder.

    The real embedder talks to localhost Ollama; tests must not depend on a
    running service, so this stands in with a cheap bag-of-characters vector.
    Search still exercises the full fusion path.
    """

    model = "fake-embed"
    batch_size = 8

    def preflight(self) -> int:
        return 16

    @property
    def dims(self) -> int:
        return 16

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * 16
            for i, ch in enumerate(t.lower()):
                v[ord(ch) % 16] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out

    def close(self) -> None:
        pass


@pytest.fixture
def indexed(lexicon_tree, claude_code_archive, codex_archive, chatgpt_export, claude_export):
    cfg = load_config(lexicon_tree / "config.yaml")
    idx = Indexer(cfg, FakeEmbedder(), verbose=False)
    summary = idx.run(full=True, batch_size=8)
    return cfg, summary


# --------------------------------------------------------------------------
# Walking and exclusion
# --------------------------------------------------------------------------

def test_walker_honours_excludes_and_never_index(lexicon_tree):
    cfg = load_config(lexicon_tree / "config.yaml")
    repos = lexicon_tree.parent / "programming"
    found = {str(f.path) for f in iter_files(cfg, repos, "programming")}
    assert not any("node_modules" in p for p in found)
    assert not any("/secrets/" in p for p in found)
    assert not any(p.endswith(".env") for p in found)
    assert any(p.endswith("Forge/docs/mission.md") for p in found)


def test_walker_attributes_loose_root_files_to_pseudo_project(lexicon_tree):
    cfg = load_config(lexicon_tree / "config.yaml")
    repos = lexicon_tree.parent / "programming"
    loose = [f for f in iter_files(cfg, repos, "programming") if f.path.name == "loose-note.md"]
    assert loose and loose[0].project == "_loose"


def test_private_directory_is_never_indexed(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    # NB: match the actual private/ directory, not the substring "private" --
    # macOS temp paths are themselves under /private/var/folders.
    private_dir = str(cfg.lexicon_root / "private")
    rows = conn.execute(
        "SELECT path FROM documents WHERE path LIKE ?", (f"{private_dir}%",)
    ).fetchall()
    assert rows == []
    hits = conn.execute("SELECT text FROM chunks WHERE text LIKE '%hunter2%'").fetchall()
    assert hits == [], "content under private/ must never reach the index"
    conn.close()


def test_symlinks_are_not_followed(lexicon_tree):
    cfg = load_config(lexicon_tree / "config.yaml")
    repos = lexicon_tree.parent / "programming"
    target = repos / "Forge" / "docs"
    link = repos / "Lighthouse" / "linked-docs"
    link.symlink_to(target, target_is_directory=True)
    found = [str(f.path) for f in iter_files(cfg, repos, "programming")]
    assert not any("linked-docs" in p for p in found)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

def test_secrets_never_reach_the_database(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    for needle in ("sk-abcdefghijklmnopqrstuvwxyz012345",
                   "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                   "AKIAIOSFODNN7EXAMPLE"):
        rows = conn.execute("SELECT 1 FROM chunks WHERE text LIKE ?", (f"%{needle}%",)).fetchall()
        assert rows == [], f"{needle} leaked into the index"
    redacted = conn.execute("SELECT 1 FROM chunks WHERE text LIKE '%[REDACTED:%' LIMIT 1").fetchall()
    assert redacted, "redaction should be visible, not silent"
    conn.close()


# --------------------------------------------------------------------------
# Storage shape
# --------------------------------------------------------------------------

def test_sessions_are_documents_not_files(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    rows = conn.execute(
        "SELECT path FROM documents WHERE source_type='transcript' AND path LIKE '%#session=%'"
    ).fetchall()
    assert len(rows) == 2, "three Claude Code files hold two sessions"
    conn.close()


def test_transcripts_get_project_attribution_from_cwd(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    row = conn.execute(
        "SELECT project FROM documents WHERE path LIKE '%session-aaaa-bbbb%'"
    ).fetchone()
    assert row["project"] == "Forge"
    conn.close()


def test_only_prose_chunks_are_embedded(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    n_events = conn.execute("SELECT COUNT(*) n FROM chunks WHERE kind='tool_event'").fetchone()["n"]
    assert n_events > 0
    leaked = conn.execute(
        "SELECT COUNT(*) n FROM chunk_vecs v JOIN chunks c ON c.id=v.chunk_id "
        "WHERE c.kind='tool_event'"
    ).fetchone()["n"]
    assert leaked == 0, "tool_event chunks are FTS-only by decision D-2026-08-18-07"
    conn.close()


def test_identical_chunks_stored_once_with_multiple_occurrences(lexicon_tree):
    repos = lexicon_tree.parent / "programming"
    body = "# Duplicate\n\n" + ("identical paragraph text. " * 40)
    (repos / "Lighthouse" / "dup1.md").write_text(body, encoding="utf-8")
    (repos / "Forge" / "dup2.md").write_text(body, encoding="utf-8")
    cfg = load_config(lexicon_tree / "config.yaml")
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    conn = dbmod.connect(cfg.db_path, read_only=True)
    dup = conn.execute(
        """SELECT chunk_hash, COUNT(*) AS n FROM occurrences
           GROUP BY chunk_hash HAVING n > 1"""
    ).fetchall()
    assert dup, "identical content in two documents should share one chunk row"
    for row in dup:
        cnt = conn.execute(
            "SELECT COUNT(*) n FROM chunks WHERE content_hash=?", (row["chunk_hash"],)
        ).fetchone()["n"]
        assert cnt == 1
    conn.close()


# --------------------------------------------------------------------------
# Incremental and rebuild
# --------------------------------------------------------------------------

def test_incremental_rerun_reports_nothing_new(indexed):
    cfg, _ = indexed
    summary = Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)
    assert summary["docs_written"] == 0
    assert summary["files_unchanged"] > 0
    assert summary["errors"] == 0


def test_edited_file_is_reindexed(indexed):
    cfg, _ = indexed
    p = cfg.source_roots[0].path / "Lighthouse" / "README.md"
    p.write_text("# Lighthouse\n\nNEWLY_EDITED_MARKER content.\n", encoding="utf-8")
    summary = Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)
    assert summary["docs_written"] >= 1
    conn = dbmod.connect(cfg.db_path, read_only=True)
    hit = conn.execute("SELECT 1 FROM chunks WHERE text LIKE '%NEWLY_EDITED_MARKER%'").fetchone()
    assert hit is not None
    conn.close()


def test_deleted_file_is_pruned(indexed):
    cfg, _ = indexed
    p = cfg.source_roots[0].path / "Lighthouse" / "README.md"
    p.unlink()
    summary = Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)
    assert summary["documents_pruned"] >= 1
    conn = dbmod.connect(cfg.db_path, read_only=True)
    assert conn.execute("SELECT 1 FROM documents WHERE path=?", (str(p),)).fetchone() is None
    conn.close()


def test_deleting_the_index_rebuilds_from_files(indexed):
    cfg, first = indexed
    dbmod.reset_index(cfg.db_path)
    assert not cfg.db_path.exists()
    second = Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    assert second["docs_written"] == first["docs_written"]
    assert second["chunks_written"] == first["chunks_written"]


def test_interrupted_embedding_resumes(indexed):
    """An interrupted run must continue, never restart from zero."""
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path)
    conn.execute("UPDATE chunks SET embedded=0 WHERE kind='prose'")
    conn.execute("DELETE FROM chunk_vecs")
    pending = conn.execute(
        "SELECT COUNT(*) n FROM chunks WHERE embedded=0 AND kind='prose'"
    ).fetchone()["n"]
    conn.commit()
    conn.close()
    assert pending > 0

    idx = Indexer(cfg, FakeEmbedder(), verbose=False)
    conn = dbmod.connect(cfg.db_path)
    done = idx.embed_pending(conn, batch_size=4)
    conn.close()
    assert done == pending

    conn = dbmod.connect(cfg.db_path, read_only=True)
    left = conn.execute("SELECT COUNT(*) n FROM chunks WHERE embedded=0 AND kind='prose'").fetchone()["n"]
    conn.close()
    assert left == 0


def test_embed_model_mismatch_refuses_to_search(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path)
    dbmod.set_meta(conn, "embed_model", "some-other-model")
    conn.commit()
    conn.close()
    conn = dbmod.connect(cfg.db_path, read_only=True)
    with pytest.raises(RuntimeError, match="not comparable"):
        dbmod.check_embed_compatibility(conn, "fake-embed", 16)
    conn.close()


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def test_exact_identifier_found_via_fts(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    results = s.search('"AAFParseError: invalid slot id 0x1F"', limit=5)
    assert results, "exact error strings must be findable"
    assert any("mission.md" in r.path for r in results)
    assert any("fts" in r.matched_by for r in results)
    s.close()


def test_exact_string_in_a_tool_event_header_is_findable(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    results = s.search('"pytest tests/test_slots.py"', limit=10)
    assert results, "commands captured as tool_event headers must be searchable"
    assert any(r.chunk_kind == "tool_event" for r in results)
    s.close()


def test_curated_lexicon_notes_outrank_transcripts(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    results = s.search("Isolated stem render", limit=10)
    assert results
    types = [r.source_type for r in results]
    best_lexicon = types.index("lexicon") if "lexicon" in types else 999
    best_transcript = types.index("transcript") if "transcript" in types else 999
    assert best_lexicon < best_transcript, "curated notes must outrank raw transcripts"
    s.close()


def test_project_filter_excludes_other_projects(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    results = s.search("AAF", project="Forge", limit=10)
    for r in results:
        assert (r.project or "").lower() in {"forge", "hammer"}
    s.close()


def test_historical_alias_resolves_to_current_project(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    assert "Lighthouse" in s.projects.resolve("Beacon")
    assert any(p.lower() == "beacon" for p in s.projects.resolve("Lighthouse"))
    s.close()


def test_known_absent_topic_returns_nothing_confident(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    results = s.search('"quantum chromodynamics lattice gauge"', limit=5)
    assert all("fts" not in r.matched_by for r in results), (
        "an absent phrase must not produce a confident lexical hit"
    )
    s.close()


def test_read_returns_chunk_with_context(indexed):
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    hit = s.search("Isolated stem", limit=1)[0]
    data = s.read(hit.path, hit.chunk_ord, context_chunks=1)
    assert "text" in data and data["text"]
    assert data["path"] == hit.path
    s.close()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_report_distinguishes_nothing_new_from_broken(indexed):
    cfg, _ = indexed
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)
    rep = build_report(cfg)
    text = "\n".join(rep.lines)
    assert "NOTHING NEW" in text
    assert rep.exit_code == 0


def test_report_marks_missing_sources_untested(lexicon_tree, claude_code_archive):
    cfg = load_config(lexicon_tree / "config.yaml")
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    rep = build_report(cfg)
    text = "\n".join(rep.lines)
    assert "chatgpt" in text and "UNTESTED" in text


def test_report_with_no_index_is_not_nothing_new(lexicon_tree):
    cfg = load_config(lexicon_tree / "config.yaml")
    rep = build_report(cfg)
    text = "\n".join(rep.lines)
    assert "NO INDEX" in text
    assert rep.exit_code == 1


def test_pipeline_version_bump_forces_reparse_but_keeps_embeddings(indexed, monkeypatch):
    """A parser/redaction change must reach already-indexed documents.

    The incremental path compares mtime+size, so without this a code fix would
    only ever apply to files edited afterwards. Re-parsing must not throw away
    vectors for chunks whose text did not actually change.
    """
    from lexiconlocal import indexer as idxmod

    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path, read_only=True)
    before = conn.execute("SELECT COUNT(*) n FROM chunk_vecs").fetchone()["n"]
    conn.close()
    assert before > 0

    monkeypatch.setattr(idxmod, "PIPELINE_VERSION", "999")
    idx = idxmod.Indexer(cfg, FakeEmbedder(), verbose=False)
    summary = idx.run(full=False, batch_size=8)
    assert idx.force_reparse is True
    assert summary["files_parsed"] > 0, "every document should be re-parsed"

    conn = dbmod.connect(cfg.db_path, read_only=True)
    after = conn.execute("SELECT COUNT(*) n FROM chunk_vecs").fetchone()["n"]
    stored = dbmod.get_meta(conn, "pipeline_version")
    conn.close()
    assert stored == "999"
    assert after == before, "unchanged chunks must keep their existing embeddings"
    assert summary["chunks_embedded"] == 0, "nothing changed, so nothing to re-embed"


def test_shared_chunk_survives_deletion_of_the_document_that_first_stored_it(indexed):
    """Chunks are shared by content hash; doc_id is 'first seen here', not owner.

    A cascade on chunks.doc_id deletes a shared chunk when its first document
    is pruned, silently punching holes in every other document that still
    references it. Found live in Phase 3.
    """
    cfg, _ = indexed
    repos = cfg.source_roots[0].path
    body = "# Shared\n\n" + ("exactly the same paragraph. " * 60)
    a = repos / "Lighthouse" / "shared_a.md"
    b = repos / "Forge" / "shared_b.md"
    a.write_text(body, encoding="utf-8")
    b.write_text(body, encoding="utf-8")
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)

    conn = dbmod.connect(cfg.db_path, read_only=True)
    shared = conn.execute(
        """SELECT chunk_hash FROM occurrences GROUP BY chunk_hash HAVING COUNT(*) > 1"""
    ).fetchall()
    conn.close()
    assert shared, "the two files should share chunk rows"

    # Delete the file that was indexed first, so its document is pruned.
    a.unlink()
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)

    conn = dbmod.connect(cfg.db_path, read_only=True)
    integ = dbmod.integrity_check(conn)
    surviving = conn.execute(
        "SELECT COUNT(*) n FROM chunks c JOIN occurrences o ON o.chunk_hash=c.content_hash "
        "JOIN documents d ON d.id=o.doc_id WHERE d.path LIKE '%shared_b.md'"
    ).fetchone()["n"]
    conn.close()
    assert integ["dangling_occurrences"] == 0, (
        "the surviving document must not be left referencing deleted chunk text"
    )
    assert surviving > 0, "shared_b.md must still have its chunks"


def test_integrity_damage_is_repaired_without_a_full_rebuild(indexed):
    """Damage must heal on the next incremental run, not require --full."""
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute(
        "SELECT c.id, c.content_hash FROM chunks c JOIN occurrences o "
        "ON o.chunk_hash = c.content_hash LIMIT 1"
    ).fetchone()
    # Simulate the damage: chunk row gone, occurrence left behind.
    conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (row["id"],))
    conn.execute("DELETE FROM chunk_vecs WHERE chunk_id=?", (row["id"],))
    conn.execute("DELETE FROM chunks WHERE id=?", (row["id"],))
    conn.commit()
    assert dbmod.integrity_check(conn)["dangling_occurrences"] > 0
    conn.close()

    idx = Indexer(cfg, FakeEmbedder(), verbose=False)
    idx.run(full=False, batch_size=8)
    assert idx.repair_mode is True, "damage should switch the indexer into repair mode"

    conn = dbmod.connect(cfg.db_path, read_only=True)
    assert dbmod.integrity_check(conn)["dangling_occurrences"] == 0
    conn.close()


def test_report_flags_index_damage_loudly(indexed):
    cfg, _ = indexed
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()
    conn.execute("DELETE FROM chunks WHERE id=?", (row["id"],))
    conn.commit(); conn.close()
    rep = build_report(cfg)
    text = "\n".join(rep.lines)
    assert "INDEX INTEGRITY: DAMAGED" in text
    assert "NOT retrievable" in text
    assert rep.exit_code == 1


def test_report_verdict_is_not_healthy_while_chunks_are_unembedded(lexicon_tree, claude_code_archive):
    """A run that parsed cleanly but embedded nothing is DEGRADED, not HEALTHY.

    The first unattended nightly run printed "VERDICT: HEALTHY" one line under
    "PENDING EMBED : 2,560 prose chunks" because the verdict only considered
    parse errors. The exit code was already correct; the sentence a human reads
    was not.
    """
    cfg = load_config(lexicon_tree / "config.yaml")
    conn = dbmod.connect(cfg.db_path)
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    conn = dbmod.connect(cfg.db_path)
    conn.execute("UPDATE chunks SET embedded=0 WHERE kind='prose'")
    conn.commit()
    conn.close()
    rep = build_report(cfg)
    text = "\n".join(rep.lines)
    assert "VERDICT: DEGRADED" in text
    assert "VERDICT: HEALTHY" not in text
    assert rep.exit_code == 1


# --------------------------------------------------------------------------
# Phase 5 D1: scoped retrieval, not post-filtering
# --------------------------------------------------------------------------

def _post_filter_search(s, query, project, limit=10):
    """The pre-D1 behaviour, reproduced: global top-50, filtered afterwards.

    Kept as a local reimplementation rather than a flag on `search()` -- the old
    path is a defect, not a mode, and should not survive in shipping code.
    """
    from lexiconlocal.search import CANDIDATES

    fts = s._fts_leg(query, CANDIDATES)
    vec = s._vec_leg(query, CANDIDATES)
    ids = {cid for cid, _ in fts} | {cid for cid, _, _ in vec}
    if not ids:
        return set()
    ph = ",".join("?" for _ in ids)
    want = {p.lower() for p in s.projects.resolve(project)}
    rows = s.conn.execute(
        f"SELECT d.path AS path, d.project AS project FROM chunks c "
        f"JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({ph})",
        list(ids),
    ).fetchall()
    return {r["path"] for r in rows if (r["project"] or "").lower() in want}


def test_scoped_search_is_a_superset_of_the_old_post_filter(indexed):
    """D1: scoping retrieval may add results, and must never remove one.

    The old path took the global top-50 and threw away everything outside the
    project. Anything it found is by definition inside the project, so a search
    that retrieves within the project must still find it.
    """
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    for query in ("AAF", "Isolated stem", "decisions"):
        for project in ("Forge", "Lighthouse"):
            old = _post_filter_search(s, query, project)
            new = {r.path for r in s.search(query, project=project, limit=50)}
            assert old <= new, (
                f"scoped search lost {old - new} for {query!r} in {project}"
            )
    s.close()


def test_scoped_search_reaches_material_the_global_pool_never_sees(indexed):
    """The actual D1 failure: a small project starved out of the global top-50.

    Reproduced by shrinking the global pool to 1 -- whatever the corpus size,
    that guarantees the post-filter has almost nothing to filter, which is what
    39-document Harbor faced against 9,077 documents in the live index.
    """
    import lexiconlocal.search as search_mod

    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    original = search_mod.CANDIDATES
    try:
        search_mod.CANDIDATES = 1
        starved = _post_filter_search(s, "AAF", "Lighthouse")
        scoped = {r.path for r in s.search("AAF", project="Lighthouse", limit=10)}
    finally:
        search_mod.CANDIDATES = original
    assert scoped, "a scoped search must find a project's own material"
    assert len(scoped) > len(starved)
    s.close()


def test_broad_filter_uses_the_enlarged_pool_not_an_exact_scan(indexed):
    """Amendment A1: only a project filter triggers scoped retrieval.

    A source_type filter selects a coarse partition, so scoping the vector leg
    to it would be a full scan for no ranking benefit.
    """
    import lexiconlocal.search as search_mod

    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    seen: list[tuple[int, list[str] | None]] = []
    real_vec = s._vec_leg

    def spy(query, limit, projects=None):
        seen.append((limit, projects))
        return real_vec(query, limit, projects)

    s._vec_leg = spy
    s.search("AAF", source_type="repo-doc", limit=10)
    assert seen == [(search_mod.BROAD_CANDIDATES, None)]

    seen.clear()
    s.search("AAF", limit=10)
    assert seen == [(search_mod.CANDIDATES, None)], "unfiltered path must be untouched"
    s.close()


def test_large_project_falls_back_to_the_global_pool(indexed):
    """Above EXACT_SCAN_MAX_CHUNKS an exact scan costs more than it is worth.

    The lexical leg stays exactly scoped either way, which is what makes the
    fallback safe: it alone guarantees a full page of in-project results.
    """
    import lexiconlocal.search as search_mod

    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    calls: list[tuple[str, int, list[str] | None]] = []
    real_vec, real_fts = s._vec_leg, s._fts_leg
    s._vec_leg = lambda q, limit, projects=None: (
        calls.append(("vec", limit, projects)) or real_vec(q, limit, projects))
    s._fts_leg = lambda q, limit, projects=None: (
        calls.append(("fts", limit, projects)) or real_fts(q, limit, projects))

    original = search_mod.EXACT_SCAN_MAX_CHUNKS
    try:
        search_mod.EXACT_SCAN_MAX_CHUNKS = 0  # force every project to be "large"
        s.search("AAF", project="Forge", limit=10)
    finally:
        search_mod.EXACT_SCAN_MAX_CHUNKS = original

    kinds = dict((c[0], c) for c in calls)
    assert kinds["fts"][2], "the lexical leg must stay scoped even on the fallback"
    assert kinds["vec"][2] is None, "the vector leg must go global on the fallback"
    assert kinds["vec"][1] == search_mod.BROAD_CANDIDATES
    s.close()


def test_project_chunk_count_does_not_touch_the_vector_table(indexed):
    """The size probe must be cheap, or it costs what it is deciding about."""
    cfg, _ = indexed
    s = Searcher(cfg, FakeEmbedder())
    projects = s.projects.resolve("Forge")
    plan = [r["detail"] for r in s.conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM chunks c "
        "JOIN documents d ON d.id = c.doc_id WHERE LOWER(d.project) IN (?)",
        [projects[0].lower()],
    ).fetchall()]
    assert not any("chunk_vecs" in d for d in plan), plan
    assert s._project_chunk_count(projects) > 0
    s.close()


# --------------------------------------------------------------------------
# historical_aliases comes from config.yaml, not from code
# --------------------------------------------------------------------------

def test_historical_aliases_are_read_from_config(lexicon_tree):
    """A renamed repo's old name must resolve without an INDEX.md row.

    These used to be a constant compiled into the package -- one operator's
    renamed projects shipped to every user. They are configuration.
    """
    from lexiconlocal.config import load_config
    from lexiconlocal.projects import load_project_index

    cfg_path = lexicon_tree / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + textwrap.dedent("""\
        historical_aliases:
          Old-Spike: NewProject
          old-spike-v2: NewProject
        """))
    cfg = load_config(cfg_path)
    assert cfg.historical_aliases == {"old-spike": "NewProject", "old-spike-v2": "NewProject"}

    idx = load_project_index(cfg.index_md, cfg.historical_aliases)
    assert "NewProject" in idx.resolve("old-spike")
    assert {"old-spike", "old-spike-v2"} <= set(idx.resolve("NewProject"))


def test_empty_historical_aliases_resolve_a_name_to_itself(lexicon_tree):
    from lexiconlocal.config import load_config
    from lexiconlocal.projects import load_project_index

    cfg = load_config(lexicon_tree / "config.yaml")
    assert cfg.historical_aliases == {}
    assert load_project_index(cfg.index_md, {}).resolve("Anything") == ["Anything"]


# --------------------------------------------------------------------------
# The local-only embedding invariant
#
# `embed.py` has always *said* localhost only. It did not enforce it: any host
# was accepted and contacted, and a `:cloud` entry satisfied model availability
# because the tag was stripped before comparing. Every chunk of the corpus goes
# through this transport, so the promise is either kept here or not at all.
# --------------------------------------------------------------------------

class _RecordingClient:
    """Stands in for httpx.Client and fails loudly if it is ever used."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, **kw):  # noqa: D102, ANN001
        self.calls.append(url)
        raise AssertionError(f"a request was made to a refused target: {url}")

    def post(self, url, **kw):  # noqa: D102, ANN001
        self.calls.append(url)
        raise AssertionError(f"a request was made to a refused target: {url}")

    def close(self) -> None:
        pass


@pytest.mark.parametrize("host", [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://127.0.0.1",
    "https://localhost:11434",
    "http://[::1]:11434",
    "http://127.5.5.5:11434",   # the whole 127/8 block is loopback
])
def test_loopback_embedding_targets_are_accepted(host):
    from lexiconlocal.embed import require_local_host

    assert require_local_host(host).startswith(("http://", "https://"))


@pytest.mark.parametrize("host", [
    "http://192.168.1.50:11434",     # LAN
    "http://10.0.0.7:11434",         # LAN
    "http://8.8.8.8:11434",          # public
    "http://ollama.example.com",     # ordinary hostname
    "http://localhost.evil.com",     # looks local, is not
    "http://[2001:4860:4860::8888]", # public IPv6
    "ftp://localhost:11434",         # not an HTTP transport
])
def test_nonlocal_embedding_targets_are_refused(host):
    from lexiconlocal.embed import EmbedTargetRefused, require_local_host

    with pytest.raises(EmbedTargetRefused):
        require_local_host(host)


def test_a_refused_target_never_reaches_the_network(monkeypatch):
    """The refusal must happen before the connection, not after it."""
    from lexiconlocal import embed as embed_mod

    recorder = _RecordingClient()
    monkeypatch.setattr(embed_mod.httpx, "Client", lambda **kw: recorder)
    with pytest.raises(embed_mod.EmbedTargetRefused):
        embed_mod.Embedder(host="http://192.168.1.50:11434")
    assert recorder.calls == []


def test_cloud_model_names_are_refused():
    from lexiconlocal.embed import EmbedTargetRefused, is_cloud_model, require_local_model

    assert is_cloud_model("nomic-embed-text:cloud")
    assert not is_cloud_model("nomic-embed-text")
    with pytest.raises(EmbedTargetRefused):
        require_local_model("nomic-embed-text:cloud")


def test_a_cloud_tagged_entry_does_not_satisfy_local_model_discovery(monkeypatch):
    """`nomic-embed-text:cloud` must not answer for `nomic-embed-text`.

    Stripping the tag before comparing meant a machine holding only the hosted
    model passed preflight and embedded the corpus through the vendor.
    """
    from lexiconlocal import embed as embed_mod

    class _TagsOnly:
        def get(self, url, **kw):  # noqa: ANN001
            class R:
                status_code = 200

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"models": [{"name": "nomic-embed-text:cloud"}]}
            return R()

        def post(self, url, **kw):  # noqa: ANN001
            raise AssertionError("must not embed when no local model is present")

        def close(self) -> None:
            pass

    monkeypatch.setattr(embed_mod.httpx, "Client", lambda **kw: _TagsOnly())
    emb = embed_mod.Embedder()
    with pytest.raises(embed_mod.EmbedError) as ei:
        emb.preflight()
    assert "cloud" in str(ei.value).lower()


def test_search_refuses_a_nonlocal_host_instead_of_degrading(monkeypatch, capsys):
    """A refused target is an operator error, not a missing service.

    `cmd_search` degrades to lexical-only when Ollama is unreachable, which is
    right. Routing a *refusal* down that path answered the query while quietly
    ignoring the `--host` that was asked for, because EmbedTargetRefused is an
    EmbedError. It must stop instead.
    """
    import argparse

    from lexiconlocal import cli

    args = argparse.Namespace(
        config=None, model="nomic-embed-text", host="http://192.168.1.50:11434",
        no_vector=False, query="anything", limit=5, project=None, source_type=None,
        after=None, before=None, kind=None, json=False, exact=False,
    )
    assert cli.cmd_search(args) == 2
    assert "REFUSED" in capsys.readouterr().err
