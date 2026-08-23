"""ChatGPT account-export parser.

**Validated against a real export** (2026-08-18 dump: 2,089 conversations /
46,131 messages / 1.5 GB on disk, spanning 2023-01-02 to 2026-08-18). Every
shape below is observed in that dump, not inferred from documentation.

Layout, one directory per export batch::

    archive/chatgpt/<hash>-<YYYY-MM-DD-HH-MM-SS>-<hash>/
        conversations-000.json .. conversations-020.json   the conversations
        chat.html                155 MB rendering of the same conversations
        conversation_asset_file_names.json   asset id -> original filename
        file-*.dat / file_*.dat  1,769 binary attachments
        user.json                PII -- NEVER INDEXED
        user_settings.json, message_feedback.json, shared_conversations.json,
        library_files.json, ads.json, export_manifest.json   bookkeeping

Five findings from the real dump drive the parsing:

1. **There is no ``conversations.json``.** The export shards conversations
   across ``conversations-NNN.json``. A parser globbing the singular name --
   as this one did before the dump landed -- silently indexes nothing at all.
2. **Nodes carry no ``children`` key.** All 48,220 nodes have exactly
   ``{id, message, parent}``. Leaf detection via ``node["children"]`` therefore
   marks *every* node a leaf, which would have emitted 3,782 near-duplicate
   "abandoned branch" documents. Children are reconstructed from parent edges.
3. **Branching is decided by genuine branch points**, the same rule the Claude
   export needed: a parent with more than one child. 419 of 2,089 conversations
   have one. ``current_node`` is always present and always resolvable here, but
   the branch-point test is what keeps a degenerate walk from burying a thread.
4. **Reasoning is a separate content type carrying real prose.** ``thoughts``
   holds 20,912 blocks / 7.3 MB under its own ``thoughts`` key -- not under
   ``parts`` -- so a parts-only reader drops it. It is model scratchpad, so it
   is indexed at the ``tool_event`` tier: FTS-searchable, never embedded, never
   outranking the answer it reasoned toward (D-2026-08-19-01).
   ``reasoning_recap`` is the sibling banner ("Thought for 1m 8s") and is 82 KB
   of pure noise; it is dropped.
5. **Attachments survive only as pointers.** 683 ``image_asset_pointer`` parts
   reference ``file-service://file-...``; 680 of them resolve through
   ``conversation_asset_file_names.json`` to a human filename. Rendering that
   filename inline makes "the screenshot I sent about X" findable; the ``.dat``
   payloads themselves are binary and are never indexed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..chunk import KIND_PROSE, KIND_TOOL_EVENT, Chunk, chunk_plain, content_hash
from ..redact import redact
from .base import ParsedDoc

#: Never indexed under any circumstance: carries email address, phone number
#: and birth year.
PII_FILENAMES = {"user.json"}

#: Files this parser reads.
CONVERSATION_GLOB = "conversations*.json"
ASSET_NAME_FILE = "conversation_asset_file_names.json"

#: Known files deliberately left out, with the reason the report prints. Being
#: explicit here is what keeps `unknown` meaningful: a future export format
#: change shows up as an unrecognised file rather than a silent gap.
SKIP_REASONS: dict[str, str] = {
    "chat.html": "155 MB rendering of the same conversations -- indexing it would double every thread",
    "ads.json": "empty bookkeeping",
    "export_manifest.json": "file listing, no content",
    ASSET_NAME_FILE: "asset id -> filename map, consumed inline rather than indexed",
    "library_files.json": "attachment metadata, no conversation content",
    "user_settings.json": "account settings, no content",
    "message_feedback.json": "thumbs up/down bookkeeping, no content",
    "shared_conversations.json": "share links for conversations already indexed",
}

#: Content types this parser knows how to read. `text` and `multimodal_text`
#: hold their text in `parts`; `code` and `execution_output` hold it in a flat
#: `text`; `thoughts` holds it under its own key; `reasoning_recap` holds none.
#: Anything outside this set is recorded in the transcript as an unhandled type
#: rather than dropped silently -- a new content type is a format change, and
#: format changes have to be loud.
KNOWN_CONTENT_TYPES = {
    None, "text", "multimodal_text", "code", "execution_output",
    "thoughts", "reasoning_recap",
}

#: Banner only ("Thought for 1m 8s"): no information, 82 KB of it.
DROP_CONTENT_TYPES = {"reasoning_recap"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_date(value) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value).date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


def _asset_label(part: dict, assets: dict[str, str]) -> str | None:
    """Render a non-text part as a searchable one-liner.

    The pointer is ``file-service://file-XYZ``; the asset map is keyed by
    ``file-XYZ.dat``. Unresolvable pointers still get a marker so the fact that
    something was attached is not lost.
    """
    ctype = part.get("content_type") or "asset"
    pointer = part.get("asset_pointer")
    if not isinstance(pointer, str):
        return None
    file_id = pointer.rsplit("/", 1)[-1]
    name = assets.get(f"{file_id}.dat") or assets.get(file_id)
    kind = {
        "image_asset_pointer": "image",
        "audio_asset_pointer": "audio",
        "real_time_user_audio_video_asset_pointer": "video",
    }.get(ctype, ctype)
    return f"[{kind}: {name}]" if name else f"[{kind}]"


def _message_content(msg: dict, assets: dict[str, str]) -> tuple[str, str]:
    """Return ``(prose, reasoning)`` for one message.

    Reasoning is kept apart from prose so it can be chunked at the FTS-only
    tier instead of consuming embedding budget.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, dict):
        return "", ""

    ctype = content.get("content_type")
    if ctype in DROP_CONTENT_TYPES:
        return "", ""

    if ctype == "thoughts":
        bits: list[str] = []
        for t in content.get("thoughts") or []:
            if not isinstance(t, dict):
                continue
            summary = t.get("summary")
            body = t.get("content")
            if isinstance(summary, str) and summary.strip():
                bits.append(f"[thinking] {summary.strip()}")
            if isinstance(body, str) and body.strip():
                bits.append(body.strip())
        return "", "\n".join(bits)

    parts: list[str] = []
    for p in content.get("parts") or []:
        if isinstance(p, str):
            if p.strip():
                parts.append(p)
        elif isinstance(p, dict):
            text = p.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
            else:
                label = _asset_label(p, assets)
                if label:
                    parts.append(label)
    # `code` / `execution_output` carry a flat `text` instead of `parts`.
    flat = content.get("text")
    if isinstance(flat, str) and flat.strip():
        parts.append(flat)
    if not parts and ctype not in KNOWN_CONTENT_TYPES:
        return "", f"[unhandled content_type: {ctype}]"
    return "\n".join(parts), ""


