"""Command line interface: ``lexicon index | search | report | web | dashboard``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .embed import DEFAULT_HOST, DEFAULT_MODEL, EmbedError, Embedder
from .indexer import Indexer
from .lock import IndexLock
from .preflight import run_preflight
from .report import build_report
from .search import Searcher


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama embedding model")
    p.add_argument("--host", default=DEFAULT_HOST, help="Ollama host (localhost only)")


def cmd_preflight(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    checks, code = run_preflight(
        cfg, model=args.model, host=args.host, autostart=not args.no_autostart
    )
    print("lexicon preflight")
    for c in checks:
        print(c.line())
    print(f"  => {'ready' if code == 0 else 'NOT READY'}")
    return code


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a Lexicon root. Never overwrites."""
    from .init import init_root

    repo = Path(__file__).resolve().parent.parent.parent
    try:
        lines = init_root(Path(args.root), repo=repo, git=not args.no_git)
    except FileExistsError as e:
        print(str(e), file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    """Report on the unattended automation, optionally as the watchdog.

    Kept separate from `preflight` because the watchdog runs from the SessionEnd
    hook, which must stay fast and must not start Ollama.
    """
    from .agents import agent_states, detection_record, watchdog

    cfg = load_config(args.config)
    state_dir = cfg.index_dir / "state"
    if args.watchdog:
        states, record_path = watchdog(state_dir, quiet=args.quiet)
    else:
        states = agent_states()
        record_path = None

    if args.json:
        print(json.dumps(detection_record(states), indent=2))
    elif not args.quiet:
        print("lexicon launch agents")
        for s in states:
            print(f"  [{'OK ' if s.ok else 'FAIL'}] {s.label:<34} "
                  f"{s.problem() or s.purpose}")
        if record_path:
            print(f"  detection recorded in {record_path}")
    return 0 if all(s.ok for s in states) else 2


def cmd_index(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)

    # Single-instance: a hook firing during the nightly job is a skip, not a
    # failure, so this exits 0 (D-2026-08-18-16).
    lock = IndexLock(cfg.index_dir)
    res = lock.acquire()
    if not res.acquired:
        print(res.message)
        return 0
    try:
        with Embedder(model=args.model, host=args.host, batch_size=args.batch_size) as emb:
            indexer = Indexer(cfg, emb, verbose=not args.quiet)
            summary = indexer.run(
                full=args.full, batch_size=args.batch_size, embed=not args.no_embed
            )
    except EmbedError as e:
        print(f"\nEMBEDDING UNAVAILABLE: {e}", file=sys.stderr)
        return 2
    finally:
        lock.release()

    print()
    print("Index summary")
    print("-" * 60)
    print(f"  files seen      : {summary['files_seen']:,}")
    print(f"  files parsed    : {summary['files_parsed']:,}")
    print(f"  unchanged       : {summary['files_unchanged']:,}")
    print(f"  skipped (empty) : {summary['files_skipped']:,}")
    print(f"  documents written: {summary['docs_written']:,}")
    print(f"  documents pruned : {summary['documents_pruned']:,}")
    print(f"  chunks written  : {summary['chunks_written']:,}")
    print(f"  chunks embedded : {summary['chunks_embedded']:,}")
    print(f"  encoding fallbacks: {summary['encoding_fallbacks']}")
    print(f"  errors          : {summary['errors']}")
    print(f"  elapsed         : {summary['elapsed_sec']:,.1f}s")
    if args.json:
        print(json.dumps(summary, indent=2))
    if summary.get("embed_error"):
        print(
            f"\nEMBEDDING UNAVAILABLE: {summary['embed_error']}\n"
            f"Stage 1 completed and is stored; prose chunks remain pending. "
            f"`lexicon report` will exit non-zero until they are embedded.",
            file=sys.stderr,
        )
        return 2
    if summary["errors"]:
        print("\nErrors occurred. Run `lexicon report` for the full list.", file=sys.stderr)
        return 1
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    emb = None
    if not args.no_vector:
        try:
            emb = Embedder(model=args.model, host=args.host)
            emb.preflight()
        except EmbedError as e:
            print(f"(vector leg unavailable, falling back to lexical only: {e})", file=sys.stderr)
            emb = None
    try:
        searcher = Searcher(cfg, emb)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        results = searcher.search(
            args.query,
            project=args.project,
            source_type=args.source_type,
            after=args.after,
            before=args.before,
            limit=args.limit,
            kind=args.kind,
        )
    except RuntimeError as e:
        print(f"REFUSING TO SEARCH: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    elif not results:
        print("No results.")
    else:
        for i, r in enumerate(results, 1):
            print(f"\n{i}. [{r.score:.4f}] {r.source_type}"
                  f"{'/' + r.chunk_kind if r.chunk_kind != 'prose' else ''}"
                  f"  project={r.project or '-'}  date={r.doc_date or '-'}"
                  f"  via={'+'.join(r.matched_by)}")
            if r.title:
                print(f"   {r.title}")
            print(f"   {r.path}  (chunk {r.chunk_ord})")
            excerpt = r.excerpt.replace("\n", "\n   ")
            print(f"   {excerpt}")
    if emb:
        emb.close()
    searcher.close()
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    searcher = Searcher(cfg, None)
    data = searcher.read(args.path, args.chunk_ord, args.context)
    print(json.dumps(data, indent=2) if args.json else data.get("text", data.get("error", "")))
    searcher.close()
    return 0 if "error" not in data else 1


WEB_LOCK_NAME = "lexicon-web.lock"


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the read-only local UI until Ctrl-C.

    The lock is taken *before* the socket so that a second invocation gets the
    running server's URL rather than a bare "address already in use" -- the
    port collision is the backstop, not the mechanism.
    """
    from .web.server import BindRefused, WebConfig, open_in_browser, serve

    cfg = load_config(args.config)
    web_cfg = WebConfig(cfg=cfg, port=args.port, bind=args.bind,
                        open_browser=not args.no_open)
    try:
        web_cfg.validate()
    except BindRefused as e:
        print(f"REFUSING TO BIND: {e}", file=sys.stderr)
        return 2

    lock = IndexLock(cfg.index_dir, name=WEB_LOCK_NAME, label="lexicon web server")
    res = lock.acquire(payload=web_cfg.url)
    if not res.acquired:
        print(res.message)
        print("Open that URL, or stop the other server first.")
        return 0

    try:
        try:
            server = serve(web_cfg, quiet=args.quiet)
        except OSError as e:
            print(f"could not bind {web_cfg.bind}:{web_cfg.port}: {e}", file=sys.stderr)
            return 2
        print(f"Lexicon web  {server.url}")
        print(f"  root      {cfg.lexicon_root}")
        if server.readers.embed_error:
            print(f"  WARNING   vector search unavailable: {server.readers.embed_error}",
                  file=sys.stderr)
            print("            lexical search still works; results will say so.",
                  file=sys.stderr)
        print("  read-only, localhost only. Ctrl-C to stop.")
        if web_cfg.open_browser:
            open_in_browser(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            server.shutdown()
    finally:
        lock.release()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .web.dashboard import build_dashboard, render_home_md, write_home

    cfg = load_config(args.config)
    data = build_dashboard(cfg)
    if args.write_home:
        target = write_home(cfg, data)
        print(f"wrote {target}")
        return 0
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(render_home_md(data))
    return 0


def cmd_distill(args: argparse.Namespace) -> int:
    """Rank the projects that have raw material but no distilled notes.

    DESIGN.md §7 keeps distillation lazy on purpose. This does not change that;
    it just stops the backlog from being invisible.
    """
    from .distill import alias_suppressions, distill_prompt, distillation_backlog

    cfg = load_config(args.config)
    backlog = distillation_backlog(cfg, limit=args.limit)

    if args.suggest:
        target = args.project or (backlog[0].project if backlog else None)
        if not target:
            print("Nothing to distil — every indexed project has notes.")
            return 0
        print(f"# Distillation pass for {target}\n")
        print(distill_prompt(cfg, target))
        return 0

    if args.json:
        print(json.dumps([e.as_dict() for e in backlog], indent=2))
        return 0

    if not backlog:
        print("Nothing to distil — every indexed project has notes.")
        _print_suppressions(alias_suppressions(cfg))
        return 0
    print(f"{len(backlog)} project(s) with raw material but no distilled notes")
    print(f"{'project':<28} {'docs':>6} {'chunks':>8} {'repo-doc':>9} "
          f"{'transcript':>11} {'last seen':>11} {'rank':>7}")
    print("-" * 88)
    for e in backlog:
        print(f"{e.project[:28]:<28} {e.documents:>6,} {e.chunks:>8,} "
              f"{e.repo_docs:>9,} {e.transcripts:>11,} "
              f"{(e.last_activity or '-'):>11} {e.score:>7.1f}")
    print()
    print("Ranked by volume decayed against recency. "
          "`lexicon distill --suggest` prints the pass prompt for the top one.")
    _print_suppressions(alias_suppressions(cfg))
    return 0


def _print_suppressions(suppressed: list) -> None:
    """Say what the backlog is leaving out, and on whose authority.

    An omission nobody can see cannot be challenged. These are the projects
    `historical_aliases` declares to be old names of something already
    distilled -- true of a rename, wrong for a predecessor or a spike, and the
    difference is a judgement only the operator can make.
    """
    if not suppressed:
        return
    print()
    print(f"{len(suppressed)} project(s) omitted as declared renames "
          f"(historical_aliases in config.yaml):")
    for s in suppressed:
        print(f"  {s.project[:28]:<28} {s.documents:>6,} docs   "
              f"treated as {s.distilled_as}")
    print("Remove an entry there if it is a separate body of work rather than "
          "an old name, and it returns to the backlog.")


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    rep = build_report(cfg)
    print("\n".join(rep.lines))
    return rep.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lexicon", description="Local Lexicon index")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="index all configured sources")
    _add_common(p_index)
    p_index.add_argument("--full", action="store_true", help="drop and rebuild everything")
    p_index.add_argument("--batch-size", type=int, default=32)
    p_index.add_argument("--no-embed", action="store_true", help="parse and store only")
    p_index.add_argument("--quiet", action="store_true")
    p_index.add_argument("--json", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="hybrid search over the index")
    _add_common(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--project")
    p_search.add_argument("--source-type", dest="source_type")
    p_search.add_argument("--kind", choices=["prose", "tool_event"])
    p_search.add_argument("--after")
    p_search.add_argument("--before")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--no-vector", action="store_true", help="lexical only")
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser("read", help="read an indexed document or chunk window")
    _add_common(p_read)
    p_read.add_argument("path")
    p_read.add_argument("--chunk-ord", dest="chunk_ord", type=int, default=None)
    p_read.add_argument("--context", type=int, default=2)
    p_read.add_argument("--json", action="store_true")
    p_read.set_defaults(func=cmd_read)

    p_distill = sub.add_parser(
        "distill", help="projects with raw material but no distilled notes"
    )
    _add_common(p_distill)
    p_distill.add_argument("--limit", type=int, default=None)
    p_distill.add_argument("--project", help="target a specific project with --suggest")
    p_distill.add_argument("--suggest", action="store_true",
                           help="print the DESIGN §7 distillation prompt, pre-filled")
    p_distill.add_argument("--json", action="store_true")
    p_distill.set_defaults(func=cmd_distill)

    p_report = sub.add_parser("report", help="coverage and health report")
    _add_common(p_report)
    p_report.set_defaults(func=cmd_report)

    p_web = sub.add_parser("web", help="serve the read-only local web UI")
    _add_common(p_web)
    p_web.add_argument("--port", type=int, default=8377)
    p_web.add_argument("--bind", default="127.0.0.1",
                       help="loopback only; any other address is refused")
    p_web.add_argument("--no-open", action="store_true", help="do not open a browser")
    p_web.add_argument("--quiet", action="store_true", help="do not log requests")
    p_web.set_defaults(func=cmd_web)

    p_dash = sub.add_parser("dashboard", help="the Home view as Markdown, JSON, or HOME.md")
    _add_common(p_dash)
    p_dash.add_argument("--write-home", action="store_true",
                        help="write a generated HOME.md at the Lexicon root")
    p_dash.add_argument("--json", action="store_true")
    p_dash.set_defaults(func=cmd_dashboard)

    p_init = sub.add_parser("init", help="create a new Lexicon root (never overwrites)")
    p_init.add_argument("root", nargs="?", default="~/Lexicon",
                        help="where to create it (default ~/Lexicon)")
    p_init.add_argument("--no-git", action="store_true", help="do not git init the root")
    p_init.set_defaults(func=cmd_init)

    p_agents = sub.add_parser(
        "agents", help="check the LaunchAgents that run capture unattended"
    )
    _add_common(p_agents)
    p_agents.add_argument("--watchdog", action="store_true",
                          help="on failure, append a detection record and notify")
    p_agents.add_argument("--json", action="store_true")
    p_agents.add_argument("--quiet", action="store_true")
    p_agents.set_defaults(func=cmd_agents)

    p_pre = sub.add_parser(
        "preflight", help="check Ollama, model, database and Lexicon repo before a run"
    )
    _add_common(p_pre)
    p_pre.add_argument("--no-autostart", action="store_true",
                       help="do not try to start Ollama if it is down")
    p_pre.set_defaults(func=cmd_preflight)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
