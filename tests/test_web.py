"""Phase 4: routes, path admission, rendering, and the dashboard shape.

The security tests here are the important ones. Never-serve is enforced at the
request layer precisely so it does not depend on the index being right
(D-2026-08-19-08), and a test that only checked "an unindexed path 404s" would
pass for the wrong reason. Every refusal test below therefore uses a file that
**exists on disk**, so a regression that started serving it would be a real
disclosure and not a missing-file 404.
"""

from __future__ import annotations

import json
import os
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from lexiconlocal.config import load_config
from lexiconlocal.indexer import Indexer
from lexiconlocal.search import Searcher
from lexiconlocal.web import notes, paths as pathmod
from lexiconlocal.web.dashboard import build_dashboard, render_home_md, write_home
from lexiconlocal.web.render import render_markdown
from lexiconlocal.web.server import BindRefused, WebConfig, serve

from .test_pipeline import FakeEmbedder


@pytest.fixture
def served(lexicon_tree, claude_code_archive, codex_archive, chatgpt_export, claude_export):
    """A fully indexed miniature Lexicon behind a live loopback server."""
    cfg = load_config(lexicon_tree / "config.yaml")
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    # Port 0: the OS picks a free one, so tests never collide with a real
    # server or with each other under xdist.
    srv = serve(WebConfig(cfg=cfg, port=0, open_browser=False), quiet=True)
    srv.readers.embedder = FakeEmbedder()
    srv.readers.embed_error = None
    srv.start_background()
    try:
        yield cfg, srv
    finally:
        srv.shutdown()


def _raw(srv, path: str):
    req = urllib.request.Request(srv.url.rstrip("/") + path)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _get(srv, path: str):
    status, headers, body = _raw(srv, path)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


def _doc_url(p) -> str:
    return "/api/doc?path=" + urllib.parse.quote(str(p), safe="")


# --------------------------------------------------------------------------
# Never-serve and path traversal — every target EXISTS on disk
# --------------------------------------------------------------------------

def test_private_file_exists_but_is_never_served(served):
    cfg, srv = served
    target = cfg.lexicon_root / "private" / "secret-notes.md"
    assert target.is_file(), "the fixture must actually create it, or this proves nothing"
    assert "hunter2" in target.read_text(encoding="utf-8")

    status, body = _get(srv, _doc_url(target))
    assert status == 404, "private/ was served"
    assert "hunter2" not in json.dumps(body)


def test_env_file_exists_but_is_never_served(served):
    cfg, srv = served
    target = cfg.source_roots[0].path / "Lighthouse" / ".env"
    assert target.is_file()
    status, body = _get(srv, _doc_url(target))
    assert status == 404
    assert "sk-" not in json.dumps(body)


def test_dot_dot_traversal_out_of_every_root(served):
    cfg, srv = served
    escape = cfg.lexicon_root / "projects" / ".." / ".." / ".." / "etc" / "hosts"
    status, _ = _get(srv, _doc_url(escape))
    assert status == 404


def test_relative_dot_dot_env_is_refused(served):
    cfg, srv = served
    status, _ = _get(srv, "/api/doc?path=" + urllib.parse.quote("../../.env", safe=""))
    assert status == 404


def test_symlink_escaping_a_root_is_refused(served, tmp_path):
    """A symlink inside an allowed root pointing outside it.

    Canonicalisation happens before containment is tested, so the link's
    target -- not its location -- decides.
    """
    cfg, srv = served
    outside = tmp_path / "outside-the-lexicon.md"
    outside.write_text("# OUTSIDE_MARKER\n", encoding="utf-8")
    link = cfg.lexicon_root / "projects" / "escape.md"
    link.symlink_to(outside)

    status, body = _get(srv, _doc_url(link))
    assert status == 404, "a symlink walked out of the Lexicon"
    assert "OUTSIDE_MARKER" not in json.dumps(body)


def test_symlink_into_private_is_refused(served):
    """The never-serve rule survives laundering through a legal-looking path."""
    cfg, srv = served
    link = cfg.lexicon_root / "projects" / "looks-fine.md"
    link.symlink_to(cfg.lexicon_root / "private" / "secret-notes.md")
    status, body = _get(srv, _doc_url(link))
    assert status == 404
    assert "hunter2" not in json.dumps(body)


def test_the_index_database_is_not_servable(served):
    cfg, srv = served
    assert cfg.db_path.is_file()
    status, _ = _get(srv, _doc_url(cfg.db_path))
    assert status == 404


