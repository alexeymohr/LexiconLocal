"""Chunking: ~800 tokens with ~15% overlap, Markdown-heading aware.

Tokens are approximated at 4 characters each. A real tokenizer would be more
accurate but would add a dependency and a model download for a value that only
needs to be roughly right -- the embedding model truncates anyway, and the
chunk boundary matters more for readability than for exactness.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
TARGET_TOKENS = 800
OVERLAP_RATIO = 0.15

TARGET_CHARS = TARGET_TOKENS * CHARS_PER_TOKEN          # 3200
OVERLAP_CHARS = int(TARGET_CHARS * OVERLAP_RATIO)       # 480

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

#: Chunk kinds. ``prose`` is embedded and FTS-indexed; ``tool_event`` is FTS
#: only (D-2026-08-18-07).
KIND_PROSE = "prose"
KIND_TOOL_EVENT = "tool_event"


@dataclass
class Chunk:
    ord: int
    kind: str
    text: str
    content_hash: str


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _split_long(block: str) -> list[str]:
    """Hard-split an oversized block on paragraph, then line, then character."""
    if len(block) <= TARGET_CHARS:
        return [block]
    out: list[str] = []
    buf = ""
    for para in block.split("\n\n"):
        piece = para if not buf else buf + "\n\n" + para
        if len(piece) <= TARGET_CHARS:
            buf = piece
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(para) <= TARGET_CHARS:
            buf = para
            continue
        # Still too big: split on lines, then raw characters.
        line_buf = ""
        for line in para.split("\n"):
            cand = line if not line_buf else line_buf + "\n" + line
            if len(cand) <= TARGET_CHARS:
                line_buf = cand
                continue
            if line_buf:
                out.append(line_buf)
                line_buf = ""
            while len(line) > TARGET_CHARS:
                out.append(line[:TARGET_CHARS])
                line = line[TARGET_CHARS:]
            line_buf = line
        if line_buf:
            buf = line_buf
    if buf:
        out.append(buf)
    return out


#: Below this a chunk carries too little context to retrieve usefully, so it is
#: folded into a neighbour even at the cost of slightly overshooting the target.
MIN_CHUNK_CHARS = 400
MERGE_OVERSHOOT = 1.25


def _merge_small(bodies: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Fold undersized bodies into a neighbour.

    A lone ``## Section A`` heading, or a short preamble sitting in front of an
    oversized section, would otherwise become a chunk with no retrievable
    content of its own.
    """
    if len(bodies) <= 1:
        return bodies
    limit = int(TARGET_CHARS * MERGE_OVERSHOOT)
    out: list[tuple[str, str]] = []
    for path, body in bodies:
        if out and (len(body) < MIN_CHUNK_CHARS or len(out[-1][1]) < MIN_CHUNK_CHARS):
            prev_path, prev_body = out[-1]
            if len(prev_body) + len(body) + 1 <= limit:
                out[-1] = (prev_path or path, prev_body + "\n" + body)
                continue
        out.append((path, body))
    return out


def chunk_markdown(text: str, kind: str = KIND_PROSE, start_ord: int = 0) -> list[Chunk]:
    """Chunk Markdown, preferring to break at headings.

    Each chunk carries the heading path it sits under, so a fragment retrieved
    out of context still says where it came from.
    """
    if not text.strip():
        return []

    # Split into (heading_path, body) sections.
    sections: list[tuple[str, list[str]]] = []
    stack: list[str] = []
    current: list[str] = []
    current_path = ""
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if current:
                sections.append((current_path, current))
                current = []
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            current_path = " > ".join(s for s in stack if s)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_path, current))

    # Pack sections greedily up to the target size. A section larger than the
    # target is hard-split; small sections merge with their neighbours so a
    # short heading never becomes a chunk of its own.
    bodies: list[tuple[str, str]] = []  # (heading_path, text)
    buf = ""
    buf_path = ""
    for path, lines in sections:
        block = "\n".join(lines)
        if not block.strip():
            continue
        if len(block) > TARGET_CHARS:
            if buf.strip():
                bodies.append((buf_path, buf))
                buf = ""
            for piece in _split_long(block):
                if piece.strip():
                    bodies.append((path, piece))
            continue
        if not buf:
            buf, buf_path = block, path
        elif len(buf) + len(block) + 1 <= TARGET_CHARS:
            buf = buf + "\n" + block
        else:
            bodies.append((buf_path, buf))
            buf, buf_path = block, path
    if buf.strip():
        bodies.append((buf_path, buf))

    bodies = _merge_small(bodies)

    # Emit with backward overlap so a fact spanning a boundary stays findable.
    chunks: list[Chunk] = []
    ordinal = start_ord
    for i, (path, body) in enumerate(bodies):
        body = body.strip()
        if not body:
            continue
        prefix = ""
        if i > 0:
            prev = bodies[i - 1][1].strip()
            if prev:
                prefix = prev[-OVERLAP_CHARS:].lstrip() + "\n\n"
        header = f"[{path}]\n" if path and not body.lstrip().startswith("#") else ""
        payload = prefix + header + body
        chunks.append(Chunk(ordinal, kind, payload, content_hash(payload)))
        ordinal += 1
    return chunks


def chunk_plain(text: str, kind: str, start_ord: int = 0) -> list[Chunk]:
    """Chunk non-Markdown text (transcript prose, tool-event lines)."""
    if not text.strip():
        return []
    chunks: list[Chunk] = []
    ordinal = start_ord
    remaining = text
    while remaining:
        if len(remaining) <= TARGET_CHARS:
            piece, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 0, TARGET_CHARS)
            if cut < TARGET_CHARS // 2:
                cut = TARGET_CHARS
            piece, remaining = remaining[:cut], remaining[max(0, cut - OVERLAP_CHARS):]
        piece = piece.strip()
        if piece:
            chunks.append(Chunk(ordinal, kind, piece, content_hash(piece)))
            ordinal += 1
        if not remaining.strip():
            break
    return chunks
