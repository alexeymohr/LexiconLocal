#!/usr/bin/env python3
"""Register the Lexicon MCP server with every client that should have it.

Idempotent. Run it, or run it with --check to verify and change nothing.

Why this exists rather than a one-time hand edit: Phase 2 recorded the server
as registered in all three clients, and on 2026-08-19 Claude Desktop had no
entry, no ``mcp-server-lexicon.log``, and zero mentions of lexicon in its
``mcp.log`` -- it had never launched the server once.

The likely mechanism, and the reason a hand edit is not enough:
``claude_desktop_config.json`` is not a hand-maintained config. Claude Desktop
owns it and rewrites it whole -- the same 9 KB file carries its live UI
preferences, and its mtime moves several times a day. An entry added while the
app is running is liable to be overwritten from the app's in-memory copy, which
fits the evidence exactly. So write it with Desktop quit where possible, and
either way let ``lexicon preflight`` verify rather than assuming.

    ./scripts/register_mcp.py           write any missing entries
    ./scripts/register_mcp.py --check   report only, exit 1 if anything is missing
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lexiconlocal.registration import (  # noqa: E402
    SERVER_NAME,
    CLAUDE_DESKTOP_CONFIG,
    registrations,
)

COMMAND = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "lexicon-mcp"

#: Only JSON clients are written. Codex's config is TOML with a long tail of
#: user settings, and Claude Code owns ~/.claude.json through `claude mcp add`;
#: rewriting either from here would risk more than it fixes. Both are still
#: *checked*, by preflight and by --check below.
WRITABLE = {"Claude Desktop": CLAUDE_DESKTOP_CONFIG}


def write_entry(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = data.setdefault("mcpServers", {})
    if servers.get(SERVER_NAME, {}).get("command") == str(COMMAND):
        return "already registered"
    backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if path.exists():
        shutil.copy2(path, backup)
    servers[SERVER_NAME] = {"command": str(COMMAND)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"registered (backup: {backup.name})" if path.exists() else "registered"


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    if not COMMAND.exists():
        print(f"FAIL  the server binary does not exist: {COMMAND}")
        print("      run `uv sync` first")
        return 1

    if not check_only:
        for client, path in WRITABLE.items():
            try:
                print(f"  {client:<16} {write_entry(path)}")
            except (OSError, ValueError) as e:
                print(f"  {client:<16} FAILED: {e}")

    print("verifying:")
    bad = False
    for r in registrations():
        print(f"  {'ok  ' if r.ok else 'FAIL'}  {r.detail()}")
        bad = bad or not r.ok
    if bad:
        return 1
    print()
    print("Registered everywhere. Claude Desktop reads this file at launch, so")
    print("restart it if it is running -- and note that it rewrites this file")
    print("from memory, which is how the Phase 2 entry was most likely lost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