def test_admission_refuses_before_consulting_the_index(served):
    """The gate is independent of what the index happens to contain.

    Even a path deliberately inserted into `documents` must not be served --
    this is the whole point of D-2026-08-19-08.
    """
    cfg, srv = served
    secret = cfg.lexicon_root / "private" / "secret-notes.md"
    verdict = pathmod.admit(cfg, str(secret))
    assert not verdict and "never-serve" in verdict.reason


def test_an_incidental_private_component_does_not_block_a_legal_path(served):
    """`private` is policy about one directory, not a banned substring.

    An earlier version refused any path containing a component named
    `private`. On macOS `/tmp` and `/var` are symlinks into `/private/...`, so
    canonicalisation put that component into *every* path under them and the
    rule refused the whole corpus. The policy is containment in
    `~/Lexicon/private`, and this pins the distinction.
    """
    cfg, srv = served
    resolved = Path(os.path.realpath(cfg.lexicon_root))
    if "private" not in resolved.parts:
        pytest.skip("this platform does not stage temp dirs under /private")
    target = cfg.lexicon_root / "projects" / "forge" / "overview.md"
    status, body = _get(srv, _doc_url(target))
    assert status == 200, "an incidental /private/ ancestor blocked a legal document"
    # ...while the actual private tree, under the same ancestor, is still refused.
    status, _ = _get(srv, _doc_url(cfg.lexicon_root / "private" / "secret-notes.md"))
    assert status == 404


def test_binary_and_unknown_suffixes_are_refused(served):
    cfg, srv = served
    blob = cfg.lexicon_root / "projects" / "attachment.dat"
    blob.write_bytes(b"\x00\x01binary")
    status, _ = _get(srv, _doc_url(blob))
    assert status == 404


