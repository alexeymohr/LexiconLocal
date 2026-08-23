#!/usr/bin/env python3
"""Leak guard: keep the operator's project names out of a public repository.

This code runs on a private corpus. The repository that holds the code is
public. The names of the operator's projects -- every directory under their
source roots, every curated project, every alias -- are exactly the thing that
must never appear in a commit, a comment, a test fixture or a doc.

Nobody maintains the list. It is **derived at run time** from the operator's
live Lexicon:

1. every top-level entry under every ``source_roots`` path in ``config.yaml``
2. every directory in ``<lexicon_root>/projects/``
3. every project row in ``<lexicon_root>/INDEX.md`` (catches renamed, nested
   and non-repo projects that are not a source-root directory)
4. every key and value in ``historical_aliases``
5. the operator's username

minus anything shorter than MIN_LEN or in a small generic-word filter. Each
name is matched case-insensitively on word boundaries, in its directory form
and its prose form (``Some_Thing`` also catches "Some Thing").

Two small private files tune it, both under ``<lexicon_root>/private/`` so they
are never indexed and never committed anywhere public:

* ``leak-allow.txt``  -- names to permit (one per line). The public repo's own
  name lands in the derived set because it lives under a source root; this is
  where it is allowed.
* ``leak-extra.txt``  -- names no filesystem knows: a client, an employer.

The guard catches names. It cannot catch meaning. The push-time review is the
answer to that; this is the floor beneath it.

Usage:
    leak_guard.py --stdin             scan text on stdin (commit-msg hook)
    leak_guard.py --staged            scan the staged diff (pre-commit hook)
    leak_guard.py --range A..B        scan commit messages + diff for a range
    leak_guard.py --tree [pathspec]   scan the working tree (audit gate)
    leak_guard.py --list              print the derived set and exit

Exit 0 = clean. Exit 1 = hits. Exit 2 = could not derive the set (no Lexicon
root, unreadable config) -- never silently passes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # the hook may run outside the venv
    yaml = None  # type: ignore[assignment]

DEFAULT_ROOT = Path(os.environ.get("LEXICON_ROOT", "~/Lexicon")).expanduser()
MIN_LEN = 5

#: Words that are plausibly a directory name AND plausibly ordinary prose.
#: Blocking these would make the guard cry wolf, and a guard that cries wolf
#: gets bypassed within a week.
GENERIC = {
    "anything", "archive", "archives", "assets", "backup", "backups", "build", "builds",
    "config", "configs", "data", "dist", "docs", "documents", "examples",
    "family", "index", "library", "notes", "output", "playground", "private",
    "project", "projects", "public", "research", "resources", "sandbox",
    "runtime", "scratch", "scripts", "source", "sources", "src", "staging", "template",
    "templates", "test", "tests", "testing", "tools", "topics", "utils",
    "vendor", "workspace",
    # An operator may legitimately have a project with a name like these --
    # a spike called "nothing" is real -- but the word is far too common in
    # prose to block. The filter applies to every source, aliases included.
    "nothing", "something", "everything", "default", "example", "sample",
}

_INDEX_ROW = re.compile(r"^\|\s*([A-Za-z0-9][A-Za-z0-9_ .-]*?)\s*\|", re.M)


def _read_list(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def derive(root: Path = DEFAULT_ROOT) -> tuple[set[str], list[str]]:
    """Return (protected names, notes about what was or was not readable)."""
    notes: list[str] = []
    names: set[str] = set()
    cfg_path = root / "config.yaml"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"no Lexicon config at {cfg_path}")
    if yaml is None:
        raise RuntimeError("PyYAML is not importable; run inside the project venv")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    for entry in cfg.get("source_roots") or []:
        p = Path(os.path.expandvars(str(entry.get("path", "")))).expanduser()
        if p.is_dir():
            names |= {e.name for e in p.iterdir() if not e.name.startswith(".")}
        else:
            notes.append(f"source root not present: {p}")

    proj = root / "projects"
    if proj.is_dir():
        names |= {e.name for e in proj.iterdir() if e.is_dir() and not e.name.startswith(".")}

    idx = root / "INDEX.md"
    if idx.is_file():
        for m in _INDEX_ROW.finditer(idx.read_text(encoding="utf-8", errors="replace")):
            cell = m.group(1).strip().strip("*`")
            if cell and cell.lower() not in {"project", "family", "name"} and not set(cell) <= {"-", ":"}:
                names.add(cell)

    for k, v in (cfg.get("historical_aliases") or {}).items():
        names |= {str(k), str(v)}

    names.add(Path.home().name)
    names |= _read_list(root / "private" / "leak-extra.txt")

    allow = {a.lower() for a in _read_list(root / "private" / "leak-allow.txt")}
    names = {
        n for n in names
        if len(n) >= MIN_LEN and n.lower() not in GENERIC and n.lower() not in allow
    }
    return names, notes


def _variants(name: str) -> set[str]:
    return {name, re.sub(r"[_-]+", " ", name)}


def compile_patterns(names: set[str]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for n in sorted(names, key=str.lower):
        alts = "|".join(re.escape(v) for v in _variants(n))
        out.append((n, re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.I)))
    return out


def scan(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[tuple[str, int, str]]:
    """Return (name, line_number, line) for every hit."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for name, pat in patterns:
            if pat.search(line):
                hits.append((name, i, line.strip()))
    return hits


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def gather(args: argparse.Namespace) -> str:
    if args.stdin:
        return sys.stdin.read()
    if args.staged:
        return _git("diff", "--cached", "--no-color", "-U0")
    if args.range:
        return (_git("log", "--format=%s%n%b", args.range)
                + "\n" + _git("diff", "--no-color", "-U0", args.range))
    if args.tree is not None:
        spec = args.tree or ["."]
        out = []
        for path in _git("ls-files", "-z", "--", *spec).split("\0"):
            if not path:
                continue
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8000]:
                continue  # binary
            out.append(f"--- {path}\n" + data.decode("utf-8", errors="replace"))
        return "\n".join(out)
    raise SystemExit("choose one of --stdin, --staged, --range, --tree, --list")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Lexicon root (default ~/Lexicon)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--stdin", action="store_true")
    g.add_argument("--staged", action="store_true")
    g.add_argument("--range")
    g.add_argument("--tree", nargs="*", metavar="PATHSPEC")
    g.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    try:
        names, notes = derive(args.root)
    except Exception as e:  # noqa: BLE001
        print(f"leak-guard: cannot derive the protected set: {e}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"leak-guard: note: {n}", file=sys.stderr)

    if args.list:
        for n in sorted(names, key=str.lower):
            print(n)
        print(f"({len(names)} protected names)", file=sys.stderr)
        return 0

    text = gather(args)
    hits = scan(text, compile_patterns(names))
    if not hits:
        return 0

    by_name: dict[str, list[tuple[int, str]]] = {}
    for name, ln, line in hits:
        by_name.setdefault(name, []).append((ln, line))
    print(f"leak-guard: {len(hits)} hit(s) on {len(by_name)} protected name(s):", file=sys.stderr)
    for name, rows in sorted(by_name.items(), key=lambda kv: kv[0].lower()):
        print(f"  {name}", file=sys.stderr)
        for ln, line in rows[:3]:
            print(f"      line {ln}: {line[:110]}", file=sys.stderr)
        if len(rows) > 3:
            print(f"      ... and {len(rows) - 3} more", file=sys.stderr)
    if os.environ.get("LEAK_GUARD", "").lower() == "off":
        print("leak-guard: LEAK_GUARD=off -- proceeding anyway, with the above in view",
              file=sys.stderr)
        return 0
    print("leak-guard: refusing. Fix the text, or LEAK_GUARD=off to override deliberately.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
