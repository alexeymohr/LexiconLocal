"""Parsing curated Lexicon notes into structures the UI can render.

`overview.md`, `decisions.md` and `log.md` follow the conventions in DESIGN.md
§4, but they are hand-written by several different agents and by the operator, and
Phase 3 already found the format drifting (D-2026-08-18-19: 52 of 82 decision
entries were missing `Date:`/`Status:`). So every parser here is **forgiving**:
anything it cannot make sense of degrades to a plainer rendering rather than
raising. A malformed heading must never blank a project page.

The one thing it does not do is guess. A decision with no parseable status is
reported as ``unknown``, not silently promoted to ``active`` -- status is the
mechanism supersession is expressed through, and inventing one would make the
UI lie about which decisions still stand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: `## D-2026-08-18-07 — Title of the decision  [active]`
#:
#: The suffix is **alphanumeric**, not numeric. Most projects number their
#: decisions with a series letter -- `D-2026-04-10-B01`, `D-2026-06-02-A01`,
#: `D-2026-04-18-TM05`, `D-2026-04-29-AD01`, `D-2026-08-18-M01` -- and only
#: a couple of projects use a bare number. A `[\d-]+` id
#: pattern truncates every one of the others at the trailing dash, which
#: silently turns `B01` into part of the title, collapses eleven unrelated
#: decisions into one id, and stops supersession references from resolving.
_ID = r"D-\d{4}-\d{2}-\d{2}-[A-Za-z0-9]+"
_DECISION_H2 = re.compile(rf"^##\s+({_ID})\s*[—\-–:]?\s*(.*?)\s*$")
#: A bracketed status anywhere in the heading tail.
_STATUS_TAG = re.compile(r"\[([a-z][a-z ]*)\]\s*$", re.IGNORECASE)
#: `- Status: superseded by D-2026-08-18-07 (2026-08-18) — ...`
_FIELD = re.compile(r"^-\s+(Date|Status|Why|Decision|Evidence|Constraint)\s*:\s*(.*)$", re.I)
_DREF = re.compile(rf"\b{_ID}\b")

#: `## 2026-08-18 — claude-code — Exact clip placement verification`
_LOG_H2 = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[—\-–]\s*(.*)$")

_H2_ANY = re.compile(r"^##\s+(.*?)\s*$")


@dataclass
class Decision:
    id: str
    title: str
    status: str              # active | superseded | unknown | <as written>
    date: str | None
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    body: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "date": self.date, "supersedes": self.supersedes,
            "superseded_by": self.superseded_by, "body": self.body,
        }


@dataclass
class LogEntry:
    date: str
    heading: str
    agent: str | None
    body: str
    decisions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"date": self.date, "heading": self.heading, "agent": self.agent,
                "body": self.body, "decisions": self.decisions}


def _split_sections(text: str, pattern: re.Pattern) -> list[tuple[re.Match, str]]:
    """Split on `## ` headings matching *pattern*, keeping each body."""
    lines = text.splitlines()
    starts: list[tuple[int, re.Match]] = []
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            starts.append((i, m))
    out: list[tuple[re.Match, str]] = []
    for n, (i, m) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out.append((m, "\n".join(lines[i + 1:end]).strip()))
    return out


def parse_decisions(path: Path) -> list[Decision]:
    """Structured decisions, newest first, with supersession links resolved."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[Decision] = []
    for m, body in _split_sections(text, _DECISION_H2):
        did, tail = m.group(1), m.group(2)
        status_m = _STATUS_TAG.search(tail)
        title = _STATUS_TAG.sub("", tail).strip().rstrip("—-–").strip()
        status = status_m.group(1).strip().lower() if status_m else "unknown"

        date = None
        superseded_by: list[str] = []
        for line in body.splitlines():
            f = _FIELD.match(line.strip())
            if not f:
                continue
            key, val = f.group(1).lower(), f.group(2).strip()
            if key == "date" and not date:
                date = val[:10]
            elif key == "status":
                # The heading tag and this field can disagree; the field is
                # where supersession is actually written, so it wins.
                low = val.lower()
                if low.startswith("superseded"):
                    status = "superseded"
                    superseded_by = _DREF.findall(val)
                elif low.startswith("active"):
                    status = status if status not in ("unknown",) else "active"
        if date is None:
            # Fall back to the id, which encodes the date it was assigned.
            m_date = re.search(r"(\d{4}-\d{2}-\d{2})", did)
            if m_date:
                date = m_date.group(1)
        out.append(Decision(id=did, title=title or did, status=status,
                            date=date, superseded_by=superseded_by, body=body))

    by_id = {d.id: d for d in out}
    for d in out:
        for target in d.superseded_by:
            if target in by_id:
                by_id[target].supersedes.append(d.id)
    out.sort(key=lambda d: (d.date or "", d.id), reverse=True)
    return out


def parse_log(path: Path, limit: int | None = None) -> list[LogEntry]:
    """Log entries, newest first."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[LogEntry] = []
    for m, body in _split_sections(text, _LOG_H2):
        date, tail = m.group(1), m.group(2)
        agent = None
        bits = [b.strip() for b in re.split(r"\s+[—–]\s+", tail)]
        if len(bits) >= 2:
            agent, heading = bits[0], " — ".join(bits[1:])
        else:
            heading = tail
        out.append(LogEntry(date=date, heading=heading, agent=agent, body=body,
                            decisions=sorted(set(_DREF.findall(body)))))
    out.sort(key=lambda e: e.date, reverse=True)
    return out[:limit] if limit else out


def overview_sections(path: Path) -> dict[str, str]:
    """`## Heading` -> body, for pulling out `Open questions` and friends."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {m.group(1).strip(): body for m, body in _split_sections(text, _H2_ANY)}


#: Headings whose contents are treated as open questions on the dashboard.
_QUESTION_HEADINGS = ("open questions", "open issues", "open items", "questions")
#: Lines that answer rather than ask -- "Resolved since Phase 1: ..." trailers
#: sit inside the same section and would otherwise read as live questions.
_RESOLVED_PREFIX = re.compile(r"^\s*(resolved|answered|closed)\b", re.IGNORECASE)


def open_questions(path: Path) -> list[str]:
    """Top-level bullets from a project's open-questions section."""
    sections = overview_sections(path)
    body = ""
    for heading, text in sections.items():
        if heading.strip().lower() in _QUESTION_HEADINGS:
            body = text
            break
    if not body:
        return []
    out: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if re.match(r"^[-*]\s+", line):
            if current:
                out.append(" ".join(current).strip())
            current = [re.sub(r"^[-*]\s+", "", line).strip()]
        elif current and line.startswith(("  ", "\t")):
            current.append(line.strip())
        elif not line.strip():
            continue
        else:
            if current:
                out.append(" ".join(current).strip())
                current = []
    if current:
        out.append(" ".join(current).strip())
    return [q for q in out if q and not _RESOLVED_PREFIX.match(q)]


def project_dirs(lexicon_root: Path) -> list[Path]:
    base = lexicon_root / "projects"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
