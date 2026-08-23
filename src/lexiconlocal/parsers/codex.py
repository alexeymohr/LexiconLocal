"""Codex rollout JSONL parser.

Streaming and line-oriented by necessity: Phase 1 found single rollout files of
893 MB with individual lines approaching 1 MB, and 27 files over 100 MB. A
whole-file read would exhaust memory for no benefit, since 92% of those bytes
are tool output we discard.

Envelope is ``{timestamp, type, payload}``; the real discriminator is
``payload.type``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..chunk import KIND_PROSE, KIND_TOOL_EVENT, Chunk, chunk_plain, content_hash
from ..redact import redact
from .base import ParsedDoc

#: Embedded and FTS-indexed (D-2026-08-18-07 tier 1).
PROSE_TYPES = {"user_message", "agent_message", "message", "reasoning"}
#: FTS only, as compact headers (tier 2).
EVENT_TYPES = {
    "function_call", "custom_tool_call", "exec_command_end",
    "patch_apply_end", "web_search_call",
}
#: Never indexed (tier 3) -- these are the bytes that make the corpus huge.
SKIP_TYPES = {
    "function_call_output", "custom_tool_call_output", "mcp_tool_call_output",
    "mcp_tool_call_end", "token_count", "context_compacted", "thread_goal_updated",
}

#: Cheap substring pre-filter. Quoted markers are exact: `"message"` does not
#: match `"user_message"`, and `"function_call"` does not match
#: `"function_call_output"`, so a 1 MB tool-output line is rejected without
#: ever being handed to json.loads.
_WANTED_MARKERS = tuple(
    f'"{t}"' for t in (PROSE_TYPES | EVENT_TYPES | {"session_meta", "turn_context"})
)

MAX_EVENT_ARG_CHARS = 200
#: Guard against a single pathological payload dominating a document.
MAX_PROSE_CHARS_PER_RECORD = 200_000


def _content_text(payload: dict) -> str:
    """Extract text from a Codex payload, which nests several shapes."""
    out: list[str] = []
    for key in ("text", "message", "content"):
        val = payload.get(key)
        if isinstance(val, str):
            if val.strip():
                out.append(val)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, str):
                    out.append(block)
                elif isinstance(block, dict):
                    for k in ("text", "content", "summary"):
                        v = block.get(k)
                        if isinstance(v, str) and v.strip():
                            out.append(v)
    # `reasoning` payloads often carry a summary list.
    summary = payload.get("summary")
    if isinstance(summary, list):
        for block in summary:
            if isinstance(block, dict):
                v = block.get("text")
                if isinstance(v, str) and v.strip():
                    out.append(v)
            elif isinstance(block, str) and block.strip():
                out.append(block)
    return "\n".join(out)[:MAX_PROSE_CHARS_PER_RECORD]


def _event_line(ptype: str, payload: dict) -> str | None:
    if ptype in {"function_call", "custom_tool_call"}:
        name = payload.get("name") or "tool"
        args = payload.get("arguments") or payload.get("input") or ""
        if isinstance(args, (dict, list)):
            args = json.dumps(args, default=str)
        args = str(args).replace("\n", " ")[:MAX_EVENT_ARG_CHARS]
        return f"[tool] {name} {args}".strip()
    if ptype == "exec_command_end":
        cmd = payload.get("command") or payload.get("formatted_command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd).replace("\n", " ")[:MAX_EVENT_ARG_CHARS]
        code = payload.get("exit_code")
        return f"[exec] {cmd}" + (f" (exit {code})" if code is not None else "")
    if ptype == "patch_apply_end":
        return f"[patch] {str(payload.get('stdout') or '')[:MAX_EVENT_ARG_CHARS]}".strip()
    if ptype == "web_search_call":
        q = payload.get("query") or ((payload.get("action") or {}) if isinstance(payload.get("action"), dict) else {}).get("query")
        return f"[web_search] {str(q or '')[:MAX_EVENT_ARG_CHARS]}".strip()
    return None


def parse_rollout(path: Path) -> ParsedDoc | None:
    prose_parts: list[str] = []
    event_lines: list[str] = []
    session_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    cli_version: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    lines_total = 0
    lines_parsed = 0

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            lines_total += 1
            if not line.strip():
                continue
            # Reject the bulk of the corpus without parsing it.
            if not any(m in line for m in _WANTED_MARKERS):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            lines_parsed += 1

            ts = obj.get("timestamp")
            if isinstance(ts, str) and len(ts) >= 10:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            etype = obj.get("type")

            if etype == "session_meta":
                # Deliberately ignore payload.base_instructions: tens of KB of
                # identical system prompt in every single file.
                session_id = session_id or payload.get("id")
                cwd = cwd or payload.get("cwd")
                cli_version = cli_version or payload.get("cli_version")
                continue
            if etype == "turn_context":
                cwd = cwd or payload.get("cwd")
                model = model or payload.get("model")
                continue

            ptype = payload.get("type")
            if ptype in SKIP_TYPES:
                continue
            if ptype in PROSE_TYPES:
                text = _content_text(payload)
                if text.strip():
                    role = payload.get("role") or (
                        "user" if ptype == "user_message"
                        else "assistant" if ptype == "agent_message"
                        else ptype
                    )
                    prose_parts.append(f"{role}: {text}")
            elif ptype in EVENT_TYPES:
                ev = _event_line(ptype, payload)
                if ev:
                    event_lines.append(ev)

    if not prose_parts and not event_lines:
        return None

    prose_text, kinds1 = redact("\n\n".join(prose_parts))
    event_text, kinds2 = redact("\n".join(event_lines))
    chunks: list[Chunk] = chunk_plain(prose_text, KIND_PROSE, 0)
    chunks += chunk_plain(event_text, KIND_TOOL_EVENT, len(chunks))
    if not chunks:
        return None

    stat = path.stat()
    # rollout-2026-05-12T19-28-22-<uuid>.jsonl
    date_from_name = None
    if path.name.startswith("rollout-") and len(path.name) > 18:
        date_from_name = path.name[8:18]

    return ParsedDoc(
        path=str(path),
        source_type="transcript",
        content_hash=content_hash("".join(c.content_hash for c in chunks)),
        chunks=chunks,
        doc_date=(first_ts or "")[:10] or date_from_name,
        mtime=stat.st_mtime,
        size=stat.st_size,
        title=None,  # filled from session_index.jsonl thread names
        extra={
            "tool": "codex",
            "session_id": session_id,
            "cwd": cwd,
            "model": model,
            "cli_version": cli_version,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "lines_total": lines_total,
            "lines_parsed": lines_parsed,
            "archived": "archived_sessions" in path.parts,
        },
        redactions=sorted(set(kinds1) | set(kinds2)),
    )


def load_thread_titles(session_index: Path) -> dict[str, str]:
    """Map session id -> human thread name from ``session_index.jsonl``."""
    titles: dict[str, str] = {}
    if not session_index.exists():
        return titles
    for line in session_index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid, name = obj.get("id"), obj.get("thread_name")
        if sid and name:
            titles[str(sid)] = str(name)
    return titles


def iter_rollouts(codex_root: Path) -> Iterator[Path]:
    for sub in ("sessions", "archived_sessions"):
        d = codex_root / sub
        if not d.exists():
            continue
        yield from sorted(p for p in d.rglob("rollout-*.jsonl") if p.is_file())