def _children_of(mapping: dict) -> dict[str, list[str]]:
    """Reconstruct the child index from parent edges.

    The export omits ``children`` entirely, so this is the only way to know
    which nodes are leaves or where a thread branched.
    """
    kids: dict[str, list[str]] = {}
    for nid, node in mapping.items():
        if not isinstance(node, dict):
            continue
        parent = node.get("parent")
        if parent is not None:
            kids.setdefault(parent, []).append(nid)
    return kids


def _canonical_ids(mapping: dict, current_node: str | None) -> list[str]:
    """Node ids from the root down to ``current_node``."""
    path: list[str] = []
    seen: set[str] = set()
    nid = current_node
    while nid and nid in mapping and nid not in seen:
        seen.add(nid)
        path.append(nid)
        node = mapping[nid]
        nid = node.get("parent") if isinstance(node, dict) else None
    path.reverse()
    return path


def split_canonical(mapping: dict, current_node: str | None) -> tuple[list[str], list[str]]:
    """Split a conversation's nodes into the canonical thread and the rest.

    Mirrors ``claude_export.split_canonical``: a conversation is only treated
    as branched when a parent genuinely has more than one child. Without that
    guard a conversation whose ``current_node`` fails to resolve would have its
    entire body demoted to the abandoned-branch tier.
    """
    kids = _children_of(mapping)
    canonical = _canonical_ids(mapping, current_node)
    has_branch_point = any(len(v) > 1 for v in kids.values())
    if not has_branch_point or len(canonical) < 2:
        return list(mapping.keys()), []
    canonical_set = set(canonical)
    off_path = [nid for nid in mapping if nid not in canonical_set]
    return canonical, off_path