# --------------------------------------------------------------------------
# Method and bind policy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_no_write_methods_exist(served, method):
    cfg, srv = served
    req = urllib.request.Request(srv.url, method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    assert e.value.code == 405
    assert e.value.headers["Allow"] == "GET, HEAD"


@pytest.mark.parametrize("addr", ["0.0.0.0", "192.168.1.10", "::"])
def test_non_loopback_bind_is_refused(lexicon_tree, addr):
    cfg = load_config(lexicon_tree / "config.yaml")
    with pytest.raises(BindRefused):
        WebConfig(cfg=cfg, port=0, bind=addr).validate()


def test_loopback_binds_are_allowed(lexicon_tree):
    cfg = load_config(lexicon_tree / "config.yaml")
    for addr in ("127.0.0.1", "::1"):
        WebConfig(cfg=cfg, port=0, bind=addr).validate()


def test_security_headers_are_present_and_block_inline_script(served):
    cfg, srv = served
    status, headers, _ = _raw(srv, "/")
    assert status == 200
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "default-src 'none'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_static_paths_cannot_escape_the_static_dir(served):
    cfg, srv = served
    status, _ = _get(srv, "/static/../server.py")
    assert status == 404
    status, _, body = _raw(srv, "/static/app.css")
    assert status == 200 and b"--accent" in body


# --------------------------------------------------------------------------
# Search parity with the CLI
# --------------------------------------------------------------------------

GOLDEN = [
    "Isolated stem render",
    '"AAFParseError: invalid slot id 0x1F"',
    "sidecar inspection of harbor manifests",
]


@pytest.mark.parametrize("query", GOLDEN)
def test_api_search_matches_the_searcher_exactly(served, query):
    """The API must be a wrapper, not a second ranking implementation."""
    cfg, srv = served
    direct = Searcher(cfg, FakeEmbedder())
    try:
        expected = direct.search(query, limit=10)
    finally:
        direct.close()

    status, body = _get(srv, "/api/search?limit=10&q=" + urllib.parse.quote(query))
    assert status == 200
    assert body["count"] == len(expected)
    assert [r["path"] for r in body["results"]] == [r.path for r in expected]
    assert [round(r["score"], 5) for r in body["results"]] == [
        round(r.score, 5) for r in expected
    ]


def test_search_requires_a_query(served):
    cfg, srv = served
    status, body = _get(srv, "/api/search?q=")
    assert status == 400 and "q is required" in body["error"]


def test_exact_mode_routes_through_the_same_quoting_rule(served):
    cfg, srv = served
    status, body = _get(srv, "/api/search?exact=1&q=" + urllib.parse.quote("bounce.py"))
    assert status == 200
    assert body["exact_mode"] is True
    assert body["effective_query"] == '"bounce.py"'


def test_results_carry_a_locator_back_to_the_source(served):
    cfg, srv = served
    status, body = _get(srv, "/api/search?q=Track+Bounce+isolated+render")
    assert body["results"], "fixture query returned nothing"
    for r in body["results"]:
        assert r["locator"] == f"{r['path']}#chunk={r['chunk_ord']}"
        assert 0.0 < r["confidence"] <= 1.0


# --------------------------------------------------------------------------
# Projects and alias resolution
# --------------------------------------------------------------------------

def test_project_endpoint_returns_notes(served):
    cfg, srv = served
    status, body = _get(srv, "/api/project/forge")
    assert status == 200
    assert body["name"] == "forge"
    assert "Isolated stem render" in body["overview_html"]
    assert body["overview_html"].startswith("<")


def test_project_alias_resolves_to_the_curated_directory(served):
    """History recorded under a name that is no longer a directory must land.

    INDEX.md maps Beacon -> Lighthouse; a link from an old transcript has to
    reach somewhere rather than 404.
    """
    cfg, srv = served
    (cfg.lexicon_root / "projects" / "Lighthouse").mkdir(parents=True, exist_ok=True)
    (cfg.lexicon_root / "projects" / "Lighthouse" / "overview.md").write_text(
        "# Lighthouse\n\nALIAS_TARGET_MARKER\n", encoding="utf-8")

    status, body = _get(srv, "/api/project/Beacon")
    assert status == 200
    assert body["name"] == "Lighthouse"
    assert body["resolved_from"] == "Beacon"
    assert "ALIAS_TARGET_MARKER" in body["overview_html"]


def test_unknown_project_is_404_not_an_empty_page(served):
    cfg, srv = served
    status, _ = _get(srv, "/api/project/NoSuchProject")
    assert status == 404


@pytest.mark.parametrize("name", ["../private", "..%2F..%2Fetc", "a/b"])
def test_project_names_cannot_traverse(served, name):
    cfg, srv = served
    status, _ = _get(srv, "/api/project/" + name)
    assert status == 404


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

def test_markdown_document_renders_to_html(served):
    cfg, srv = served
    target = cfg.lexicon_root / "projects" / "forge" / "overview.md"
    status, body = _get(srv, _doc_url(target))
    assert status == 200
    assert body["format"] == "markdown"
    assert "<h1" in body["html"]
    assert body["source_path"] == str(target)
    assert body["indexed"] is True


def test_transcript_document_renders_as_ordered_chunks(served):
    """A conversation has no file; it must still be viewable, with locators."""
    cfg, srv = served
    s = Searcher(cfg, None)
    try:
        row = s.conn.execute(
            "SELECT path FROM documents WHERE source_type='transcript' AND path LIKE '%#%' LIMIT 1"
        ).fetchone()
    finally:
        s.close()
    assert row is not None, "the fixture should index at least one transcript"

    status, body = _get(srv, _doc_url(row["path"]))
    assert status == 200
    assert body["kind"] == "transcript"
    assert body["chunk_count"] >= 1
    assert 'id="chunk-0"' in body["html"]
    # The archive directory it came from, cited so a claim can be checked.
    assert "#" not in body["source_path"]


def test_document_requires_a_path(served):
    cfg, srv = served
    status, body = _get(srv, "/api/doc")
    assert status == 400


# --------------------------------------------------------------------------
# Markdown rendering and scrubbing
# --------------------------------------------------------------------------

def test_markdown_renders_structure():
    html = render_markdown(textwrap.dedent("""\
        # Heading

        Some **bold** text and `code`.

        | a | b |
        |---|---|
        | 1 | 2 |

        ```python
        print("hi")
        ```
        """))
    assert "<h1" in html and "<strong>" in html and "<table>" in html and "<code" in html


@pytest.mark.parametrize("payload,forbidden", [
    ("<script>alert(1)</script>", "alert"),
    ('<img src=x onerror="steal()">', "onerror"),
    ('<iframe src="http://evil"></iframe>', "<iframe"),
    ('<a href="javascript:alert(1)">x</a>', "javascript:"),
    ("<style>body{display:none}</style>", "display:none"),
])
def test_indexed_content_cannot_smuggle_script_into_the_page(payload, forbidden):
    """4,600 unaudited Markdown files are rendered by this server.

    CSP is the real control, but the scrubber is the layer that does not
    depend on the browser honouring a header.
    """
    out = render_markdown(f"# Doc\n\n{payload}\n")
    assert forbidden.lower() not in out.lower()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def test_dashboard_shape(served):
    cfg, srv = served
    status, d = _get(srv, "/api/dashboard")
    assert status == 200
    for key in ("generated_at", "projects", "recent_log", "recent_decisions",
                "open_questions", "health", "lexicon_root"):
        assert key in d, f"dashboard is missing {key}"
    h = d["health"]
    assert h["ok"] is True
    assert h["state"] in ("green", "amber", "red")
    assert h["documents"] > 0
    assert isinstance(h["sources"], list) and h["sources"]
    assert {"key", "status", "files_seen"} <= set(h["sources"][0])
    assert any(p["name"] == "forge" for p in d["projects"])


def test_dashboard_health_agrees_with_the_text_report(served):
    """`health()` is a second reader of the same tables as `build_report`.

    Duplication that nothing pins drifts. This is the pin.
    """
    from lexiconlocal.report import build_report, health

    cfg, srv = served
    h = health(cfg)
    text = "\n".join(build_report(cfg).lines)
    assert f"documents     : {h['documents']:,}" in text
    assert f"unique chunks : {h['chunks']:,}" in text
    assert f"embedded      : {h['embedded']:,}" in text
    if h["pending_embed"]:
        assert f"PENDING EMBED : {h['pending_embed']:,}" in text


def test_health_reports_no_index_without_raising(tmp_path, lexicon_tree):
    from lexiconlocal.report import health

    cfg = load_config(lexicon_tree / "config.yaml")
    h = health(cfg)          # nothing indexed in this fixture instance
    assert h["ok"] is False and h["state"] == "no-index"


def test_health_cache_invalidates_when_the_index_changes(served):
    from lexiconlocal.web import dashboard as dash

    cfg, srv = served
    first = dash.cached_health(cfg)
    assert dash.cached_health(cfg) is first, "identical state should reuse the cache"
    # Touching the database must be enough to invalidate: the key is
    # (mtime_ns, size), not a timeout.
    st = cfg.db_path.stat()
    os.utime(cfg.db_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert dash.cached_health(cfg, blocking=True) is not first


def test_stale_health_is_served_immediately_and_refreshed_behind_it(served):
    """An index write must not hand the next page load a near-second stall.

    The SessionEnd hook writes the database several times an hour; recomputing
    in the request would make a random reload pay 350-900 ms for a number that
    is almost always unchanged.
    """
    import time
    from lexiconlocal.web import dashboard as dash

    cfg, srv = served
    dash.cached_health(cfg, blocking=True)
    st = cfg.db_path.stat()
    os.utime(cfg.db_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))

    t0 = time.perf_counter()
    stale = dash.cached_health(cfg)
    elapsed = (time.perf_counter() - t0) * 1000
    assert stale.get("stale") is True, "the caller was not told the value is stale"
    assert elapsed < 50, f"a stale read blocked for {elapsed:.0f} ms"

    for _ in range(100):
        if not dash.cached_health(cfg).get("stale"):
            break
        time.sleep(0.05)
    assert dash.cached_health(cfg).get("stale") is not True, "background refresh never landed"


def test_first_ever_health_read_is_computed_not_guessed(served):
    from lexiconlocal.web import dashboard as dash

    cfg, srv = served
    dash._HEALTH_CACHE.clear()
    h = dash.cached_health(cfg)
    assert h["ok"] is True and "stale" not in h


# --------------------------------------------------------------------------
# HOME.md generator
# --------------------------------------------------------------------------

def test_home_md_is_marked_generated_and_never_hand_edited(served):
    cfg, srv = served
    target = write_home(cfg)
    text = target.read_text(encoding="utf-8")
    assert target.name == "HOME.md"
    assert text.startswith("---\ngenerated: true")
    assert "DO NOT EDIT" in text
    assert "## Projects" in text and "## Recent activity" in text


def test_generated_home_md_never_enters_the_corpus(served):
    """A generated file at boost 3.0 would outrank the notes it summarises.

    `lexicon` documents carry the highest static boost in the ranker, so an
    auto-written summary landing in that tier would push real curated notes
    below a paraphrase of themselves. Only projects/ and topics/ are walked,
    and this pins that.
    """
    cfg, srv = served
    write_home(cfg)
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=False, batch_size=8)
    s = Searcher(cfg, None)
    try:
        rows = s.conn.execute(
            "SELECT path FROM documents WHERE path LIKE ?", (f"%{cfg.lexicon_root}/HOME.md",)
        ).fetchall()
    finally:
        s.close()
    assert rows == [], "the generated HOME.md was indexed"


