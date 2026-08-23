"""Parser behaviour, focused on the traps Phase 1 documented."""

from __future__ import annotations

from pathlib import Path

from lexiconlocal.chunk import KIND_PROSE, KIND_TOOL_EVENT
from lexiconlocal.parsers import chatgpt as chatgpt_parser
from lexiconlocal.parsers import claude_code as cc
from lexiconlocal.parsers import claude_export as claude_parser
from lexiconlocal.parsers import codex as codex_parser
from lexiconlocal.parsers.markdown import parse_markdown


def _all_text(doc) -> str:
    return "\n".join(c.text for c in doc.chunks)


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def test_claude_code_groups_multiple_files_into_one_session(lexicon_tree, claude_code_archive):
    files = sorted(claude_code_archive.rglob("*.jsonl"))
    groups = cc.group_by_session(files)
    assert len(files) == 3
    # Two files share a sessionId; the third is its own session.
    assert len(groups) == 2, "files must be grouped by sessionId, not counted individually"
    assert len(groups["session-aaaa-bbbb"]) == 2


def test_claude_code_excludes_tool_result_disguised_as_user_record(
    lexicon_tree, claude_code_archive
):
    """The documented trap: tool output stored in a `type: "user"` record."""
    files = sorted(claude_code_archive.glob("part*.jsonl"))
    doc = cc.parse_session("session-aaaa-bbbb", files, lexicon_tree / "archive")
    text = _all_text(doc)
    assert "CONTAMINATED_TOOL_OUTPUT_MARKER" not in text
    assert "How do we isolate a track bounce?" in text
    assert "Use Isolated stem, not Mixdown." in text
    assert "Second file, same session" in text


def test_claude_code_skips_meta_records(lexicon_tree, claude_code_archive):
    files = sorted(claude_code_archive.glob("part*.jsonl"))
    doc = cc.parse_session("session-aaaa-bbbb", files, lexicon_tree / "archive")
    assert "META_RECORD_MARKER" not in _all_text(doc)


def test_claude_code_captures_tool_headers_as_tool_event_chunks(
    lexicon_tree, claude_code_archive
):
    files = sorted(claude_code_archive.glob("part*.jsonl"))
    doc = cc.parse_session("session-aaaa-bbbb", files, lexicon_tree / "archive")
    events = [c for c in doc.chunks if c.kind == KIND_TOOL_EVENT]
    assert events, "tool_use blocks must become searchable tool_event chunks"
    joined = "\n".join(c.text for c in events)
    assert "python render.py --track 3" in joined
    assert "Bash" in joined


def test_claude_code_records_cwd_for_project_attribution(lexicon_tree, claude_code_archive):
    files = sorted(claude_code_archive.glob("part*.jsonl"))
    doc = cc.parse_session("session-aaaa-bbbb", files, lexicon_tree / "archive")
    assert doc.extra["cwd"].endswith("Forge")
    assert doc.extra["file_count"] == 2
    assert doc.doc_date == "2026-08-01"


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

def test_codex_skips_tool_output_and_base_instructions(lexicon_tree, codex_archive):
    path = next(codex_archive.glob("rollout-*.jsonl"))
    doc = codex_parser.parse_rollout(path)
    text = _all_text(doc)
    assert "BASE_INSTRUCTIONS_MARKER" not in text, "system prompt boilerplate must not be indexed"
    assert "FUNCTION_OUTPUT_MARKER" not in text, "tool output bodies must not be indexed"
    assert "Investigate the AAF slot parsing bug." in text
    assert "Fixed: slot id must be masked with 0xFF." in text
    assert "The slot id is read as unsigned." in text


def test_codex_indexes_tool_headers_only(lexicon_tree, codex_archive):
    path = next(codex_archive.glob("rollout-*.jsonl"))
    doc = codex_parser.parse_rollout(path)
    events = "\n".join(c.text for c in doc.chunks if c.kind == KIND_TOOL_EVENT)
    assert "grep -rn slot_id src/" in events
    assert "pytest tests/test_slots.py" in events


def test_codex_parse_is_far_smaller_than_the_source_file(lexicon_tree, codex_archive):
    path = next(codex_archive.glob("rollout-*.jsonl"))
    doc = codex_parser.parse_rollout(path)
    indexed = len(_all_text(doc))
    assert indexed < path.stat().st_size / 10, (
        "the bulk of a rollout is tool output and must be discarded"
    )


def test_codex_thread_titles_load(lexicon_tree, codex_archive):
    titles = codex_parser.load_thread_titles(
        lexicon_tree / "archive" / "codex" / "session_index.jsonl"
    )
    assert titles["sess1"] == "AAF slot parsing bug"


