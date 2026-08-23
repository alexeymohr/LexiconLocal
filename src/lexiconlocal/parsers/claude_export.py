"""Claude account-export parser (web + Cowork chats, memories, projects).

**Validated against a real export** (2026-08-18 dump, 730 conversations /
16,497 messages / 282 MB). The shape below is observed, not inferred.

Layout, one directory per export batch::

    archive/claude/data-<uuid>-<ts>-<hash>-batch-NNNN/
        conversations.json    flat JSON array of conversations
        memories.json         Claude's distilled memory of the user
        projects/<uuid>.json  project definition + attached docs
        users.json            PII -- NEVER INDEXED
        login_history.json    PII -- NEVER INDEXED

Exports arrive additively as further ``batch-NNNN`` directories. Documents are
keyed by conversation/project UUID rather than by file path, so a later export
that re-includes a conversation *updates* it instead of duplicating it, while
genuinely new conversations simply appear.

Three findings from the real dump drive the parsing:

1. **The flat ``text`` field is a superset of the ``content`` text blocks.**
   It is never shorter, and is longer in 5,820 of 16,497 messages because it
   also carries ``thinking`` content. Using it avoids both loss and
   double-counting (flat text 32.1 MB = text blocks 21.9 MB + thinking 9.5 MB).
2. **Tool traffic dominates the bytes, exactly as in Codex and Claude Code.**
   Of 282 MB, ``tool_result`` is 92.2 MB and ``tool_use`` 47.6 MB against
   32.1 MB of prose, so D-2026-08-18-07's three tiers apply unchanged.
3. **Conversations branch, but the threading data is uneven.** 187 of 730 have
   a genuine branch point -- overwhelmingly retries and edits -- and those are
   handled like the ChatGPT export: the canonical thread is the path back from
   the newest message, and off-path messages become a separate, downranked
   document. Crucially, branching is decided by real branch points and *not* by
   whether a parent walk reaches every message: conversations from 2023 carry
   ``parent_message_uuid: null`` throughout, and treating unreachable messages
   as abandoned buried 45 of one 46-message conversation. See ``split_canonical``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..chunk import KIND_PROSE, KIND_TOOL_EVENT, Chunk, chunk_markdown, chunk_plain, content_hash
from ..redact import redact
from .base import ParsedDoc

#: Files inside an export batch that must never be indexed under any
#: circumstance. users.json carries email address, full name and verified phone
#: number; login_history.json carries IP addresses and session times.
PII_FILENAMES = {"users.json", "login_history.json"}

#: Files this parser knows how to handle. Anything in a batch that is neither
#: known nor explicitly PII is reported as skipped rather than ignored --
#: a future export format change must be loud, not silent.
KNOWN_FILENAMES = {"conversations.json", "memories.json"}

#: Content block types. `text` and `thinking` are already folded into the flat
#: `text` field; `tool_use` becomes a searchable header; `tool_result` bodies
#: and `token_budget` bookkeeping are never indexed.
PROSE_BLOCK_TYPES = {"text", "thinking"}
SKIP_BLOCK_TYPES = {"tool_result", "token_budget"}

MAX_INPUT_CHARS = 200
SALIENT_TOOL_INPUTS = (
    "command", "file_path", "path", "query", "pattern", "url", "prompt", "description",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_date(value) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


def _message_prose(msg: dict) -> str:
    """Prose for one message.

    The flat ``text`` field is preferred because the real dump shows it to be a
    superset of the ``content`` text blocks -- it also contains ``thinking``.
    Falling back to blocks covers messages that carry only structured content.
    """
    flat = msg.get("text")
    if isinstance(flat, str) and flat.strip():
        return flat
    parts: list[str] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            if isinstance(block, str) and block.strip():
                parts.append(block)
            continue
        btype = block.get("type")
        if btype in SKIP_BLOCK_TYPES:
            continue
        if btype in PROSE_BLOCK_TYPES or btype is None:
            t = block.get("text") or block.get("thinking")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts)


def _message_tool_events(msg: dict) -> list[str]:
    out: list[str] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "tool"
        inp = block.get("input")
        bits: list[str] = []
        if isinstance(inp, dict):
            for key in SALIENT_TOOL_INPUTS:
                val = inp.get(key)
                if val is None:
                    continue
                s = str(val).replace("\n", " ")[:MAX_INPUT_CHARS]
                if s.strip():
                    bits.append(f"{key}={s}")
        out.append(f"[tool] {name} " + " ".join(bits) if bits else f"[tool] {name}")
    return out


def _canonical_uuids(messages: list[dict]) -> set[str]:
    """UUIDs on the path from the newest message back to the root."""
    if not messages:
        return set()
    by_uuid = {m.get("uuid"): m for m in messages if m.get("uuid")}
    newest = max(messages, key=lambda m: m.get("created_at") or "")
    path: set[str] = set()
    cur = newest
    while cur is not None:
        uid = cur.get("uuid")
        if not uid or uid in path:
            break
        path.add(uid)
        cur = by_uuid.get(cur.get("parent_message_uuid"))
    return path


def split_canonical(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split messages into the canonical thread and any abandoned branches.

    Branching is decided by **genuine branch points** -- a parent with more
    than one resolvable child -- not by whether a parent walk happens to reach
    every message. That distinction matters: conversations from 2023 in the
    real dump carry ``parent_message_uuid: null`` on every message, so a walk
    back from the newest message terminates immediately. Treating unreachable
    messages as abandoned would have buried 45 of one 46-message conversation
    under the downranked-branch boost.

    Returns ``(canonical, off_path)``, each ordered by ``created_at``.
    """
    ordered = sorted(messages, key=lambda m: m.get("created_at") or "")
    ids = {m.get("uuid") for m in messages if m.get("uuid")}
    child_count: dict[str, int] = {}
    for m in messages:
        parent = m.get("parent_message_uuid")
        if parent in ids:
            child_count[parent] = child_count.get(parent, 0) + 1
    has_branch_point = any(v > 1 for v in child_count.values())
    if not has_branch_point:
        return ordered, []

    canonical_ids = _canonical_uuids(messages)
    # A branch point exists but the walk still collapsed -- threading data is
    # too broken to trust. Keep everything rather than discard the thread.
    if len(canonical_ids) < 2:
        return ordered, []

    canonical = [m for m in ordered if m.get("uuid") in canonical_ids]
    off_path = [m for m in ordered if m.get("uuid") not in canonical_ids]
    return canonical, off_path


