"""Claude Code JSONL transcript parser.

Two Phase 1 findings drive this file:

1. **Tool results live inside ``type: "user"`` records.** A parser that treats
   every ``user`` record as a human prompt silently ingests shell output. The
   giveaway is a top-level ``toolUseResult`` key, or content blocks that are
   entirely ``tool_result``. Both are excluded here and tested explicitly.
2. **229 files hold only 57 sessions.** Documents are grouped by ``sessionId``,
   not by file, or session counts inflate roughly fourfold.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from ..chunk import KIND_PROSE, KIND_TOOL_EVENT, Chunk, chunk_plain, content_hash
from ..redact import redact
from .base import ParsedDoc

#: Tool inputs worth keeping as a searchable header (D-2026-08-18-07 tier 2).
SALIENT_TOOL_INPUTS = (
    "command", "file_path", "path", "pattern", "query", "url", "notebook_path",
    "old_string", "prompt", "description", "subagent_type",
)
MAX_INPUT_CHARS = 200


def _text_blocks(content) -> tuple[list[str], list[dict]]:
    """Split a message ``content`` into (prose texts, tool_use blocks)."""
    if isinstance(content, str):
        return ([content] if content.strip() else []), []
    if not isinstance(content, list):
        return [], []
    texts: list[str] = []
    tool_uses: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text") or ""
            if t.strip():
                texts.append(t)
        elif btype == "thinking":
            t = block.get("thinking") or ""
            if t.strip():
                texts.append(t)
        elif btype == "tool_use":
            tool_uses.append(block)
        # tool_result blocks are tool OUTPUT -- never indexed.
    return texts, tool_uses


def _tool_event_line(block: dict) -> str | None:
    name = block.get("name") or "tool"
    inp = block.get("input")
    if not isinstance(inp, dict):
        return f"[tool] {name}"
    parts = []
    for key in SALIENT_TOOL_INPUTS:
        if key in inp and inp[key] is not None:
            val = str(inp[key]).replace("\n", " ")[:MAX_INPUT_CHARS]
            if val.strip():
                parts.append(f"{key}={val}")
    return f"[tool] {name} " + " ".join(parts) if parts else f"[tool] {name}"


def session_id_of_file(path: Path) -> str | None:
    """Cheaply determine which session a transcript file belongs to."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                if '"sessionId"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("sessionId")
                if sid:
                    return str(sid)
    except OSError:
        return None
    return None


def group_by_session(files: list[Path]) -> dict[str, list[Path]]:
    """Group transcript files by ``sessionId``, falling back to filename stem."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        sid = session_id_of_file(f) or f.stem
        groups[sid].append(f)
    for sid in groups:
        groups[sid].sort()
    return dict(groups)


def parse_session(session_id: str, files: list[Path], archive_root: Path) -> ParsedDoc | None:
    prose_parts: list[str] = []
    event_lines: list[str] = []
    cwd: str | None = None
    git_branch: str | None = None
    slug: str | None = None
    version: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    sidechain = False
    skipped_meta = 0

    for path in files:
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                ts = obj.get("timestamp")
                if isinstance(ts, str) and len(ts) >= 10:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                cwd = cwd or obj.get("cwd")
                git_branch = git_branch or obj.get("gitBranch")
                slug = slug or obj.get("slug") or obj.get("customTitle")
                version = version or obj.get("version")
                if obj.get("isSidechain"):
                    sidechain = True

                # Meta records are bookkeeping, not conversation.
                if obj.get("isMeta"):
                    skipped_meta += 1
                    continue

                rtype = obj.get("type")
                if rtype not in {"user", "assistant"}:
                    continue

                # THE TRAP: a `user` record carrying toolUseResult is tool
                # output wearing a user record's clothes. Never prose.
                if "toolUseResult" in obj:
                    continue

                message = obj.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if content is None:
                    continue
                texts, tool_uses = _text_blocks(content)
                role = (message or {}).get("role") or rtype
                for t in texts:
                    prose_parts.append(f"{role}: {t}")
                for tu in tool_uses:
                    line_out = _tool_event_line(tu)
                    if line_out:
                        event_lines.append(line_out)

    if not prose_parts and not event_lines:
        return None

    prose_text, kinds1 = redact("\n\n".join(prose_parts))
    event_text, kinds2 = redact("\n".join(event_lines))

    chunks: list[Chunk] = chunk_plain(prose_text, KIND_PROSE, 0)
    chunks += chunk_plain(event_text, KIND_TOOL_EVENT, len(chunks))
    if not chunks:
        return None

    rel_dir = files[0].parent
    try:
        rel = rel_dir.relative_to(archive_root)
        dir_label = str(rel)
    except ValueError:
        dir_label = rel_dir.name

    doc_path = f"{rel_dir}#session={session_id}"
    stat_mtime = max((f.stat().st_mtime for f in files), default=None)
    total_size = sum(f.stat().st_size for f in files)

    return ParsedDoc(
        path=doc_path,
        source_type="transcript",
        content_hash=content_hash("".join(c.content_hash for c in chunks)),
        chunks=chunks,
        project=None,  # filled by the indexer from cwd
        root=None,
        doc_date=(first_ts or "")[:10] or None,
        mtime=stat_mtime,
        size=total_size,
        title=slug or f"Claude Code session {session_id[:8]}",
        extra={
            "tool": "claude-code",
            "session_id": session_id,
            "files": [str(f) for f in files],
            "file_count": len(files),
            "cwd": cwd,
            "git_branch": git_branch,
            "version": version,
            "sidechain": sidechain,
            "archive_dir": dir_label,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "skipped_meta_records": skipped_meta,
        },
        redactions=sorted(set(kinds1) | set(kinds2)),
    )


def iter_sessions(claude_code_root: Path, archive_root: Path) -> Iterator[ParsedDoc]:
    files = sorted(p for p in claude_code_root.rglob("*.jsonl") if p.is_file())
    for sid, group in group_by_session(files).items():
        doc = parse_session(sid, group, archive_root)
        if doc is not None:
            yield doc