def test_home_md_and_the_dashboard_come_from_one_assembly(served):
    cfg, srv = served
    data = build_dashboard(cfg)
    md = render_home_md(data)
    for p in data["projects"]:
        assert p["name"] in md


# --------------------------------------------------------------------------
# Curated-note parsing
# --------------------------------------------------------------------------

def test_decision_parser_reads_status_and_supersession(tmp_path):
    p = tmp_path / "decisions.md"
    p.write_text(textwrap.dedent("""\
        ## D-2026-01-01-01 — First idea  [active]

        - Date: 2026-01-01
        - Status: superseded by D-2026-02-02-02 (2026-02-02) — replaced wholesale
        - Why: it was wrong

        ## D-2026-02-02-02 — Better idea  [active]

        - Date: 2026-02-02
        - Status: active
        - Why: it is right
        """), encoding="utf-8")
    ds = {d.id: d for d in notes.parse_decisions(p)}
    assert ds["D-2026-01-01-01"].status == "superseded"
    assert ds["D-2026-01-01-01"].superseded_by == ["D-2026-02-02-02"]
    assert ds["D-2026-02-02-02"].status == "active"
    assert ds["D-2026-02-02-02"].supersedes == ["D-2026-01-01-01"]


def test_decision_without_a_status_is_unknown_not_active(tmp_path):
    """Phase 3 found 52 of 82 entries missing Status. Guessing would lie."""
    p = tmp_path / "decisions.md"
    p.write_text("## D-2026-03-03-03 — Undated thought\n\n- Why: because\n", encoding="utf-8")
    (d,) = notes.parse_decisions(p)
    assert d.status == "unknown"
    assert d.date == "2026-03-03", "the id still carries the date"