def _render(messages: list[dict]) -> tuple[str, list[str]]:
    prose_parts: list[str] = []
    events: list[str] = []
    for msg in messages:
        sender = msg.get("sender") or msg.get("role") or "unknown"
        text = _message_prose(msg)
        if text.strip():
            prose_parts.append(f"{sender}: {text}")
        events.extend(_message_tool_events(msg))
    return "\n\n".join(prose_parts), events


def _build(
    *,
    path: str,
    title: str,
    doc_date: str | None,
    prose: str,
    events: list[str],
    mtime: float | None,
    extra: dict,
    source_type: str = "transcript",
    markdown: bool = False,
) -> ParsedDoc | None:
    clean_prose, kinds1 = redact(prose)
    clean_events, kinds2 = redact("\n".join(events))
    chunks: list[Chunk] = (
        chunk_markdown(clean_prose, KIND_PROSE, 0) if markdown
        else chunk_plain(clean_prose, KIND_PROSE, 0)
    )
    chunks += chunk_plain(clean_events, KIND_TOOL_EVENT, len(chunks))
    if not chunks:
        return None
    return ParsedDoc(
        path=path,
        source_type=source_type,
        content_hash=content_hash("".join(c.content_hash for c in chunks)),
        chunks=chunks,
        doc_date=doc_date,
        mtime=mtime,
        size=len(prose) + len(clean_events),
        title=title,
        extra=extra,
        redactions=sorted(set(kinds1) | set(kinds2)),
    )


# ---------------------------------------------------------------------------
# conversations.json
# ---------------------------------------------------------------------------

