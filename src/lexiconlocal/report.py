"""Coverage and health reporting.

The governing requirement (CLAUDE.md): "nothing new" and "importer broke" must
be unmistakably different. So the report always states, per source, whether it
produced documents, whether it errored, and whether it has ever seen real data
at all -- a source with zero files is UNTESTED, which is neither healthy nor
broken and must not be reported as either.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import db as dbmod
from .chunk import KIND_PROSE, KIND_TOOL_EVENT
from .config import Config
from .indexer import SOURCE_KEYS

#: Sources that cannot work yet because the data has not arrived.
PENDING_SOURCES = {
    "chatgpt": "ChatGPT account export has not been dropped into archive/chatgpt/",
    "claude-export": "Claude account export has not been dropped into archive/claude/",
    "claude-memory": "Claude account export has not been dropped into archive/claude/",
    "claude-project": "Claude account export has not been dropped into archive/claude/",
}


@dataclass
class Report:
    lines: list[str]
    exit_code: int


def health(cfg: Config) -> dict:
    """The same facts ``build_report`` prints, as data rather than as lines.

    A second reader of the same tables rather than a refactor of the printer:
    ``build_report`` interleaves formatting with exit-code accumulation and its
    verdict logic has tests behind it, so rewriting it to serve a UI would risk
    the health report to benefit the dashboard. The duplication is deliberate
    and is pinned by a test asserting the two agree on every shared number --
    the link that keeps them from drifting apart is that test, not convention.
    """
    if not cfg.db_path.exists():
        return {"ok": False, "state": "no-index", "detail": f"no database at {cfg.db_path}"}

    conn = dbmod.connect(cfg.db_path, read_only=True)
    try:
        one = lambda q, *a: conn.execute(q, a).fetchone()  # noqa: E731
        docs = one("SELECT COUNT(*) n FROM documents")["n"]
        chunks = one("SELECT COUNT(*) n FROM chunks")["n"]
        prose = one("SELECT COUNT(*) n FROM chunks WHERE kind=?", KIND_PROSE)["n"]
        events = one("SELECT COUNT(*) n FROM chunks WHERE kind=?", KIND_TOOL_EVENT)["n"]
        vecs = one("SELECT COUNT(*) n FROM chunk_vecs")["n"]
        pending = one(
            "SELECT COUNT(*) n FROM chunks WHERE embedded=0 AND kind=?", KIND_PROSE
        )["n"]
        by_type = {
            r["source_type"]: r["n"]
            for r in conn.execute(
                "SELECT source_type, COUNT(*) n FROM documents GROUP BY source_type"
            )
        }
        run = one("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1")
        per_source = {}
        if run and run["per_source_json"]:
            try:
                per_source = json.loads(run["per_source_json"])
            except ValueError:
                per_source = {}
        integ = dbmod.integrity_check(conn)

        errors = sum(len(v.get("errors") or []) for v in per_source.values())
        sources = []
        for key in SOURCE_KEYS:
            st = per_source.get(key, {})
            seen = st.get("files_seen", 0)
            status = (
                "untested" if not seen
                else "errors" if st.get("errors")
                else "ok"
            )
            sources.append({
                "key": key,
                "status": status,
                "files_seen": seen,
                "files_parsed": st.get("files_parsed", 0),
                "docs_written": st.get("docs_written", 0),
                "errors": len(st.get("errors") or []),
                "why": PENDING_SOURCES.get(key) if not seen else None,
            })

        damaged = bool(integ["dangling_occurrences"])
        if damaged or errors:
            state = "red"
        elif pending:
            state = "amber"
        else:
            state = "green"

        return {
            "ok": True,
            "state": state,
            "documents": docs,
            "chunks": chunks,
            "prose_chunks": prose,
            "tool_event_chunks": events,
            "embedded": vecs,
            "pending_embed": pending,
            "documents_by_type": by_type,
            "sources": sources,
            "parse_errors": errors,
            "integrity": integ,
            "db_bytes": cfg.db_path.stat().st_size,
            "embed_model": dbmod.get_meta(conn, "embed_model"),
            "last_full_rebuild": dbmod.get_meta(conn, "last_full_rebuild"),
            "last_run": {
                "started": run["started"],
                "finished": run["finished"],
                "mode": run["mode"],
                "docs_written": run["docs_written"] or 0,
                "chunks_embedded": run["chunks_embedded"] or 0,
                "files_seen": run["files_seen"] or 0,
            } if run else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Capture freshness (Phase 5 D2)
# ---------------------------------------------------------------------------

#: Where transcripts live before they are archived. Codex reaches the archive
#: only through the nightly rsync; Claude Code also has the SessionEnd hook,
#: which is why the two lag very differently and why both are shown.
LIVE_CAPTURE_SOURCES: list[tuple[str, str, str]] = [
    ("codex", "~/.codex/sessions", "archive/codex/sessions"),
    ("claude-code", "~/.claude/projects", "archive/claude-code"),
]

#: The daily job runs at 03:30, so a full cycle of accumulation is normal and
#: must not raise an alarm. Past 26 hours it is not accumulation any more --
#: capture has stopped, which is what happened on 2026-08-19 and went unseen.
CAPTURE_LAG_ALARM_HOURS = 26.0


def _newest_mtime(root: Path, pattern: str = "*.jsonl") -> float | None:
    newest: float | None = None
    if not root.exists():
        return None
    for f in root.rglob(pattern):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def capture_freshness(
    cfg: Config, sources: list[tuple[str, str, str]] | None = None
) -> list[dict]:
    """How far behind the archive is, per live transcript source.

    rsync preserves mtimes, so the newest archived mtime is literally the newest
    live mtime as of the last sync -- the difference between the two is the
    capture lag, with no clock arithmetic to get wrong.
    """
    out: list[dict] = []
    for name, live_str, archive_rel in (sources or LIVE_CAPTURE_SOURCES):
        live_root = Path(live_str).expanduser()
        arch_root = cfg.lexicon_root / archive_rel
        live = _newest_mtime(live_root)
        arch = _newest_mtime(arch_root)
        if live is None:
            out.append({"source": name, "state": "no live source",
                        "live_root": str(live_root), "lag_hours": None})
            continue
        if arch is None:
            out.append({"source": name, "state": "NEVER ARCHIVED",
                        "live_root": str(live_root), "lag_hours": None})
            continue
        lag = max(0.0, (live - arch) / 3600.0)
        out.append({
            "source": name,
            "state": "stalled" if lag > CAPTURE_LAG_ALARM_HOURS else "ok",
            "live_root": str(live_root),
            "lag_hours": lag,
            "newest_live": time.strftime("%Y-%m-%d %H:%M", time.localtime(live)),
            "newest_archived": time.strftime("%Y-%m-%d %H:%M", time.localtime(arch)),
        })
    return out


def build_report(cfg: Config) -> Report:
    out: list[str] = []
    exit_code = 0

    if not cfg.db_path.exists():
        return Report(
            [
                "IMPORTER STATE: NO INDEX",
                f"  No database at {cfg.db_path}",
                "  This is not 'nothing new' -- the index has never been built.",
                "  Run: lexicon index --full",
            ],
            1,
        )

    conn = dbmod.connect(cfg.db_path, read_only=True)
    out.append("Lexicon index report")
    out.append("=" * 72)

    model = dbmod.get_meta(conn, "embed_model")
    dims = dbmod.get_meta(conn, "embed_dims")
    schema = dbmod.get_meta(conn, "schema_version")
    last_full = dbmod.get_meta(conn, "last_full_rebuild")
    out.append(f"  database      : {cfg.db_path}")
    size_mb = cfg.db_path.stat().st_size / 1048576
    out.append(f"  size          : {size_mb:,.1f} MB")
    out.append(f"  schema        : v{schema}")
    out.append(f"  embed model   : {model} ({dims} dims)")
    out.append(f"  last full     : {last_full or 'never'}")

    docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
    chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
    occ = conn.execute("SELECT COUNT(*) n FROM occurrences").fetchone()["n"]
    prose = conn.execute("SELECT COUNT(*) n FROM chunks WHERE kind=?", (KIND_PROSE,)).fetchone()["n"]
    events = conn.execute("SELECT COUNT(*) n FROM chunks WHERE kind=?", (KIND_TOOL_EVENT,)).fetchone()["n"]
    vecs = conn.execute("SELECT COUNT(*) n FROM chunk_vecs").fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) n FROM chunks WHERE embedded=0 AND kind=?", (KIND_PROSE,)
    ).fetchone()["n"]

    out.append("")
    out.append(f"  documents     : {docs:,}")
    out.append(f"  unique chunks : {chunks:,}  (prose {prose:,} / tool_event {events:,})")
    out.append(f"  occurrences   : {occ:,}  (dedupe saved {occ - chunks:,} duplicate chunks)")
    out.append(f"  embedded      : {vecs:,}")
    if pending:
        out.append(f"  PENDING EMBED : {pending:,} prose chunks -- run `lexicon index` to resume")
        exit_code = max(exit_code, 1)

    # ---- integrity ---------------------------------------------------------
    integ = dbmod.integrity_check(conn)
    out.append("")
    if integ["dangling_occurrences"] or integ["orphaned_vectors"]:
        out.append("INDEX INTEGRITY: DAMAGED")
        if integ["dangling_occurrences"]:
            out.append(
                f"  {integ['dangling_occurrences']:,} occurrence(s) across "
                f"{integ['documents_with_holes']:,} document(s) reference chunk text that is "
                f"no longer stored — that content is in the archive but NOT retrievable."
            )
            out.append("  Repair: lexicon index --full  (the files are the truth)")
            exit_code = max(exit_code, 1)
        if integ["orphaned_vectors"]:
            out.append(f"  {integ['orphaned_vectors']:,} vector(s) with no chunk (harmless, cleaned on next index)")
    else:
        out.append("Index integrity: ok (no dangling occurrences, no orphaned vectors)")

    # ---- per source --------------------------------------------------------
    out.append("")
    out.append("Per-source coverage")
    out.append("-" * 72)

    run = conn.execute(
        "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    per_source = {}
    if run and run["per_source_json"]:
        try:
            per_source = json.loads(run["per_source_json"])
        except ValueError:
            per_source = {}

    doc_counts = {
        r["source_type"]: r["n"]
        for r in conn.execute(
            "SELECT source_type, COUNT(*) n FROM documents GROUP BY source_type"
        )
    }

    for key in SOURCE_KEYS:
        s = per_source.get(key, {})
        seen = s.get("files_seen", 0)
        parsed = s.get("files_parsed", 0)
        skipped = s.get("files_skipped", 0)
        unchanged = s.get("files_unchanged", 0)
        errs = s.get("error_count", 0)
        docs_w = s.get("docs_written", 0)

        if seen == 0 and parsed == 0:
            why = PENDING_SOURCES.get(key)
            if why:
                out.append(f"  {key:<14} UNTESTED   -- {why}")
            else:
                out.append(f"  {key:<14} UNTESTED   -- no files seen at this source")
            continue

        status = "ERRORS" if errs else "ok"
        if errs:
            exit_code = max(exit_code, 1)
        out.append(
            f"  {key:<14} {status:<10} seen={seen:<6} parsed={parsed:<6} "
            f"unchanged={unchanged:<6} skipped={skipped:<5} docs={docs_w:<6} errors={errs}"
        )
        for line in s.get("errors", [])[:10]:
            out.append(f"       ERROR: {line}")
        if errs > 10:
            out.append(f"       ... and {errs - 10} more errors")

    # ---- bundled-source batch surveys --------------------------------------
    # Account exports arrive as a bundle of files rather than one file per
    # document, so per-file classification is printed here: what was parsed,
    # what was excluded as PII, what was skipped on purpose, and -- the point
    # of the whole exercise -- anything the parser did not recognise.
    for key, heading in (
        ("claude-export", "Claude export batches"),
        ("chatgpt", "ChatGPT export batches"),
    ):
        surveys = per_source.get(key, {}).get("extra_notes") or []
        if not surveys:
            continue
        out.append("")
        out.append(heading)
        out.append("-" * 72)
        for sv in surveys:
            out.append(f"  {sv['batch']}")
            parsed = sv.get("parsed") or []
            shown = ", ".join(parsed[:4]) + (f", ... (+{len(parsed) - 4} more)" if len(parsed) > 4 else "")
            out.append(f"    parsed       : {shown or '(none)'}")
            out.append(f"    PII EXCLUDED : {', '.join(sv.get('pii_excluded') or []) or '(none)'}")
            for line in sv.get("skipped") or []:
                out.append(f"    skipped      : {line}")
            if sv.get("unknown"):
                out.append(f"    UNRECOGNISED : {', '.join(sv['unknown'])}  <-- not indexed")

    # ---- encoding fallbacks ------------------------------------------------
    fallbacks = []
    if run and run["fallback_encoding"]:
        try:
            fallbacks = json.loads(run["fallback_encoding"])
        except ValueError:
            fallbacks = []
    out.append("")
    if fallbacks:
        out.append(f"Encoding fallbacks ({len(fallbacks)} files read with errors='replace'):")
        for f in fallbacks[:30]:
            out.append(f"  {f}")
        if len(fallbacks) > 30:
            out.append(f"  ... and {len(fallbacks) - 30} more")
    else:
        out.append("Encoding fallbacks: none")

    # ---- redactions --------------------------------------------------------
    red_totals: dict[str, int] = {}
    for s in per_source.values():
        for kind, n in (s.get("redactions") or {}).items():
            red_totals[kind] = red_totals.get(kind, 0) + n
    out.append("")
    if red_totals:
        out.append("Redactions applied before storage:")
        for kind, n in sorted(red_totals.items(), key=lambda kv: -kv[1]):
            out.append(f"  {kind:<20} {n} documents")
    else:
        out.append("Redactions applied: none")

    # ---- capture freshness -------------------------------------------------
    # Indexing well is worthless if nothing new is arriving. This is the only
    # part of the report that looks outside the index at all.
    out.append("")
    out.append("Capture freshness (live source vs archive)")
    out.append("-" * 72)
    for f in capture_freshness(cfg):
        if f["lag_hours"] is None:
            out.append(f"  {f['source']:<14} {f['state']}: {f['live_root']}")
            if f["state"] == "NEVER ARCHIVED":
                exit_code = max(exit_code, 1)
            continue
        line = (f"  {f['source']:<14} newest live {f['newest_live']} | "
                f"newest archived {f['newest_archived']} | lag {f['lag_hours']:.1f}h")
        if f["state"] == "stalled":
            out.append(line)
            out.append(f"  {'':<14} CAPTURE STALLED -- nothing has been archived for "
                       f"{f['lag_hours']:.0f}h. Check `lexicon agents`.")
            exit_code = max(exit_code, 1)
        else:
            out.append(line)

    # ---- last run ----------------------------------------------------------
    out.append("")
    out.append("Last ingest run")
    out.append("-" * 72)
    if run is None:
        out.append("  IMPORTER STATE: no ingest run has ever been recorded.")
        exit_code = max(exit_code, 1)
    else:
        changed = (run["docs_written"] or 0)
        out.append(f"  mode          : {run['mode']}")
        out.append(f"  started       : {run['started']}")
        out.append(f"  finished      : {run['finished']}")
        out.append(f"  files seen    : {run['files_seen']:,}")
        out.append(f"  files parsed  : {run['files_parsed']:,}")
        out.append(f"  unchanged     : {run['files_unchanged']:,}")
        out.append(f"  docs written  : {changed:,}")
        out.append(f"  chunks written: {run['chunks_written']:,}")
        out.append(f"  chunks embedded: {run['chunks_embedded']:,}")
        total_errors = 0
        if run["errors_json"]:
            try:
                total_errors = sum(len(v) for v in json.loads(run["errors_json"]).values())
            except ValueError:
                total_errors = 0
        out.append("")
        if total_errors:
            out.append(f"  VERDICT: IMPORTER ERRORS -- {total_errors} file(s) failed to parse. See above.")
            exit_code = max(exit_code, 1)
        elif pending:
            # The 2026-08-19 03:30 run printed "VERDICT: HEALTHY" directly under
            # "PENDING EMBED : 2,560 prose chunks". The exit code was right, but a
            # verdict line that says HEALTHY while half the pipeline is dead is
            # exactly the silent failure this report exists to prevent.
            out.append(
                f"  VERDICT: DEGRADED -- parsing is fine ({changed:,} documents written) but "
                f"{pending:,} prose chunk(s) are unembedded. Semantic search is incomplete "
                f"until they are. Check `lexicon preflight`."
            )
        elif changed == 0 and (run["files_seen"] or 0) > 0:
            out.append("  VERDICT: NOTHING NEW -- every source was seen and was already current.")
        else:
            out.append(f"  VERDICT: HEALTHY -- {changed:,} documents written, no parse errors.")

    docs_by_type = ", ".join(f"{k}={v:,}" for k, v in sorted(doc_counts.items()))
    out.append("")
    out.append(f"Documents by source_type: {docs_by_type or '(none)'}")

    conn.close()
    return Report(out, exit_code)