def _order_key(mapping: dict, nid: str):
    node = mapping.get(nid) or {}
    msg = node.get("message")
    ts = msg.get("create_time") if isinstance(msg, dict) else None
    return (ts if isinstance(ts, (int, float)) else 0.0, nid)


def _render(mapping: dict, node_ids: list[str], assets: dict[str, str]) -> tuple[str, str]:
    prose_out: list[str] = []
    reasoning_out: list[str] = []
    for nid in node_ids:
        node = mapping.get(nid)
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue  # the synthetic root node carries no message
        author = msg.get("author") or {}
        role = author.get("role") if isinstance(author, dict) else None
        if role in (None, "system"):
            continue
        prose, reasoning = _message_content(msg, assets)
        if prose.strip():
            prose_out.append(f"{role}: {prose}")
        if reasoning.strip():
            reasoning_out.append(reasoning)
    return "\n\n".join(prose_out), "\n\n".join(reasoning_out)


def _build(
    *,
    path: str,
    title: str,
    doc_date: str | None,
    prose: str,
    reasoning: str,
    mtime: float | None,
    extra: dict,
) -> ParsedDoc | None:
    clean_prose, kinds1 = redact(prose)
    clean_reasoning, kinds2 = redact(reasoning)
    chunks: list[Chunk] = chunk_plain(clean_prose, KIND_PROSE, 0)
    chunks += chunk_plain(clean_reasoning, KIND_TOOL_EVENT, len(chunks))
    if not chunks:
        return None
    return ParsedDoc(
        path=path,
        source_type="transcript",
        content_hash=content_hash("".join(c.content_hash for c in chunks)),
        chunks=chunks,
        doc_date=doc_date,
        mtime=mtime,
        size=len(clean_prose) + len(clean_reasoning),
        title=title,
        extra=extra,
        redactions=sorted(set(kinds1) | set(kinds2)),
    )


# ---------------------------------------------------------------------------
# conversations-NNN.json
# ---------------------------------------------------------------------------

