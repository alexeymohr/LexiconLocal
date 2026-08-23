"""Dashboard assembly: one answer to "what is going on across everything".

This is the view no editor can give. Obsidian sees curated Markdown; the index
sees every transcript; neither shows both plus whether the machinery that
produced them is healthy. Putting the three side by side is the whole reason
Phase 4 exists.

The same data backs `GET /api/dashboard`, the Home page, and the optional
generated `HOME.md` -- one assembly function, three renderings, so the page and
the file can never disagree.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..report import health
from ..distill import distillation_backlog
from . import notes

#: Health is expensive to compute -- the integrity anti-join alone scans every
#: occurrence row -- and it can only change when something writes the index.
#: So it is cached against the database's (mtime_ns, size): an indexer run
#: moves both, a page reload moves neither.
#:
#: Invalidation is *stale-while-revalidate*, not recompute-in-the-request.
#: Measured on the live index, a cold recompute costs 350-900 ms depending on
#: the page cache, and the SessionEnd hook writes the database several times an
#: hour -- so a strict cache would hand a random page load a near-second stall
#: for a number that is almost always unchanged. The first ever call computes
#: synchronously; afterwards a reader gets the previous answer immediately,
#: flagged `stale`, while one background thread refreshes it.
_HEALTH_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_HEALTH_LOCK = threading.Lock()
_HEALTH_INFLIGHT: set[str] = set()

#: How much of each list the dashboard carries. Deliberately small: this view
#: answers "what is going on", and a hundred rows answers nothing.
RECENT_LOG_ENTRIES = 12
RECENT_DECISIONS = 12
MAX_QUESTIONS_PER_PROJECT = 6


@dataclass
class ProjectSummary:
    name: str
    path: str
    last_activity: str | None
    entry_count: int
    decision_count: int
    active_decisions: int
    open_questions: int
    has_overview: bool
    status_line: str | None

    def as_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path,
            "last_activity": self.last_activity,
            "log_entries": self.entry_count,
            "decisions": self.decision_count,
            "active_decisions": self.active_decisions,
            "open_questions": self.open_questions,
            "has_overview": self.has_overview,
            "status": self.status_line,
        }


#: `**bold**`, `*em*`, `` `code` `` -- the status line is shown as plain text in
#: a table row, so its Markdown has to be unwrapped rather than escaped and
#: displayed as source.
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")


def _status_line(overview: Path) -> str | None:
    """The `**Status:** ...` line most overviews carry near the top."""
    if not overview.exists():
        return None
    for line in overview.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
        low = line.lower()
        if not (low.startswith("**status:**") or low.startswith("status:")):
            continue
        text = _EMPHASIS.sub("", line.split(":", 1)[1]).strip()
        # Overviews hard-wrap, so this line is usually a fragment. Saying so
        # with an ellipsis is better than looking like a truncation bug.
        if text and text[-1] not in ".!?":
            text += "…"
        return text
    return None


def project_summary(pdir: Path) -> ProjectSummary:
    log = parse_log_safe(pdir / "log.md")
    decisions = notes.parse_decisions(pdir / "decisions.md")
    overview = pdir / "overview.md"
    return ProjectSummary(
        name=pdir.name,
        path=str(pdir),
        last_activity=log[0].date if log else None,
        entry_count=len(log),
        decision_count=len(decisions),
        active_decisions=sum(1 for d in decisions if d.status == "active"),
        open_questions=len(notes.open_questions(overview)),
        has_overview=overview.exists(),
        status_line=_status_line(overview),
    )


def parse_log_safe(path: Path):
    try:
        return notes.parse_log(path)
    except Exception:  # noqa: BLE001 - a malformed log must not blank the page
        return []


def _db_key(cfg: Config) -> tuple[int, int] | None:
    try:
        st = cfg.db_path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _refresh_health(cfg: Config, key: tuple[int, int]) -> None:
    name = str(cfg.db_path)
    try:
        value = health(cfg)
        with _HEALTH_LOCK:
            # Re-read the key: the index may have been written again while this
            # was computing, in which case what was just measured is already
            # stale and storing it under the new key would pin a wrong answer.
            _HEALTH_CACHE[name] = (_db_key(cfg) or key, value)
    except Exception:  # noqa: BLE001 - a failed refresh keeps the last good value
        pass
    finally:
        with _HEALTH_LOCK:
            _HEALTH_INFLIGHT.discard(name)


def cached_health(cfg: Config, *, blocking: bool = False) -> dict:
    """Health for a page load. Never blocks except on the very first call."""
    key = _db_key(cfg)
    if key is None:
        return health(cfg)
    name = str(cfg.db_path)

    with _HEALTH_LOCK:
        slot = _HEALTH_CACHE.get(name)
        if slot and slot[0] == key:
            return slot[1]
        if slot is None or blocking:
            fresh_needed = True
        else:
            fresh_needed = False
            if name not in _HEALTH_INFLIGHT:
                _HEALTH_INFLIGHT.add(name)
                spawn = True
            else:
                spawn = False

    if fresh_needed:
        value = health(cfg)
        with _HEALTH_LOCK:
            _HEALTH_CACHE[name] = (key, value)
        return value

    if spawn:
        threading.Thread(target=_refresh_health, args=(cfg, key), daemon=True).start()
    return {**slot[1], "stale": True}


#: Enough to act on, not so many that the Home page becomes a to-do list.
BACKLOG_SHOWN = 8


def build_dashboard(cfg: Config) -> dict:
    """Everything Home needs, in one pass over the curated notes."""
    projects: list[ProjectSummary] = []
    recent_log: list[dict] = []
    recent_decisions: list[dict] = []
    questions: list[dict] = []

    for pdir in notes.project_dirs(cfg.lexicon_root):
        summary = project_summary(pdir)
        projects.append(summary)

        for e in parse_log_safe(pdir / "log.md")[:RECENT_LOG_ENTRIES]:
            d = e.as_dict()
            d["project"] = pdir.name
            # The feed wants a headline, not a whole session write-up.
            d["body"] = _first_sentence(e.body)
            recent_log.append(d)

        for dec in notes.parse_decisions(pdir / "decisions.md")[:RECENT_DECISIONS]:
            d = dec.as_dict()
            d["project"] = pdir.name
            d["body"] = _first_sentence(dec.body)
            recent_decisions.append(d)

        for q in notes.open_questions(pdir / "overview.md")[:MAX_QUESTIONS_PER_PROJECT]:
            questions.append({"project": pdir.name, "question": q})

    # Recency across projects, not within one.
    projects.sort(key=lambda p: (p.last_activity or "", p.name), reverse=True)
    recent_log.sort(key=lambda e: (e["date"], e["project"]), reverse=True)
    recent_decisions.sort(key=lambda d: (d["date"] or "", d["id"]), reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lexicon_root": str(cfg.lexicon_root),
        "projects": [p.as_dict() for p in projects],
        "recent_log": recent_log[:RECENT_LOG_ENTRIES],
        "recent_decisions": recent_decisions[:RECENT_DECISIONS],
        "open_questions": questions,
        # Projects the index knows about but the curated layer does not.
        # DESIGN.md §7 keeps distillation lazy on purpose; this only makes the
        # backlog visible instead of implicit (Phase 5 D5).
        "distillation_backlog": [e.as_dict()
                                 for e in distillation_backlog(cfg, limit=BACKLOG_SHOWN)],
        "health": cached_health(cfg),
    }


def _first_sentence(body: str, limit: int = 240) -> str:
    """The first meaningful bullet or line of an entry body."""
    for line in body.splitlines():
        t = line.strip().lstrip("-*").strip()
        if not t:
            continue
        return t[:limit] + ("…" if len(t) > limit else "")
    return ""


# ---------------------------------------------------------------------------
# generated HOME.md
# ---------------------------------------------------------------------------

HOME_HEADER = """---
generated: true
generator: lexicon dashboard --write-home
generated_at: {ts}
---

