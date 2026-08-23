"""Claude account-export parser.

Fixtures are synthetic but reproduce shapes observed in the real 2026-08-18
dump (CLAUDE.md forbids copying real transcripts into this repo).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lexiconlocal.chunk import KIND_PROSE, KIND_TOOL_EVENT
from lexiconlocal.parsers import claude_export as ce


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _msg(uuid, sender, text, parent=None, created="2026-01-01T00:00:00Z", blocks=None):
    return {
        "uuid": uuid, "sender": sender, "text": text,
        "content": blocks if blocks is not None else [{"type": "text", "text": text}],
        "created_at": created, "updated_at": created,
        "parent_message_uuid": parent, "attachments": [], "files": [],
    }


@pytest.fixture
def batch(tmp_path: Path) -> Path:
    """One export batch with the real directory shape."""
    d = tmp_path / "claude" / "data-abc-123-def-batch-0000"

    conversations = [
        # 1. linear conversation, assistant message carries thinking + tool blocks
        {
            "uuid": "conv-linear", "name": "Linear chat", "summary": "a summary line",
            "created_at": "2026-02-01T10:00:00Z", "updated_at": "2026-02-01T10:05:00Z",
            "chat_messages": [
                _msg("m1", "human", "LINEAR_QUESTION about bounce?", None, "2026-02-01T10:00:00Z"),
                # flat `text` is the superset: it carries the thinking too
                _msg(
                    "m2", "assistant",
                    "THINKING_CONTENT_MARKER\nLINEAR_ANSWER_MARKER",
                    "m1", "2026-02-01T10:00:05Z",
                    blocks=[
                        {"type": "thinking", "thinking": "THINKING_CONTENT_MARKER"},
                        {"type": "text", "text": "LINEAR_ANSWER_MARKER"},
                        {"type": "tool_use", "name": "bash",
                         "input": {"command": "pytest tests/test_bounce.py"}},
                        {"type": "tool_result", "content": "TOOL_OUTPUT_MARKER should never be indexed"},
                        {"type": "token_budget", "budget": 1000},
                    ],
                ),
            ],
        },
        # 2. genuine branch point: m1 has two children
        {
            "uuid": "conv-branched", "name": "Branched chat",
            "created_at": "2026-03-01T10:00:00Z",
            "chat_messages": [
                _msg("b1", "human", "BRANCH_ROOT_QUESTION", None, "2026-03-01T10:00:00Z"),
                _msg("b2", "assistant", "ABANDONED_TAKE_MARKER", "b1", "2026-03-01T10:00:10Z"),
                _msg("b3", "assistant", "CANONICAL_TAKE_MARKER", "b1", "2026-03-01T10:00:20Z"),
                _msg("b4", "human", "CANONICAL_FOLLOWUP_MARKER", "b3", "2026-03-01T10:00:30Z"),
            ],
        },
        # 3. the 2023 shape: every parent is null, no branch point at all
        {
            "uuid": "conv-nullparents", "name": "Old chat with no threading",
            "created_at": "2023-09-09T06:45:55Z",
            "chat_messages": [
                _msg(f"n{i}", "human" if i % 2 == 0 else "assistant",
                     f"OLD_MESSAGE_{i}_MARKER", None, f"2023-09-09T06:{45+i:02d}:00Z")
                for i in range(6)
            ],
        },
    ]
    _write(d / "conversations.json", conversations)

    _write(d / "memories.json", [{
        "account_uuid": "acct-1",
        "conversations_memory": "# Work context\n\nMEMORY_BLOB_MARKER about the user.",
        "memory_files": [
            {"path": "notes/projects.md", "content": "MEMORY_FILE_MARKER content",
             "updated_at": "2026-05-05T00:00:00Z"},
        ],
    }])

    _write(d / "projects" / "proj-1.json", {
        "uuid": "proj-1", "name": "email searching",
        "description": "PROJECT_DESCRIPTION_MARKER read only",
        "prompt_template": "", "is_private": True,
        "created_at": "2026-07-16T18:08:43Z",
        "docs": [{"uuid": "doc-1", "filename": "claude/scope.md",
                  "content": "# Scope\n\nPROJECT_DOC_MARKER details.",
                  "created_at": "2026-07-16T18:09:00Z"}],
    })

    # PII -- must never be indexed
    _write(d / "users.json", [{"email_address": "PII_EMAIL_MARKER@example.com",
                               "full_name": "PII_NAME_MARKER",
                               "verified_phone_number": "+15550001111",
                               "uuid": "u1"}])
    _write(d / "login_history.json", {"logins": [
        {"ip": "203.0.113.9", "at": "2026-08-01T00:00:00Z", "marker": "PII_LOGIN_MARKER"}]})
    return tmp_path / "claude"


def _all_text(docs) -> str:
    return "\n".join(c.text for d in docs for c in d.chunks)


# --------------------------------------------------------------------------
# PII
# --------------------------------------------------------------------------

def test_pii_files_are_never_indexed(batch):
    docs = list(ce.iter_exports(batch))
    text = _all_text(docs)
    for marker in ("PII_EMAIL_MARKER", "PII_NAME_MARKER", "PII_LOGIN_MARKER", "+15550001111"):
        assert marker not in text, f"{marker} leaked out of the export"


def test_survey_classifies_pii_and_flags_unknown_files(batch):
    bdir = ce.iter_batches(batch)[0]
    sv = ce.survey_batch(bdir)
    assert set(sv["pii_excluded"]) == {"users.json", "login_history.json"}
    assert "conversations.json" in sv["parsed"]
    assert "memories.json" in sv["parsed"]
    assert any(p.startswith("projects/") for p in sv["parsed"])
    assert sv["unknown"] == []

    # A file the parser does not know about must be surfaced, not ignored.
    (bdir / "surprise_new_export_file.json").write_text("{}", encoding="utf-8")
    assert ce.survey_batch(bdir)["unknown"] == ["surprise_new_export_file.json"]


# --------------------------------------------------------------------------
# message content
# --------------------------------------------------------------------------

def test_tool_result_bodies_are_never_indexed(batch):
    docs = list(ce.iter_exports(batch))
    assert "TOOL_OUTPUT_MARKER" not in _all_text(docs)


def test_flat_text_superset_captures_thinking_without_duplicating(batch):
    docs = list(ce.iter_exports(batch))
    linear = [d for d in docs if d.extra.get("conversation_id") == "conv-linear"][0]
    text = "\n".join(c.text for c in linear.chunks if c.kind == KIND_PROSE)
    assert "THINKING_CONTENT_MARKER" in text
    assert "LINEAR_ANSWER_MARKER" in text
    # the flat field is used once, not concatenated with the blocks as well
    assert text.count("LINEAR_ANSWER_MARKER") == 1


def test_tool_use_becomes_a_searchable_header(batch):
    docs = list(ce.iter_exports(batch))
    linear = [d for d in docs if d.extra.get("conversation_id") == "conv-linear"][0]
    events = "\n".join(c.text for c in linear.chunks if c.kind == KIND_TOOL_EVENT)
    assert "pytest tests/test_bounce.py" in events
    assert "bash" in events


def test_summary_is_indexed(batch):
    docs = list(ce.iter_exports(batch))
    linear = [d for d in docs if d.extra.get("conversation_id") == "conv-linear"][0]
    assert "a summary line" in "\n".join(c.text for c in linear.chunks)


# --------------------------------------------------------------------------
# branching
# --------------------------------------------------------------------------

def test_genuine_branch_splits_canonical_from_abandoned(batch):
    docs = list(ce.iter_exports(batch))
    canon = [d for d in docs if d.extra.get("conversation_id") == "conv-branched"
             and d.extra["branch"] == "canonical"][0]
    aband = [d for d in docs if d.extra.get("conversation_id") == "conv-branched"
             and d.extra["branch"] == "abandoned"][0]
    ctext = "\n".join(c.text for c in canon.chunks)
    atext = "\n".join(c.text for c in aband.chunks)
    assert "CANONICAL_TAKE_MARKER" in ctext and "CANONICAL_FOLLOWUP_MARKER" in ctext
    assert "ABANDONED_TAKE_MARKER" not in ctext
    assert "ABANDONED_TAKE_MARKER" in atext


def test_null_parents_everywhere_keeps_the_whole_conversation_canonical():
    """The 2023 shape: no threading data at all.

    Deciding branches by path reachability rather than by real branch points
    buried 45 of one real 46-message conversation under the abandoned-branch
    boost. Everything must stay canonical when there is no branch point.
    """
    msgs = [_msg(f"n{i}", "human", f"m{i}", None, f"2023-09-09T06:{45+i:02d}:00Z")
            for i in range(6)]
    canonical, off_path = ce.split_canonical(msgs)
    assert len(canonical) == 6
    assert off_path == []


def test_broken_threading_with_a_branch_point_still_keeps_everything():
    """A branch point exists but the walk collapses -- keep the thread."""
    msgs = [
        _msg("x1", "human", "a", "missing-parent", "2026-01-01T00:00:00Z"),
        _msg("x2", "assistant", "b", "missing-parent", "2026-01-01T00:00:01Z"),
    ]
    canonical, off_path = ce.split_canonical(msgs)
    assert len(canonical) == 2 and off_path == []


def test_abandoned_branch_is_downranked_by_the_boost_table():
    from lexiconlocal.search import boost_for, BOOSTS
    normal = boost_for("transcript", "prose", "/x/conv", None)
    aband = boost_for("transcript", "prose", "/x/conv", "abandoned")
    assert aband < normal
    assert aband == BOOSTS["chatgpt:abandoned"]


# --------------------------------------------------------------------------
# memories and projects
# --------------------------------------------------------------------------

def test_memories_are_indexed_as_their_own_source_type(batch):
    docs = [d for d in ce.iter_exports(batch) if d.source_type == "claude-memory"]
    text = _all_text(docs)
    assert "MEMORY_BLOB_MARKER" in text
    assert "MEMORY_FILE_MARKER" in text
    kinds = {d.extra["kind"] for d in docs}
    assert kinds == {"conversations_memory", "memory_file"}


def test_projects_and_their_docs_are_indexed(batch):
    docs = [d for d in ce.iter_exports(batch) if d.source_type == "claude-project"]
    text = _all_text(docs)
    assert "PROJECT_DESCRIPTION_MARKER" in text
    assert "PROJECT_DOC_MARKER" in text
    assert {d.extra["kind"] for d in docs} == {"project", "project_doc"}


def test_memory_and_project_boosts_rank_above_transcripts():
    from lexiconlocal.search import boost_for
    transcript = boost_for("transcript", "prose", "/x", None)
    assert boost_for("claude-memory", "prose", "/x", None) > transcript
    assert boost_for("claude-project", "prose", "/x", None) > transcript


# --------------------------------------------------------------------------
# additive batches
# --------------------------------------------------------------------------

def test_later_batch_updates_rather_than_duplicates(batch, tmp_path):
    """A second export re-including a conversation must not duplicate it."""
    first = {d.path for d in ce.iter_exports(batch)}

    b2 = batch / "data-abc-999-zzz-batch-0001"
    _write(b2 / "conversations.json", [{
        "uuid": "conv-linear", "name": "Linear chat",
        "created_at": "2026-02-01T10:00:00Z",
        "chat_messages": [
            _msg("m1", "human", "LINEAR_QUESTION about bounce?", None, "2026-02-01T10:00:00Z"),
            _msg("m9", "assistant", "SECOND_BATCH_NEW_REPLY", "m1", "2026-02-02T10:00:00Z"),
        ],
    }, {
        "uuid": "conv-brandnew", "name": "Only in batch 1",
        "created_at": "2026-04-01T10:00:00Z",
        "chat_messages": [_msg("z1", "human", "BRAND_NEW_MARKER", None, "2026-04-01T10:00:00Z")],
    }])

    docs = list(ce.iter_exports(batch))

    # The stream yields one document per (batch, conversation); identity is the
    # path, and the indexer upserts on it, so the last occurrence is what
    # survives. Collapse the same way here.
    surviving = {}
    for d in docs:
        surviving[d.path] = d

    linear = [d for d in surviving.values()
              if d.extra.get("conversation_id") == "conv-linear"
              and d.extra["branch"] == "canonical"]
    assert len(linear) == 1, "the same conversation in two batches is one document"
    # batches are processed oldest-first, so the newest export wins
    assert linear[0].extra["batch"].endswith("batch-0001")
    assert "SECOND_BATCH_NEW_REPLY" in "\n".join(c.text for c in linear[0].chunks)

    new_paths = set(surviving) - first
    assert any("conv-brandnew" in p for p in new_paths)


def test_batches_are_discovered_in_order(batch):
    b2 = batch / "data-abc-999-zzz-batch-0001"
    b2.mkdir(parents=True, exist_ok=True)
    (b2 / "conversations.json").write_text("[]", encoding="utf-8")
    names = [b.name for b in ce.iter_batches(batch)]
    assert names == sorted(names)
    assert len(names) == 2


def test_memories_and_projects_are_keyed_per_batch(batch, tmp_path):
    """A later export must not overwrite an earlier snapshot.

    Conversations have stable uuids and the newest export merely holds more of
    them, so updating in place is right. A memory blob has no id: it is a
    point-in-time snapshot, and overwriting it discards history the Lexicon
    exists to keep.
    """
    b2 = batch / "data-abc-999-zzz-batch-0001"
    _write(b2 / "memories.json", [{
        "account_uuid": "acct-1",
        "conversations_memory": "SECOND_BATCH_MEMORY_MARKER",
        "memory_files": [],
    }])
    _write(b2 / "projects" / "proj-1.json", {
        "uuid": "proj-1", "name": "email searching",
        "description": "SECOND_BATCH_PROJECT_MARKER",
        "prompt_template": "", "docs": [],
    })

    docs = list(ce.iter_exports(batch))
    mem = [d for d in docs if d.extra.get("kind") == "conversations_memory"]
    assert len(mem) == 2, "each batch's memory snapshot is its own document"
    assert len({d.path for d in mem}) == 2, "paths must not collide across batches"
    text = "\n".join(c.text for d in mem for c in d.chunks)
    assert "MEMORY_BLOB_MARKER" in text, "the earlier snapshot must survive"
    assert "SECOND_BATCH_MEMORY_MARKER" in text

    projs = [d for d in docs if d.extra.get("kind") == "project"]
    assert len({d.path for d in projs}) == len(projs), "project paths must not collide"
