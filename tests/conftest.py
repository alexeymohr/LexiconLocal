"""Synthetic fixtures only.

Never copy real transcripts or exports into this repo (CLAUDE.md). Everything
here is hand-written to reproduce the specific shapes Phase 1 documented.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

CONFIG_TEMPLATE = """\
schema_version: 1
lexicon_root: {root}
source_roots:
  - path: {repos}
    type: repos
exclude_dirs:
  - node_modules
  - .git
  - OLD-retired
  - worktrees
  - secrets
exclude_files:
  - "*.env"
  - ".env*"
  - "*.pem"
  - ".DS_Store"
never_index:
  - {root}/private
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def lexicon_tree(tmp_path: Path) -> Path:
    """A miniature ~/Lexicon plus a miniature ~/programming."""
    root = tmp_path / "Lexicon"
    repos = tmp_path / "programming"

    # --- curated notes ---------------------------------------------------
    _write(root / "INDEX.md", textwrap.dedent("""\
        # Lexicon INDEX

        ## Project families (alias groups)

        | Family | Members | Notes |
        |---|---|---|
        | **Maritime tools** | `Lighthouse`, `Harbor` | Historical alias: **`Beacon`** |

        ## Active projects

        | Project | One-liner | Repo path | Last activity | Aliases |
        |---|---|---|---|---|
        | Lighthouse | Sidecar manifest inspector | `~/programming/Lighthouse` | 2026-08-11 | Beacon, Light House |
        | Forge | Workshop tooling | `~/programming/Forge` | 2026-08-17 | Smithy |
        """))
    _write(root / "projects" / "forge" / "overview.md", textwrap.dedent("""\
        # Forge overview

        Isolated stem render is the verified approach for clean stems.
        Mixdown produced contaminated output in the three-track test.
        """))
    _write(root / "topics" / "audio.md", "# Audio topics\n\nLoudness and stems notes.\n")
    _write(root / "private" / "secret-notes.md", "# Private\n\nDo not index: hunter2 passphrase.\n")

    # --- in-place repo docs ----------------------------------------------
    _write(repos / "Forge" / "docs" / "mission.md", textwrap.dedent("""\
        # Mission 42 — Isolated stem verification

        We verified `Isolated stem render` against src/render/bounce.py
        and the error string `AAFParseError: invalid slot id 0x1F`.
        """))
    _write(repos / "Forge" / "runtime" / "generated.md", "# Generated noise\n\nignore me\n")
    _write(repos / "Lighthouse" / "README.md", "# Lighthouse\n\nSidecar inspection for harbor manifests.\n")
    _write(repos / "Lighthouse" / "node_modules" / "pkg" / "readme.md", "# should be excluded\n")
    _write(repos / "Lighthouse" / "secrets" / "notes.md", "# excluded dir\n")
    _write(repos / "loose-note.md", "# Loose\n\nA file at the root of the source tree.\n")

    # non-UTF-8 CLAUDE.md (latin-1 bytes that are invalid UTF-8)
    p = repos / "Lighthouse" / "CLAUDE.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes("# Lighthouse guide\n\nCaf\xe9 na\xefve r\xe9sum\xe9 notes.\n".encode("latin-1"))

    # a file carrying fake credentials -- must be redacted before storage
    _write(repos / "Lighthouse" / "docs" / "setup.md", textwrap.dedent("""\
        # Setup

        export OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz012345
        export GH=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
        aws key AKIAIOSFODNN7EXAMPLE
        """))

    # excluded by exclude_files
    _write(repos / "Lighthouse" / ".env", "SECRET=sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzz\n")

    _write(root / "config.yaml", CONFIG_TEMPLATE.format(root=root, repos=repos))
    (root / "archive" / "chatgpt").mkdir(parents=True, exist_ok=True)
    (root / "archive" / "claude").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def claude_code_archive(lexicon_tree: Path) -> Path:
    """Two files sharing one sessionId, plus the toolUseResult trap."""
    d = lexicon_tree / "archive" / "claude-code" / "-Users-operator-programming-Forge"
    sid = "session-aaaa-bbbb"
    cwd = str(lexicon_tree.parent / "programming" / "Forge")

    _write_jsonl(d / "part1.jsonl", [
        {"type": "custom-title", "sessionId": sid, "customTitle": "Bounce work"},
        {"type": "user", "sessionId": sid, "cwd": cwd, "gitBranch": "main",
         "timestamp": "2026-08-01T10:00:00Z",
         "message": {"role": "user", "content": "How do we isolate a track bounce?"}},
        {"type": "assistant", "sessionId": sid, "timestamp": "2026-08-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Use Isolated stem, not Mixdown."},
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "python render.py --track 3", "description": "render"}},
         ]}},
        # THE TRAP: tool output stored as a `user` record.
        {"type": "user", "sessionId": sid, "timestamp": "2026-08-01T10:00:06Z",
         "toolUseResult": {"stdout": "CONTAMINATED_TOOL_OUTPUT_MARKER rendering..."},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "CONTAMINATED_TOOL_OUTPUT_MARKER rendering..."}]}},
        {"type": "user", "sessionId": sid, "isMeta": True,
         "message": {"role": "user", "content": "META_RECORD_MARKER should be skipped"}},
    ])
    _write_jsonl(d / "part2.jsonl", [
        {"type": "assistant", "sessionId": sid, "cwd": cwd,
         "timestamp": "2026-08-01T11:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Second file, same session: verified clean stems."}]}},
    ])
    # A different session in the same directory.
    _write_jsonl(d / "other.jsonl", [
        {"type": "user", "sessionId": "session-cccc", "cwd": cwd,
         "timestamp": "2026-08-02T09:00:00Z",
         "message": {"role": "user", "content": "Unrelated second session."}},
    ])
    _write(d / "notes.md", "# Sidecar note\n\nAn ordinary markdown file in the archive.\n")
    return d