def test_log_parser_splits_date_agent_and_heading(tmp_path):
    p = tmp_path / "log.md"
    p.write_text(textwrap.dedent("""\
        ## 2026-08-18 — claude-code — Exact clip placement verification

        - Goal: verify placement
        - Decisions: D-2026-08-18-01

        ## 2026-08-17 — codex — Earlier work

        - Goal: something else
        """), encoding="utf-8")
    entries = notes.parse_log(p)
    assert [e.date for e in entries] == ["2026-08-18", "2026-08-17"]
    assert entries[0].agent == "claude-code"
    assert entries[0].heading == "Exact clip placement verification"
    assert entries[0].decisions == ["D-2026-08-18-01"]


def test_open_questions_skips_the_resolved_trailer(tmp_path):
    p = tmp_path / "overview.md"
    p.write_text(textwrap.dedent("""\
        # Project

        ## Open questions

        - A real open question
        - Another one
          with a continuation line

        Resolved since Phase 1: embedding scope, vendored repos.
        """), encoding="utf-8")
    qs = notes.open_questions(p)
    assert len(qs) == 2
    assert qs[1].endswith("with a continuation line")
    assert not any(q.lower().startswith("resolved") for q in qs)


def test_malformed_notes_do_not_raise(tmp_path):
    p = tmp_path / "decisions.md"
    p.write_text("## not a decision heading\n\nrandom text\n### deeper\n", encoding="utf-8")
    assert notes.parse_decisions(p) == []
    assert notes.parse_log(tmp_path / "missing.md") == []
    assert notes.open_questions(tmp_path / "missing.md") == []


def test_cross_project_decision_feed_identifies_the_project(served):
    """Decision ids are unique per project, not per Lexicon.

    Two projects minted D-2026-08-19-06 on the same day. A feed that shows the
    id alone makes those read as a duplicate or a conflict, so every entry in
    the cross-project feed carries its project.
    """
    cfg, srv = served
    root = cfg.lexicon_root / "projects"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "decisions.md").write_text(
            f"## D-2026-09-09-01 — {name} decision  [active]\n\n- Date: 2026-09-09\n"
            f"- Status: active\n- Why: because\n", encoding="utf-8")

    status, d = _get(srv, "/api/dashboard")
    assert status == 200
    clashing = [x for x in d["recent_decisions"] if x["id"] == "D-2026-09-09-01"]
    assert len(clashing) == 2, "both projects' decisions should appear"
    assert {x["project"] for x in clashing} == {"alpha", "beta"}

    md = render_home_md(d)
    assert "[alpha](projects/alpha/decisions.md)" in md
    assert "[beta](projects/beta/decisions.md)" in md