def parse_conversations(path: Path, batch: str, root_key: str) -> Iterator[ParsedDoc]:
    """Yield one document per conversation, plus one per abandoned branch.

    ``json.load`` on the file handle rather than ``json.loads(read_text())``:
    the real dump is 282 MB, and the latter would hold the raw string and the
    parsed structure simultaneously.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            convs = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(convs, dict):
        convs = convs.get("conversations") or [convs]
    if not isinstance(convs, list):
        return

    mtime = path.stat().st_mtime
    for i, conv in enumerate(convs):
        if not isinstance(conv, dict):
            continue
        messages = conv.get("chat_messages") or conv.get("messages") or []
        if not isinstance(messages, list) or not messages:
            continue
        conv_id = conv.get("uuid") or conv.get("id") or f"index-{i}"
        title = conv.get("name") or conv.get("title") or f"Claude conversation {i}"
        doc_date = _parse_date(conv.get("created_at") or conv.get("create_time"))
        summary = conv.get("summary") or ""

        canonical, off_path = split_canonical(messages)

        prose, events = _render(canonical)
        if summary.strip():
            prose = f"summary: {summary}\n\n{prose}"
        doc = _build(
            path=f"{root_key}#conversation={conv_id}",
            title=str(title),
            doc_date=doc_date,
            prose=prose,
            events=events,
            mtime=mtime,
            extra={
                "tool": "claude-export",
                "conversation_id": conv_id,
                "branch": "canonical",
                "message_count": len(canonical),
                "off_path_messages": len(off_path),
                "export_file": str(path),
                "batch": batch,
                "updated_at": conv.get("updated_at"),
            },
        )
        if doc is not None:
            yield doc

        # Abandoned edits/retries: real content, but never above the real thread.
        if off_path:
            b_prose, b_events = _render(off_path)
            bdoc = _build(
                path=f"{root_key}#conversation={conv_id}&branch=abandoned",
                title=f"{title} (abandoned branch)",
                doc_date=doc_date,
                prose=b_prose,
                events=b_events,
                mtime=mtime,
                extra={
                    "tool": "claude-export",
                    "conversation_id": conv_id,
                    "branch": "abandoned",
                    "message_count": len(off_path),
                    "export_file": str(path),
                    "batch": batch,
                },
            )
            if bdoc is not None:
                yield bdoc


# ---------------------------------------------------------------------------
# memories.json
# ---------------------------------------------------------------------------

def parse_memories(path: Path, batch: str, root_key: str) -> Iterator[ParsedDoc]:
    """Claude's distilled memory of the user: prose plus attached memory files.

    Keyed **per batch**, unlike conversations. A conversation has a stable uuid
    and the newest export simply holds more of it, so updating in place loses
    nothing. A memory blob has no id -- it is a point-in-time snapshot -- so
    keying it without the batch makes each export silently overwrite the last,
    which is exactly the loss DESIGN.md 1.5 forbids. Successive snapshots are
    separate documents, the way archive/codex/memories-<date>/ already works.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return

    mtime = path.stat().st_mtime
    for entry in data:
        if not isinstance(entry, dict):
            continue
        blob = entry.get("conversations_memory")
        if isinstance(blob, str) and blob.strip():
            doc = _build(
                path=f"{root_key}#batch={batch}/memories/conversations_memory",
                title="Claude memory — conversations",
                doc_date=_parse_date(entry.get("updated_at")),
                prose=blob,
                events=[],
                mtime=mtime,
                extra={"tool": "claude-export", "kind": "conversations_memory",
                       "export_file": str(path), "batch": batch},
                source_type="claude-memory",
                markdown=True,
            )
            if doc is not None:
                yield doc

        for mf in entry.get("memory_files") or []:
            if not isinstance(mf, dict):
                continue
            content = mf.get("content") or mf.get("text") or ""
            if not isinstance(content, str) or not content.strip():
                continue
            name = mf.get("path") or mf.get("filename") or mf.get("name") or "memory"
            doc = _build(
                path=f"{root_key}#batch={batch}/memories/{name}",
                title=f"Claude memory — {name}",
                doc_date=_parse_date(mf.get("updated_at")),
                prose=content,
                events=[],
                mtime=mtime,
                extra={"tool": "claude-export", "kind": "memory_file",
                       "memory_path": name, "export_file": str(path), "batch": batch},
                source_type="claude-memory",
                markdown=True,
            )
            if doc is not None:
                yield doc


# ---------------------------------------------------------------------------
# projects/<uuid>.json
# ---------------------------------------------------------------------------

