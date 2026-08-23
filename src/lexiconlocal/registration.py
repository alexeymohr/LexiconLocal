"""Is the MCP server actually registered with the clients that should have it?

Phase 2 recorded the server as registered in Claude Code, Codex and Claude
Desktop. On 2026-08-19 only the first two were true. Claude Desktop's config
held one server (``apple-dev``), its log directory had no
``mcp-server-lexicon.log``, and ``mcp.log`` mentioned lexicon zero times: the
server was never launched there, not once.

The likely mechanism is worth recording, because it decides whether the fix
holds. ``claude_desktop_config.json`` is not a hand-maintained config file --
Claude Desktop owns it and rewrites it whole, since the same file carries a
9 KB ``preferences`` blob of live UI state. An edit made while the app is
running is liable to be overwritten from memory. So the registration is not
something to write once and trust; it is state to verify, which is what this
module is for.

Three config formats, three parsers. A client whose config file is absent is
reported as such and is not a failure -- not everyone has every client.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "lexicon"

CLAUDE_CODE_CONFIG = Path("~/.claude.json").expanduser()
CODEX_CONFIG = Path("~/.codex/config.toml").expanduser()
CLAUDE_DESKTOP_CONFIG = Path(
    "~/Library/Application Support/Claude/claude_desktop_config.json"
).expanduser()


@dataclass
class Registration:
    client: str
    config: Path
    config_exists: bool
    registered: bool
    command: str | None = None

    @property
    def command_ok(self) -> bool:
        return bool(self.command) and Path(self.command).exists()

    @property
    def ok(self) -> bool:
        if not self.config_exists:
            return True  # client not installed; nothing to be wrong about
        return self.registered and self.command_ok

    def detail(self) -> str:
        if not self.config_exists:
            return f"{self.client}: no config at {self.config} (client not installed)"
        if not self.registered:
            return f"{self.client}: '{SERVER_NAME}' NOT registered in {self.config}"
        if not self.command_ok:
            return f"{self.client}: registered but command missing: {self.command}"
        return f"{self.client}: registered -> {self.command}"


def _json_client(client: str, path: Path) -> Registration:
    if not path.exists():
        return Registration(client, path, False, False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Registration(client, path, True, False)
    entry = ((data or {}).get("mcpServers") or {}).get(SERVER_NAME)
    if not isinstance(entry, dict):
        return Registration(client, path, True, False)
    return Registration(client, path, True, True, entry.get("command"))


def _codex(path: Path = CODEX_CONFIG) -> Registration:
    """Read ``[mcp_servers.lexicon] command = "..."`` without a TOML dependency.

    Only the one table matters, and Python's tomllib would happily choke on an
    unrelated syntax error elsewhere in a long user config -- turning "is the
    server registered" into "is the whole file valid". A targeted scan answers
    the actual question.
    """
    if not path.exists():
        return Registration("Codex", path, False, False)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Registration("Codex", path, True, False)
    m = re.search(
        rf"^\[mcp_servers\.{re.escape(SERVER_NAME)}\]\s*$(.*?)(?=^\[|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return Registration("Codex", path, True, False)
    cmd = re.search(r'^\s*command\s*=\s*"([^"]*)"', m.group(1), re.MULTILINE)
    return Registration("Codex", path, True, True, cmd.group(1) if cmd else None)


def registrations(
    *,
    claude_code: Path | None = None,
    codex: Path | None = None,
    desktop: Path | None = None,
) -> list[Registration]:
    return [
        _json_client("Claude Code", claude_code or CLAUDE_CODE_CONFIG),
        _codex(codex or CODEX_CONFIG),
        _json_client("Claude Desktop", desktop or CLAUDE_DESKTOP_CONFIG),
    ]
