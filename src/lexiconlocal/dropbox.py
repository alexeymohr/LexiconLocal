"""Export drop-point classification and freshness.

The operator drops account exports into
``~/programming/LexiconLocal/downloaded_archives/``. The daily job files them
into the archive. The rules live here rather than in the shell script so they
can be tested: misfiling an export is a silent data-loss bug, and an
unrecognised drop must raise a notification rather than sit there for months.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

#: A Claude export batch directory: data-<uuid>-<ts>-<hash>-batch-NNNN
CLAUDE_BATCH_RE = re.compile(r"^data-.*-batch-\d+$")
#: ChatGPT exports arrive as a zip. Observed names vary, so match generously.
CHATGPT_ZIP_RE = re.compile(r"^(chatgpt|openai|conversations|export).*\.zip$", re.IGNORECASE)
#: ...but the 2026-08-18 export arrived already extracted, as a directory named
#: <sha256>-<YYYY-MM-DD-HH-MM-SS>-<hash> holding conversations-NNN.json shards.
#: It is identified by its contents, not its name, which is opaque.
CHATGPT_SHARD_GLOB = "conversations-*.json"

IGNORED_NAMES = {".DS_Store", ".localized"}

#: Warn when the newest export batch is older than this.
EXPORT_STALE_DAYS = 35


@dataclass
class DropEntry:
    path: Path
    kind: str          # claude-batch | chatgpt-zip | unrecognised
    destination: Path | None = None
    reason: str = ""


def classify_drop(entry: Path, lexicon_root: Path, today: date | None = None) -> DropEntry:
    """Decide what a dropped file or directory is and where it belongs."""
    name = entry.name
    today = today or date.today()

    if entry.is_dir() and CLAUDE_BATCH_RE.match(name):
        return DropEntry(entry, "claude-batch",
                         lexicon_root / "archive" / "claude" / name,
                         "Claude account export batch")

    if entry.is_file() and entry.suffix.lower() == ".zip" and CHATGPT_ZIP_RE.match(name):
        return DropEntry(entry, "chatgpt-zip",
                         lexicon_root / "archive" / "chatgpt" / today.isoformat(),
                         "ChatGPT account export archive")

    # An extracted ChatGPT export: an opaquely named directory whose only
    # recognisable feature is the conversations-NNN.json shards inside it. The
    # real 2026-08-18 export arrived exactly this way and the zip rule above
    # would have filed it as "unrecognised".
    if entry.is_dir() and any(entry.glob(CHATGPT_SHARD_GLOB)):
        return DropEntry(entry, "chatgpt-dir",
                         lexicon_root / "archive" / "chatgpt" / name,
                         "ChatGPT account export (already extracted)")

    # A bare directory holding conversations.json is a Claude export someone
    # renamed or extracted by hand -- accept it rather than lose it.
    if entry.is_dir() and (entry / "conversations.json").exists():
        return DropEntry(entry, "claude-batch",
                         lexicon_root / "archive" / "claude" / name,
                         "Claude export (non-standard directory name)")

    return DropEntry(entry, "unrecognised", None,
                     "not a recognised export; left in place for a human to look at")


def scan_drop_point(drop_dir: Path, lexicon_root: Path, today: date | None = None) -> list[DropEntry]:
    if not drop_dir.exists():
        return []
    out: list[DropEntry] = []
    for entry in sorted(drop_dir.iterdir()):
        if entry.name in IGNORED_NAMES or entry.name.startswith("."):
            continue
        out.append(classify_drop(entry, lexicon_root, today))
    return out


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------

def _batch_date(path: Path) -> date | None:
    """Best-effort date for an archived export batch."""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", path.name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Claude batch dirs embed a unix timestamp: data-<uuid>-<ts>-<hash>-batch-N
    for part in path.name.split("-"):
        if part.isdigit() and 10 <= len(part) <= 11:
            try:
                return datetime.fromtimestamp(int(part)).date()
            except (ValueError, OSError):
                continue
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


def newest_export_date(archive_subdir: Path) -> date | None:
    if not archive_subdir.exists():
        return None
    dates = []
    for child in archive_subdir.iterdir():
        if child.name in IGNORED_NAMES or child.name in {"README.md", ".gitkeep"}:
            continue
        d = _batch_date(child)
        if d:
            dates.append(d)
    return max(dates) if dates else None


def export_freshness(lexicon_root: Path, today: date | None = None,
                     stale_days: int = EXPORT_STALE_DAYS) -> list[tuple[str, int | None]]:
    """Return ``(source, age_days)`` for each export source.

    ``age_days`` is ``None`` when no export has ever arrived, which is a
    different problem from a stale one and must read differently.
    """
    today = today or date.today()
    out = []
    for source in ("chatgpt", "claude"):
        newest = newest_export_date(lexicon_root / "archive" / source)
        out.append((source, None if newest is None else (today - newest).days))
    return out


def stale_sources(lexicon_root: Path, today: date | None = None,
                  stale_days: int = EXPORT_STALE_DAYS) -> list[str]:
    msgs = []
    for source, age in export_freshness(lexicon_root, today, stale_days):
        if age is None:
            msgs.append(f"{source}: no export has ever arrived")
        elif age > stale_days:
            msgs.append(f"{source}: newest export is {age} days old (>{stale_days})")
    return msgs


def dir_content_hash(path: Path) -> str:
    """Stable hash of a directory's file contents, for change detection.

    Used to avoid re-snapshotting ``~/.codex/memories/`` every single night
    when nothing in it changed.
    """
    h = hashlib.sha256()
    if not path.exists():
        return ""
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        if f.name in IGNORED_NAMES or ".git" in f.parts:
            continue
        h.update(str(f.relative_to(path)).encode())
        try:
            h.update(f.read_bytes())
        except OSError:
            continue
    return h.hexdigest()