def parse_project(path: Path, batch: str, root_key: str) -> Iterator[ParsedDoc]:
    """A Claude project: its brief, plus each attached document.

    Also keyed per batch: a project's attached docs get edited over time, and a
    later export replacing the earlier snapshot would discard that history.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            proj = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(proj, dict):
        return

    mtime = path.stat().st_mtime
    proj_id = proj.get("uuid") or path.stem
    name = proj.get("name") or path.stem
    doc_date = _parse_date(proj.get("created_at"))

    brief_parts = [p for p in (proj.get("description"), proj.get("prompt_template")) if p]
    if brief_parts:
        doc = _build(
            path=f"{root_key}#batch={batch}/project={proj_id}",
            title=f"Claude project — {name}",
            doc_date=doc_date,
            prose=f"# {name}\n\n" + "\n\n".join(brief_parts),
            events=[],
            mtime=mtime,
            extra={"tool": "claude-export", "kind": "project", "project_uuid": proj_id,
                   "project_name": name, "is_private": proj.get("is_private"),
                   "export_file": str(path), "batch": batch},
            source_type="claude-project",
            markdown=True,
        )
        if doc is not None:
            yield doc

    for d in proj.get("docs") or []:
        if not isinstance(d, dict):
            continue
        content = d.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        filename = d.get("filename") or d.get("uuid") or "doc"
        doc = _build(
            path=f"{root_key}#batch={batch}/project={proj_id}/doc={filename}",
            title=f"{name} — {filename}",
            doc_date=_parse_date(d.get("created_at")) or doc_date,
            prose=content,
            events=[],
            mtime=mtime,
            extra={"tool": "claude-export", "kind": "project_doc", "project_uuid": proj_id,
                   "project_name": name, "filename": filename,
                   "export_file": str(path), "batch": batch},
            source_type="claude-project",
            markdown=True,
        )
        if doc is not None:
            yield doc


# ---------------------------------------------------------------------------
# batch discovery
# ---------------------------------------------------------------------------

def iter_batches(claude_root: Path) -> list[Path]:
    """Export batch directories, oldest first.

    Sorted so that when several batches contain the same conversation, the
    later batch is processed last and its version wins the upsert.
    """
    if not claude_root.exists():
        return []
    batches: set[Path] = set()
    for p in claude_root.iterdir():
        if not p.is_dir():
            continue
        # The real export names its directories data-<uuid>-...-batch-NNNN, but
        # a dump extracted or renamed by hand should still be picked up.
        if p.name.startswith("data-") or (p / "conversations.json").exists() \
                or (p / "memories.json").exists() or (p / "projects").is_dir():
            batches.add(p)
    # Tolerate a dump extracted directly into archive/claude/.
    if (claude_root / "conversations.json").exists():
        batches.add(claude_root)
    return sorted(batches)


def iter_exports(claude_root: Path) -> Iterator[ParsedDoc]:
    for batch_dir in iter_batches(claude_root):
        batch = batch_dir.name
        root_key = str(claude_root)
        conv = batch_dir / "conversations.json"
        if conv.exists():
            yield from parse_conversations(conv, batch, root_key)
        mem = batch_dir / "memories.json"
        if mem.exists():
            yield from parse_memories(mem, batch, root_key)
        proj_dir = batch_dir / "projects"
        if proj_dir.is_dir():
            for p in sorted(proj_dir.glob("*.json")):
                yield from parse_project(p, batch, root_key)


def survey_batch(batch_dir: Path) -> dict:
    """Classify every file in a batch: parsed, PII-excluded, or unrecognised.

    Unrecognised files are surfaced by ``lexicon report`` rather than ignored,
    so a change to the export format is loud instead of a silent gap.
    """
    parsed: list[str] = []
    excluded: list[str] = []
    unknown: list[str] = []
    for p in sorted(batch_dir.rglob("*")):
        if not p.is_file() or p.name == ".DS_Store":
            continue
        rel = str(p.relative_to(batch_dir))
        if p.name in PII_FILENAMES:
            excluded.append(rel)
        elif p.name in KNOWN_FILENAMES or p.parent.name == "projects":
            parsed.append(rel)
        else:
            unknown.append(rel)
    return {"batch": batch_dir.name, "parsed": parsed, "pii_excluded": excluded, "unknown": unknown}