<!-- DO NOT EDIT. This file is regenerated from the Lexicon's own notes and
     index. Edit the sources, not this file: projects/<name>/overview.md,
     decisions.md, log.md. -->

# Lexicon — Home
"""


def render_home_md(data: dict) -> str:
    """The dashboard as Markdown, for editors and agents.

    Marked `generated: true` in front matter and stamped DO NOT EDIT, because
    an append-only knowledge base and a file that gets overwritten every night
    must be impossible to confuse. Everything here is a pointer to a real note;
    nothing originates in this file.
    """
    h = data["health"]
    out = [HOME_HEADER.format(ts=data["generated_at"])]

    if h.get("ok"):
        integ = h["integrity"]
        state = {"green": "healthy", "amber": "degraded", "red": "DAMAGED"}.get(
            h["state"], h["state"]
        )
        out.append(
            f"**Index:** {state} — {h['documents']:,} documents · "
            f"{h['chunks']:,} chunks · {h['embedded']:,} embeddings"
            + (f" · **{h['pending_embed']:,} pending embed**" if h["pending_embed"] else "")
            + (f" · **{integ['dangling_occurrences']:,} unretrievable chunks**"
               if integ["dangling_occurrences"] else "")
        )
        untested = [s["key"] for s in h["sources"] if s["status"] == "untested"]
        if untested:
            out.append(f"**Untested sources:** {', '.join(untested)}")
    else:
        out.append(f"**Index:** {h.get('detail', 'unavailable')}")

    out.append("\n## Projects\n")
    out.append("| Project | Last activity | Log | Decisions (active) | Open questions |")
    out.append("|---|---|---|---|---|")
    for p in data["projects"]:
        out.append(
            f"| [{p['name']}](projects/{p['name']}/overview.md) "
            f"| {p['last_activity'] or '—'} | {p['log_entries']} "
            f"| {p['decisions']} ({p['active_decisions']}) | {p['open_questions']} |"
        )

    backlog = data.get("distillation_backlog") or []
    if backlog:
        out.append("\n## Not yet distilled\n")
        out.append("Indexed and searchable, but with no `projects/<name>/` notes — an agent "
                   "landing here must read transcripts to learn what happened. "
                   "`lexicon distill --suggest` prints the pass prompt.\n")
        out.append("| Project | Documents | Last activity |")
        out.append("|---|---:|---|")
        for e in backlog:
            out.append(f"| {e['project']} | {e['documents']:,} "
                       f"| {e['last_activity'] or '—'} |")

    out.append("\n## Recent activity\n")
    for e in data["recent_log"]:
        agent = f" · {e['agent']}" if e.get("agent") else ""
        out.append(f"- **{e['date']}** — [{e['project']}](projects/{e['project']}/log.md)"
                   f"{agent} — {e['heading']}")

    out.append("\n## Recent decisions\n")
    # The project is part of a decision's identity across the Lexicon: ids are
    # unique within a project only, and two projects have minted the same id on
    # the same day.
    for d in data["recent_decisions"]:
        mark = {"active": "", "superseded": " ~~superseded~~"}.get(d["status"], f" _{d['status']}_")
        out.append(f"- `{d['id']}` [{d['project']}](projects/{d['project']}/decisions.md) — "
                   f"{d['title']}{mark}")

    if data["open_questions"]:
        out.append("\n## Open questions\n")
        for q in data["open_questions"]:
            out.append(f"- **{q['project']}** — {q['question']}")

    out.append("")
    return "\n".join(out)


def write_home(cfg: Config, data: dict | None = None) -> Path:
    data = data or build_dashboard(cfg)
    target = cfg.lexicon_root / "HOME.md"
    target.write_text(render_home_md(data), encoding="utf-8")
    return target