def test_codex_covers_archived_sessions(lexicon_tree, codex_archive):
    archived = lexicon_tree / "archive" / "codex" / "archived_sessions"
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "rollout-2026-07-01T00-00-00-old.jsonl").write_text(
        '{"timestamp":"2026-07-01T00:00:00Z","type":"response_item",'
        '"payload":{"type":"user_message","text":"archived session content"}}\n',
        encoding="utf-8",
    )
    found = list(codex_parser.iter_rollouts(lexicon_tree / "archive" / "codex"))
    assert any("archived_sessions" in str(p) for p in found)


# --------------------------------------------------------------------------
# ChatGPT branched export
# --------------------------------------------------------------------------

def _cg(lexicon_tree):
    return list(chatgpt_parser.iter_exports(lexicon_tree / "archive" / "chatgpt"))


def _by_conv(docs, conv_id, branch="canonical"):
    hits = [d for d in docs
            if d.extra.get("conversation_id") == conv_id and d.extra.get("branch") == branch]
    assert len(hits) == 1, f"expected exactly one {branch} doc for {conv_id}, got {len(hits)}"
    return hits[0]


def test_chatgpt_canonical_thread_follows_current_node(lexicon_tree, chatgpt_export):
    doc = _by_conv(_cg(lexicon_tree), "conv1")
    text = _all_text(doc)
    assert "CANONICAL_ANSWER_MARKER" in text
    assert "CANONICAL_TAIL" in text
    assert "ABANDONED_BRANCH_MARKER" not in text


def test_chatgpt_abandoned_branch_indexed_separately(lexicon_tree, chatgpt_export):
    doc = _by_conv(_cg(lexicon_tree), "conv1", branch="abandoned")
    assert "ABANDONED_BRANCH_MARKER" in _all_text(doc)


def test_chatgpt_reads_sharded_conversation_files(lexicon_tree, chatgpt_export):
    """The real export has no ``conversations.json`` -- only ``-NNN`` shards.

    The pre-dump parser globbed the singular name and would have indexed
    nothing at all from a 1.5 GB export.
    """
    assert not (chatgpt_export / "conversations.json").exists()
    doc = _by_conv(_cg(lexicon_tree), "conv2")
    assert "SHARDED_ANSWER_MARKER" in _all_text(doc)


def test_chatgpt_branching_needs_a_real_branch_point(lexicon_tree, chatgpt_export):
    """A linear thread must not be split, even though no node has ``children``.

    Reconstructing children from parent edges is the only way to tell a leaf
    from a branch point here; without it every node reads as a leaf and the
    thread is shredded into near-duplicate "abandoned" documents.
    """
    docs = _cg(lexicon_tree)
    assert [d for d in docs if d.extra.get("conversation_id") == "conv2"
            and d.extra.get("branch") == "abandoned"] == []


def test_chatgpt_reasoning_is_searchable_but_not_embedded(lexicon_tree, chatgpt_export):
    """`thoughts` text lives under its own key and is held at the FTS-only tier."""
    doc = _by_conv(_cg(lexicon_tree), "conv2")
    prose = "\n".join(c.text for c in doc.chunks if c.kind == KIND_PROSE)
    events = "\n".join(c.text for c in doc.chunks if c.kind == KIND_TOOL_EVENT)
    assert "REASONING_TRACE_MARKER" in events
    assert "REASONING_TRACE_MARKER" not in prose
    assert "SHARDED_ANSWER_MARKER" in prose


def test_chatgpt_drops_reasoning_recap_banner(lexicon_tree, chatgpt_export):
    assert "Thought for" not in _all_text(_by_conv(_cg(lexicon_tree), "conv2"))


def test_chatgpt_renders_attachment_filenames_inline(lexicon_tree, chatgpt_export):
    """The .dat payload is binary, but its original filename is findable."""
    assert "ATTACHED_SCREENSHOT_NAME.png" in _all_text(_by_conv(_cg(lexicon_tree), "conv2"))


def test_chatgpt_survey_excludes_pii_and_bulk_and_flags_nothing_unknown(
    lexicon_tree, chatgpt_export
):
    sv = chatgpt_parser.survey_batch(chatgpt_export)
    assert sv["pii_excluded"] == ["user.json"]
    assert sv["unknown"] == []
    assert any("chat.html" in line for line in sv["skipped"])
    assert any(".dat attachment" in line for line in sv["skipped"])
    assert sorted(sv["parsed"]) == ["conversations-000.json", "conversations-001.json"]


def test_chatgpt_pii_and_html_never_reach_a_document(lexicon_tree, chatgpt_export):
    blob = "\n".join(_all_text(d) for d in _cg(lexicon_tree))
    assert "nobody@example.com" not in blob
    assert "+10000000000" not in blob
    assert "SHOULD_NOT_BE_INDEXED_HTML" not in blob


def test_chatgpt_documents_are_keyed_by_conversation_not_shard(lexicon_tree, chatgpt_export):
    """A later export shards differently; path-keyed docs would duplicate everything."""
    for doc in _cg(lexicon_tree):
        assert "conversations-0" not in doc.path
        assert doc.extra["conversation_id"] in doc.path