@pytest.fixture
def codex_archive(lexicon_tree: Path) -> Path:
    """A rollout with base_instructions bloat and function_call_output bloat."""
    d = lexicon_tree / "archive" / "codex" / "sessions" / "2026" / "08"
    cwd = str(lexicon_tree.parent / "programming" / "Lighthouse")
    bloat = "BASE_INSTRUCTIONS_MARKER " * 2000
    output_bloat = "FUNCTION_OUTPUT_MARKER " * 5000

    _write_jsonl(d / "rollout-2026-08-03T10-00-00-sess1.jsonl", [
        {"timestamp": "2026-08-03T10:00:00Z", "type": "session_meta",
         "payload": {"id": "sess1", "cwd": cwd, "cli_version": "1.2.3",
                     "base_instructions": {"text": bloat}}},
        {"timestamp": "2026-08-03T10:00:01Z", "type": "turn_context",
         "payload": {"cwd": cwd, "model": "gpt-5", "effort": "high"}},
        {"timestamp": "2026-08-03T10:00:02Z", "type": "response_item",
         "payload": {"type": "user_message", "text": "Investigate the AAF slot parsing bug."}},
        {"timestamp": "2026-08-03T10:00:03Z", "type": "response_item",
         "payload": {"type": "reasoning",
                     "summary": [{"text": "The slot id is read as unsigned."}]}},
        {"timestamp": "2026-08-03T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": {"command": "grep -rn slot_id src/"}}},
        {"timestamp": "2026-08-03T10:00:05Z", "type": "response_item",
         "payload": {"type": "function_call_output", "output": output_bloat}},
        {"timestamp": "2026-08-03T10:00:06Z", "type": "event_msg",
         "payload": {"type": "mcp_tool_call_end", "result": output_bloat}},
        {"timestamp": "2026-08-03T10:00:07Z", "type": "event_msg",
         "payload": {"type": "exec_command_end",
                     "command": "pytest tests/test_slots.py", "exit_code": 0}},
        {"timestamp": "2026-08-03T10:00:08Z", "type": "response_item",
         "payload": {"type": "agent_message", "text": "Fixed: slot id must be masked with 0xFF."}},
        {"timestamp": "2026-08-03T10:00:09Z", "type": "event_msg",
         "payload": {"type": "token_count", "total": 1234}},
    ])
    _write(lexicon_tree / "archive" / "codex" / "session_index.jsonl",
           json.dumps({"id": "sess1", "thread_name": "AAF slot parsing bug",
                       "updated_at": "2026-08-03T11:00:00Z"}) + "\n")
    _write(lexicon_tree / "archive" / "codex" / "memories-2026-08-18" / "MEMORY.md",
           "# Codex memory\n\nForge prefers Isolated stem for isolated renders.\n")
    _write(lexicon_tree / "archive" / "codex" / "attachments" / "paste1.txt",
           "A pasted attachment mentioning slot id 0x1F.\n")
    return d