def parse_conversations(
    path: Path, batch: str, root_key: str, assets: dict[str, str] | None = None
) -> Iterator[ParsedDoc]:
    """Yield one document per conversation, plus one per abandoned branch.

    Documents are keyed by conversation id rather than by file path: the export
    shards conversations across ``conversations-NNN.json`` and a later export
    will not shard them the same way, so a path-keyed document would duplicate
    the whole corpus on every new batch.

    ``json.load`` on the file handle rather than ``json.loads(read_text())`` --
    the largest shard is 25 MB and the latter would hold the raw string and the
    parsed structure at once.
    """
    assets = assets or {}
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
        mapping = conv.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            continue
        conv_id = conv.get("conversation_id") or conv.get("id") or f"{path.name}-{i}"
        title = conv.get("title") or f"ChatGPT conversation {conv_id}"
        doc_date = _parse_date(conv.get("create_time"))

        canonical, off_path = split_canonical(mapping, conv.get("current_node"))
        canonical.sort(key=lambda n: _order_key(mapping, n))
        off_path.sort(key=lambda n: _order_key(mapping, n))

        prose, reasoning = _render(mapping, canonical, assets)
        doc = _build(
            path=f"{root_key}#conversation={conv_id}",
            title=str(title),
            doc_date=doc_date,
            prose=prose,
            reasoning=reasoning,
            mtime=mtime,
            extra={
                "tool": "chatgpt",
                "conversation_id": conv_id,
                "branch": "canonical",
                "message_count": len(canonical),
                "off_path_messages": len(off_path),
                "model": conv.get("default_model_slug"),
                "is_archived": conv.get("is_archived"),
                "is_starred": conv.get("is_starred"),
                "export_file": str(path),
                "batch": batch,
                "updated_at": _parse_date(conv.get("update_time")),
            },
        )
        if doc is not None:
            yield doc

        if off_path:
            b_prose, b_reasoning = _render(mapping, off_path, assets)
            bdoc = _build(
                path=f"{root_key}#conversation={conv_id}&branch=abandoned",
                title=f"{title} (abandoned branch)",
                doc_date=doc_date,
                prose=b_prose,
                reasoning=b_reasoning,
                mtime=mtime,
                extra={
                    "tool": "chatgpt",
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
# batch discovery
# ---------------------------------------------------------------------------

def iter_batches(chatgpt_root: Path) -> list[Path]:
    """Every directory holding conversation shards, oldest first.

    Found by looking for the shards rather than by matching a directory name.
    Two layouts exist in practice and both must work: an export moved in whole
    (``archive/chatgpt/<opaque-name>/``) and one the daily job filed from a zip
    (``archive/chatgpt/<date>/extracted/``). A name-matching or single-level
    scan silently misses one of them.

    Sorted so that when several batches hold the same conversation, the later
    batch is processed last and its version wins the upsert.
    """
    if not chatgpt_root.exists():
        return []
    batches = {f.parent for f in chatgpt_root.rglob(CONVERSATION_GLOB) if f.is_file()}
    return sorted(batches, key=_batch_sort_key)


def batch_label(chatgpt_root: Path, batch_dir: Path) -> str:
    """Stable name for a batch: its path relative to archive/chatgpt/."""
    try:
        rel = batch_dir.relative_to(chatgpt_root)
    except ValueError:
        return batch_dir.name
    return str(rel) if str(rel) != "." else chatgpt_root.name


def _batch_sort_key(p: Path) -> tuple:
    """Order by the export timestamp in the directory path, then by path.

    ``<hash>-2026-08-18-20-22-53-<hash>`` -- the hash prefix makes plain
    lexical sort arbitrary, so the embedded date is extracted when present.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2}(?:-\d{2}-\d{2}-\d{2})?)", str(p))
    return (m.group(1) if m else "", str(p))


def load_assets(batch_dir: Path) -> dict[str, str]:
    f = batch_dir / ASSET_NAME_FILE
    if not f.exists():
        return {}
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def iter_exports(chatgpt_root: Path) -> Iterator[ParsedDoc]:
    for batch_dir in iter_batches(chatgpt_root):
        assets = load_assets(batch_dir)
        root_key = str(chatgpt_root)
        label = batch_label(chatgpt_root, batch_dir)
        for f in sorted(batch_dir.glob(CONVERSATION_GLOB)):
            yield from parse_conversations(f, label, root_key, assets)


def survey_batch(batch_dir: Path, label: str | None = None) -> dict:
    """Classify every file in a batch: parsed, PII-excluded, skipped, unknown.

    1,769 of the real batch's 1,799 files are ``.dat`` attachment payloads, so
    they are counted rather than listed; anything genuinely unrecognised is
    surfaced by ``lexicon report`` instead of being ignored.
    """
    parsed: list[str] = []
    excluded: list[str] = []
    skipped: list[str] = []
    unknown: list[str] = []
    attachments = 0
    for p in sorted(batch_dir.iterdir()):
        if not p.is_file() or p.name == ".DS_Store":
            continue
        name = p.name
        if name in PII_FILENAMES:
            excluded.append(name)
        elif p.match(CONVERSATION_GLOB):
            parsed.append(name)
        elif name in SKIP_REASONS:
            skipped.append(f"{name} ({SKIP_REASONS[name]})")
        elif name.startswith(("file-", "file_")) and p.suffix == ".dat":
            attachments += 1
        else:
            unknown.append(name)
    if attachments:
        skipped.append(f"{attachments} *.dat attachment payloads (binary; filenames indexed inline)")
    return {
        "batch": label or batch_dir.name,
        "parsed": parsed,
        "pii_excluded": excluded,
        "skipped": skipped,
        "unknown": unknown,
    }