@pytest.mark.parametrize("text,ord_,expected", [
    ("ode wrote a thing", 3, "…wrote a thing"),
    ("vidence: `TODO.md` says", 4, "…`TODO.md` says"),
    ("Code wrote a thing", 3, "Code wrote a thing"),      # clean capital start
    ("# Heading here", 3, "# Heading here"),              # markdown heading
    ("- a bullet", 3, "- a bullet"),                      # list item
    ("| col | col |", 3, "| col | col |"),                # table row
    ("2026-08-19 entry", 3, "2026-08-19 entry"),          # date
    ("ode wrote a thing", 0, "ode wrote a thing"),        # first chunk: never trimmed
    ("supercalifragilisticexpialidocious rest", 3,
     "supercalifragilisticexpialidocious rest"),          # too long to be a fragment
])
def test_excerpt_trimming_only_eats_obvious_fragments(text, ord_, expected):
    """Cosmetic trimming must fail in the safe direction: never hide content."""
    from lexiconlocal.web.api import _trim_leading_partial_word

    assert _trim_leading_partial_word(text, ord_) == expected


@pytest.mark.parametrize("heading,expect_id,expect_title", [
    ("## D-2026-04-10-B01 — Separate Sorter from Ledger [active]",
     "D-2026-04-10-B01", "Separate Sorter from Ledger"),
    ("## D-2026-04-18-TM05 — Engine/CLI first; GUI later [active]",
     "D-2026-04-18-TM05", "Engine/CLI first; GUI later"),
    ("## D-2026-04-29-AD01 — Isolate audio feasibility [active]",
     "D-2026-04-29-AD01", "Isolate audio feasibility"),
    ("## D-2026-08-18-M01 — A missed window is missed [active]",
     "D-2026-08-18-M01", "A missed window is missed"),
    ("## D-2026-08-18-01 — Plain Markdown plus git  [active]",
     "D-2026-08-18-01", "Plain Markdown plus git"),
])
def test_decision_ids_carry_their_series_suffix(tmp_path, heading, expect_id, expect_title):
    """Most projects letter their decision series; only two use bare numbers.

    A numeric-only id pattern truncated `D-2026-04-10-B01` to `D-2026-04-10-`,
    which pushed `B01` into the title, collapsed eleven unrelated decisions of
    one project into a single id, and stopped supersession references from
    resolving anywhere outside kb-self.
    """
    p = tmp_path / "decisions.md"
    p.write_text(f"{heading}\n\n- Date: 2026-04-10\n- Status: active\n- Why: x\n",
                 encoding="utf-8")
    (d,) = notes.parse_decisions(p)
    assert d.id == expect_id
    assert d.title == expect_title


def test_supersession_resolves_across_lettered_ids(tmp_path):
    p = tmp_path / "decisions.md"
    p.write_text(textwrap.dedent("""\
        ## D-2026-04-10-B01 — First  [active]

        - Date: 2026-04-10
        - Status: superseded by D-2026-04-11-B02 — replaced

        ## D-2026-04-11-B02 — Second  [active]

        - Date: 2026-04-11
        - Status: active
        """), encoding="utf-8")
    ds = {d.id: d for d in notes.parse_decisions(p)}
    assert ds["D-2026-04-10-B01"].superseded_by == ["D-2026-04-11-B02"]
    assert ds["D-2026-04-11-B02"].supersedes == ["D-2026-04-10-B01"]


def test_ids_are_unique_within_every_real_project():
    """Guards the parser against silently collapsing a project's decisions.

    Runs against the live Lexicon when present; skipped elsewhere so the suite
    stays hermetic on a fresh machine.
    """
    base = Path.home() / "Lexicon" / "projects"
    if not base.is_dir():
        pytest.skip("no live Lexicon on this machine")
    for pdir in sorted(x for x in base.iterdir() if x.is_dir()):
        ds = notes.parse_decisions(pdir / "decisions.md")
        ids = [d.id for d in ds]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"{pdir.name} has duplicate decision ids: {sorted(dupes)}"