@pytest.fixture
def chatgpt_export(lexicon_tree: Path) -> Path:
    """A ChatGPT export shaped like the real one.

    Deliberately mirrors the 2026-08-18 dump rather than the documented shape:
    conversations are **sharded** across ``conversations-NNN.json``, nodes carry
    **no ``children`` key** (only ``{id, message, parent}``), reasoning arrives
    as a ``thoughts`` content type with its text under its own key, and
    ``reasoning_recap`` is a content-free banner. Each of those broke the
    pre-dump parser.
    """
    d = lexicon_tree / "archive" / "chatgpt" / "export-2026-08-18-12-00-00"

    def msg(role, ctype="text", **kw):
        return {"author": {"role": role}, "create_time": kw.pop("t", 1786000000.0),
                "content": {"content_type": ctype, **kw}}

    mapping = {
        "root": {"id": "root", "message": None, "parent": None},
        "u1": {"id": "u1", "parent": "root",
               "message": msg("user", parts=["What is CANONICAL_QUESTION?"], t=1786000001.0)},
        "a1": {"id": "a1", "parent": "u1",
               "message": msg("assistant", parts=["CANONICAL_ANSWER_MARKER here."], t=1786000002.0)},
        "u2": {"id": "u2", "parent": "a1",
               "message": msg("user", parts=["Thanks, CANONICAL_TAIL."], t=1786000003.0)},
        # Sibling of a1 under the same parent: the genuine branch point.
        "a2": {"id": "a2", "parent": "u1",
               "message": msg("assistant", parts=["ABANDONED_BRANCH_MARKER here."], t=1786000004.0)},
    }
    conv1 = {"conversation_id": "conv1", "title": "Branched chat",
             "create_time": 1786000000.0, "update_time": 1786000100.0,
             "current_node": "u2", "mapping": mapping,
             "default_model_slug": "gpt-5-thinking"}
    _write(d / "conversations-000.json", json.dumps([conv1]))

    # Second shard: a linear thread with reasoning, a recap banner and an image.
    mapping2 = {
        "r": {"id": "r", "message": None, "parent": None},
        "m1": {"id": "m1", "parent": "r",
               "message": msg("user", ctype="multimodal_text", t=1786100001.0, parts=[
                   {"content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-ABC123"},
                   "Look at SHARDED_QUESTION_MARKER.",
               ])},
        "m2": {"id": "m2", "parent": "m1",
               "message": msg("assistant", ctype="thoughts", t=1786100002.0, thoughts=[
                   {"summary": "Weighing options", "content": "REASONING_TRACE_MARKER internal note."}
               ])},
        "m3": {"id": "m3", "parent": "m2",
               "message": msg("assistant", ctype="reasoning_recap", t=1786100003.0,
                              content="Thought for 1m 8s")},
        "m4": {"id": "m4", "parent": "m3",
               "message": msg("assistant", parts=["SHARDED_ANSWER_MARKER is the answer."],
                              t=1786100004.0)},
    }
    conv2 = {"conversation_id": "conv2", "title": "Sharded chat",
             "create_time": 1786100000.0, "current_node": "m4", "mapping": mapping2}
    _write(d / "conversations-001.json", json.dumps([conv2]))

    _write(d / "conversation_asset_file_names.json",
           json.dumps({"file-ABC123.dat": "ATTACHED_SCREENSHOT_NAME.png"}))
    # Present in every real export; must never be indexed.
    _write(d / "user.json", json.dumps({"email": "nobody@example.com",
                                        "phone_number": "+10000000000"}))
    _write(d / "chat.html", "<html>SHOULD_NOT_BE_INDEXED_HTML</html>")
    _write(d / "file-ABC123.dat", "binary-ish payload")
    return d


@pytest.fixture
def claude_export(lexicon_tree: Path) -> Path:
    d = lexicon_tree / "archive" / "claude" / "export-2026-08-18"
    conv = {"uuid": "cc-1", "name": "Claude web chat", "created_at": "2026-07-04T12:00:00Z",
            "chat_messages": [
                {"sender": "human", "text": "CLAUDE_EXPORT_QUESTION about loudness?"},
                {"sender": "assistant", "content": [{"type": "text", "text": "CLAUDE_EXPORT_ANSWER_MARKER."}]},
            ]}
    _write(d / "conversations.json", json.dumps([conv]))
    return d