def test_claude_export_parses_flat_messages(lexicon_tree, claude_export):
    docs = list(claude_parser.iter_exports(lexicon_tree / "archive" / "claude"))
    assert len(docs) == 1
    text = _all_text(docs[0])
    assert "CLAUDE_EXPORT_ANSWER_MARKER" in text
    assert docs[0].doc_date == "2026-07-04"


# --------------------------------------------------------------------------
# Markdown + encoding
# --------------------------------------------------------------------------

def test_non_utf8_markdown_is_read_with_fallback_and_flagged(lexicon_tree):
    p = lexicon_tree.parent / "programming" / "Lighthouse" / "CLAUDE.md"
    doc = parse_markdown(p, source_type="repo-doc", project="Lighthouse", root="programming")
    assert doc is not None, "a non-UTF-8 file must never be silently dropped"
    assert doc.used_encoding_fallback is True
    assert "Lighthouse guide" in _all_text(doc)


def test_markdown_chunks_are_prose(lexicon_tree):
    p = lexicon_tree.parent / "programming" / "Forge" / "docs" / "mission.md"
    doc = parse_markdown(p, source_type="repo-doc", project="Forge", root="programming")
    assert all(c.kind == KIND_PROSE for c in doc.chunks)
    assert doc.title == "Mission 42 — Isolated stem verification"


# --------------------------------------------------------------------------
# Redaction boundaries
# --------------------------------------------------------------------------

def test_redaction_does_not_eat_file_paths_or_urls():
    """The first real index redacted the middle of ordinary paths.

    Paths and symbols are what exact-identifier search depends on, so the
    high-entropy rule must not span "/".
    """
    from lexiconlocal.redact import redact

    for text in (
        "see (/Users/operator/programming/Kiln/Sources/App/Services/BriefPublishService.swift:37-92)",
        "https://raw.githubusercontent.com/someorg/somerepo/refs/heads/main/docs/GettingStarted.md",
        "commit 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c passed",
    ):
        out, kinds = redact(text)
        assert out == text, f"redaction damaged: {out}"
        assert not kinds


def test_redaction_still_catches_real_secrets():
    from lexiconlocal.redact import redact

    for text, expected in (
        # A Google API key is literally "AIza" plus 35 characters.
        ("YOUTUBE_API_KEY=AIza" + "B" * 35, "google-api-key"),
        ("export K=sk-abcdefghijklmnopqrstuvwxyz0123", "openai-key"),
        ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "github-token"),
        ("aws AKIAIOSFODNN7EXAMPLE here", "aws-access-key"),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "pem"),
    ):
        out, kinds = redact(text)
        assert expected in kinds, f"{expected} not caught in {text!r} -> {kinds}"
        assert "[REDACTED:" in out


def test_chatgpt_batch_found_when_nested_under_an_extraction_dir(lexicon_tree, chatgpt_export):
    """The zip path files shards into <date>/extracted/, one level deeper.

    A single-level scan of archive/chatgpt/ finds the moved-in-whole layout and
    misses this one entirely.
    """
    root = lexicon_tree / "archive" / "chatgpt"
    nested = root / "2026-09-01" / "extracted"
    nested.mkdir(parents=True)
    (nested / "conversations-000.json").write_text(
        (chatgpt_export / "conversations-001.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    batches = chatgpt_parser.iter_batches(root)
    assert nested in batches
    labels = {chatgpt_parser.batch_label(root, b) for b in batches}
    assert "2026-09-01/extracted" in labels


def test_chatgpt_unknown_content_type_is_recorded_not_dropped(lexicon_tree, chatgpt_export):
    """A content type the parser has never seen must leave a trace.

    Every ChatGPT parsing defect so far has been a silent omission. A new
    content type is a format change; it has to be greppable in the index rather
    than vanish.
    """
    import json as _json

    d = lexicon_tree / "archive" / "chatgpt" / "future-export"
    d.mkdir(parents=True)
    conv = {"conversation_id": "future", "title": "Future format",
            "create_time": 1790000000.0, "current_node": "n1",
            "mapping": {
                "r": {"id": "r", "message": None, "parent": None},
                "n1": {"id": "n1", "parent": "r", "message": {
                    "author": {"role": "assistant"}, "create_time": 1790000001.0,
                    "content": {"content_type": "holographic_diagram", "frames": []}}},
            }}
    (d / "conversations-000.json").write_text(_json.dumps([conv]), encoding="utf-8")
    docs = [x for x in _cg(lexicon_tree) if x.extra.get("conversation_id") == "future"]
    assert docs, "a conversation of only unknown content must still produce a document"
    assert "unhandled content_type: holographic_diagram" in _all_text(docs[0])
