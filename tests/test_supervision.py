"""Phase 5 D2/D6: does the system notice when its own automation stops?

The 2026-08-19 outage was not a crash. Three LaunchAgents were switched off in
System Settings, ``launchctl`` forgot them, capture stopped, and nothing said a
word for most of a day. Every test here exists because some check would have
caught that and did not exist.
"""

from __future__ import annotations

import json
import os
import textwrap

import pytest
from pathlib import Path

from lexiconlocal import agents as A
from lexiconlocal import registration as R

# A trimmed `sfltool dumpbtm`, in the real layout: Disposition precedes
# Identifier, and the identifier carries a numeric type prefix.
BTM_DUMP = """\
========================
 Records for UID 501 : ABCD
========================

 Items:

 #1:
                 UUID: 11111111-1111-1111-1111-111111111111
                 Name: bash
                 Type: legacy agent (0x10008)
          Disposition: [enabled, allowed, notified] (0x8)
           Identifier: 8.com.example.fine
                  URL: file:///Users/x/Library/LaunchAgents/com.example.fine.plist

 #2:
                 UUID: 22222222-2222-2222-2222-222222222222
                 Name: bash
                 Type: legacy agent (0x10008)
          Disposition: [enabled, disallowed, notified] (0x9)
           Identifier: 8.com.lexiconlocal.daily
                  URL: file:///Users/x/Library/LaunchAgents/com.lexiconlocal.daily.plist
"""


# ---------------------------------------------------------------------------
# BTM parsing
# ---------------------------------------------------------------------------

def test_btm_parses_disposition_per_label(monkeypatch):
    monkeypatch.setenv("LEXICON_BTM", "force")
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=15.0: BTM_DUMP)
    d = A.btm_dispositions()
    assert d["com.example.fine"] == "enabled, allowed, notified"
    assert d["com.lexiconlocal.daily"] == "enabled, disallowed, notified"


def test_btm_empty_read_is_unknown_not_allowed(monkeypatch):
    """An unreadable BTM must not be mistaken for a clean bill of health.

    A transient empty read was observed converting a real FAIL into a pass.
    """
    monkeypatch.setenv("LEXICON_BTM", "force")
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=15.0: "")
    assert A.btm_dispositions() == {}
    st = A.AgentState("l", "p", Path("/nope"), True, True, disposition=None)
    assert st.allowed is None


def test_btm_read_is_skipped_when_unattended(monkeypatch):
    """An unattended caller must never execute sfltool at all.

    ``sfltool dumpbtm`` raises the macOS admin-password dialog via authd.
    Observed 2026-08-21: opening a Claude conversation fired the SessionEnd
    hook, the watchdog ran, and a password prompt appeared out of nowhere.
    """
    monkeypatch.delenv("LEXICON_BTM", raising=False)
    monkeypatch.setattr(A.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(A.sys.stdout, "isatty", lambda: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=15.0: calls.append(cmd) or BTM_DUMP)
    assert A.btm_dispositions() == {}
    assert not calls, "the gate must short-circuit before any subprocess"


def test_btm_skip_wins_even_interactively(monkeypatch):
    monkeypatch.setenv("LEXICON_BTM", "skip")
    monkeypatch.setattr(A.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=15.0: BTM_DUMP)
    assert A.btm_dispositions() == {}


# ---------------------------------------------------------------------------
# Agent state: loaded is not the same as runnable
# ---------------------------------------------------------------------------

def _state(**kw):
    base = dict(label="com.lexiconlocal.daily", purpose="x", plist=Path("/p"),
                plist_exists=True, loaded=True, disposition="enabled, allowed, notified")
    base.update(kw)
    return A.AgentState(**base)


def test_loaded_but_disallowed_is_not_ok():
    """The exact state after `launchctl bootstrap` on a switched-off agent.

    launchctl accepts it and `launchctl list` shows it, but macOS removes it
    again. A check that only asked "is it loaded" would report green.
    """
    st = _state(disposition="enabled, disallowed, notified")
    assert st.loaded is True
    assert st.allowed is False
    assert st.ok is False
    assert "System Settings" in st.problem()


def test_healthy_agent_is_ok():
    assert _state().ok is True
    assert _state().problem() == ""


def test_missing_plist_and_unloaded_are_reported_distinctly():
    assert "plist missing" in _state(plist_exists=False).problem()
    assert "not registered" in _state(loaded=False).problem()


def test_unknown_btm_does_not_fail_the_check():
    """On a machine without sfltool, unknown must not mean broken."""
    assert _state(disposition=None).ok is True


# ---------------------------------------------------------------------------
# Detection records (amendment A2)
# ---------------------------------------------------------------------------

def test_detection_record_captures_the_diagnostic_fields(monkeypatch):
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=15.0: "")
    monkeypatch.setattr(A, "_boot_time", lambda: "2026-08-07T07:37:37")
    monkeypatch.setattr(A, "_uptime_seconds", lambda: 1_073_000)
    rec = A.detection_record([
        _state(disposition="enabled, disallowed, notified"),
        _state(label="com.lexiconlocal.golden"),
    ])
    assert rec["uptime_seconds"] == 1_073_000
    assert rec["boot_time"] == "2026-08-07T07:37:37"
    assert "session_manager" in rec and "console_user" in rec
    assert [p["label"] for p in rec["problems"]] == ["com.lexiconlocal.daily"]
    assert rec["problems"][0]["disposition"] == "enabled, disallowed, notified"
    assert rec["healthy"] == ["com.lexiconlocal.golden"]
    assert rec["detected_at"]


def test_detections_are_append_only(tmp_path: Path):
    A.append_detection(tmp_path, {"detected_at": "a"})
    path = A.append_detection(tmp_path, {"detected_at": "b"})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(x)["detected_at"] for x in lines] == ["a", "b"]


def test_watchdog_records_and_notifies_only_on_failure(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    class _Done:
        stdout = ""

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _Done()

    # Patch subprocess itself rather than `_run`, so the notification path is
    # exercised end to end; `_run`'s own probes land in `calls` too, hence the
    # osascript-specific assertions below.
    monkeypatch.setattr(A.subprocess, "run", _fake_run)

    monkeypatch.setattr(A, "agent_states", lambda: [_state()])
    states, path = A.watchdog(tmp_path)
    assert path is None, "a healthy check must record nothing"
    assert not [c for c in calls if "osascript" in c[0]], "and must not notify"

    monkeypatch.setattr(
        A, "agent_states",
        lambda: [_state(disposition="enabled, disallowed, notified")],
    )
    states, path = A.watchdog(tmp_path)
    assert path is not None and path.exists()
    notifications = [c for c in calls if "osascript" in c[0]]
    assert len(notifications) == 1
    assert "display notification" in notifications[0][-1]
    assert "com.lexiconlocal.daily".rsplit(".", 1)[-1] in notifications[0][-1]


# ---------------------------------------------------------------------------
# MCP registration (D6 / amendment A3)
# ---------------------------------------------------------------------------

def test_json_client_registered(tmp_path: Path):
    cmd = tmp_path / "lexicon-mcp"
    cmd.write_text("#!/bin/sh\n")
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"lexicon": {"command": str(cmd)}}}))
    r = R._json_client("X", cfg)
    assert r.registered and r.command_ok and r.ok


def test_json_client_missing_entry_is_a_failure(tmp_path: Path):
    """Exactly Claude Desktop's state: a config with other servers but not ours."""
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"apple-dev": {"command": "/bin/true"}}}))
    r = R._json_client("Claude Desktop", cfg)
    assert not r.registered and not r.ok
    assert "NOT registered" in r.detail()


def test_registered_but_command_gone_is_a_failure(tmp_path: Path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"lexicon": {"command": "/no/such/bin"}}}))
    r = R._json_client("X", cfg)
    assert r.registered and not r.ok
    assert "command missing" in r.detail()


def test_absent_client_config_is_not_a_failure(tmp_path: Path):
    r = R._json_client("X", tmp_path / "absent.json")
    assert r.ok and not r.config_exists


def test_codex_toml_is_read_without_a_toml_parser(tmp_path: Path):
    cmd = tmp_path / "lexicon-mcp"
    cmd.write_text("#!/bin/sh\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(f"""\
        [projects."/some/path"]
        trust_level = "trusted"

        [mcp_servers.lexicon]
        command = "{cmd}"

        [mcp_servers.other]
        command = "/bin/true"
        """))
    r = R._codex(cfg)
    assert r.registered and r.command == str(cmd) and r.ok


def test_codex_without_the_table_is_a_failure(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.other]\ncommand = "/bin/true"\n')
    assert not R._codex(cfg).registered


def test_codex_survives_unrelated_syntax_errors(tmp_path: Path):
    """A broken line elsewhere must not turn into 'not registered'."""
    cmd = tmp_path / "lexicon-mcp"
    cmd.write_text("#!/bin/sh\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'this is = = not valid toml\n\n[mcp_servers.lexicon]\ncommand = "{cmd}"\n')
    assert R._codex(cfg).ok


# ---------------------------------------------------------------------------
# Capture freshness (D2 item 5)
# ---------------------------------------------------------------------------

def test_capture_freshness_flags_a_stalled_source(lexicon_tree: Path, tmp_path: Path):
    from lexiconlocal.config import load_config
    from lexiconlocal.report import CAPTURE_LAG_ALARM_HOURS, capture_freshness

    cfg = load_config(lexicon_tree / "config.yaml")
    live = tmp_path / "live"
    arch = cfg.lexicon_root / "archive" / "probe"
    for d in (live, arch):
        d.mkdir(parents=True, exist_ok=True)
    (arch / "old.jsonl").write_text("{}")
    (live / "new.jsonl").write_text("{}")
    now = os.path.getmtime(live / "new.jsonl")
    os.utime(arch / "old.jsonl", (now - 3600 * 48, now - 3600 * 48))

    [f] = capture_freshness(cfg, [("probe", str(live), "archive/probe")])
    assert f["state"] == "stalled"
    assert f["lag_hours"] > CAPTURE_LAG_ALARM_HOURS


def test_capture_freshness_tolerates_a_normal_daily_cycle(lexicon_tree: Path, tmp_path: Path):
    """The job runs at 03:30, so a day of accumulation is not an alarm."""
    from lexiconlocal.config import load_config
    from lexiconlocal.report import capture_freshness

    cfg = load_config(lexicon_tree / "config.yaml")
    live = tmp_path / "live"
    arch = cfg.lexicon_root / "archive" / "probe"
    for d in (live, arch):
        d.mkdir(parents=True, exist_ok=True)
    (arch / "old.jsonl").write_text("{}")
    (live / "new.jsonl").write_text("{}")
    now = os.path.getmtime(live / "new.jsonl")
    os.utime(arch / "old.jsonl", (now - 3600 * 20, now - 3600 * 20))

    [f] = capture_freshness(cfg, [("probe", str(live), "archive/probe")])
    assert f["state"] == "ok"


def test_capture_freshness_reports_a_never_archived_source(lexicon_tree: Path, tmp_path: Path):
    from lexiconlocal.config import load_config
    from lexiconlocal.report import capture_freshness

    cfg = load_config(lexicon_tree / "config.yaml")
    live = tmp_path / "live"
    live.mkdir()
    (live / "new.jsonl").write_text("{}")
    [f] = capture_freshness(cfg, [("probe", str(live), "archive/never")])
    assert f["state"] == "NEVER ARCHIVED"


# ---------------------------------------------------------------------------
# D3: what the MCP surface tells an agent
# ---------------------------------------------------------------------------

def _mcp_search():
    from lexiconlocal import mcp_server as M
    fn = M.lexicon_search
    return getattr(fn, "fn", getattr(fn, "func", fn))


class _FakeResult:
    def __init__(self, confidence, matched_by):
        self.path = "/x/y.md"
        self.project = "P"
        self.source_type = "repo-doc"
        self.doc_date = "2026-08-01"
        self.title = "T"
        self.score = 0.02
        self.chunk_ord = 0
        self.chunk_kind = "prose"
        self.excerpt = "body"
        self.chunk_id = 1
        self.confidence = confidence
        self.matched_by = matched_by


class _FakeSearcher:
    def __init__(self, results):
        self._results = results

    def search(self, *a, **kw):
        return self._results


def _render(monkeypatch, confidences):
    from lexiconlocal import mcp_server as M

    results = [_FakeResult(c, ["fts", "vector"]) for c in confidences]
    monkeypatch.setattr(M, "_get_searcher", lambda: (_FakeSearcher(results), None))
    return _mcp_search()("q", limit=len(results))


def test_mcp_renders_confidence_and_matched_by_on_every_line(monkeypatch):
    """Phase 4 shipped these to the CLI and the web API but not to agents."""
    out = _render(monkeypatch, [0.9, 0.85, 0.8])
    assert out.count("confidence=") == 3
    assert out.count("matched_by=fts+vector") == 3


def test_mcp_states_absence_in_words_not_only_in_a_number(monkeypatch):
    from lexiconlocal.mcp_server import ABSENT_BANNER

    out = _render(monkeypatch, [1.00, 0.32, 0.32, 0.36, 0.32])
    assert ABSENT_BANNER in out, "a top hit of 1.00 must not suppress the banner"

    out = _render(monkeypatch, [0.97, 1.00, 1.00, 0.99, 0.98])
    assert ABSENT_BANNER not in out


def test_mcp_reminder_survives_alongside_the_banner(monkeypatch):
    from lexiconlocal.mcp_server import REMINDER

    assert REMINDER in _render(monkeypatch, [0.3, 0.3, 0.3])
    assert REMINDER in _render(monkeypatch, [0.9, 0.9, 0.9])


def test_tool_description_teaches_the_scale():
    """An agent cannot act on a number it has not been told how to read."""
    from lexiconlocal import mcp_server as M

    desc = getattr(M.lexicon_search, "description", "") or ""
    if not desc:  # decorator shape differs across mcp versions
        import inspect
        desc = inspect.getsource(M)[:8000]
    for phrase in ("confidence", "matched_by", "ordinal"):
        assert phrase in desc


def test_registration_write_preserves_everything_else(tmp_path: Path, monkeypatch):
    """The Desktop config also holds 9 KB of live UI state. Do not eat it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "register_mcp", Path(__file__).parent.parent / "scripts" / "register_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cmd = tmp_path / "lexicon-mcp"
    cmd.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mod, "COMMAND", cmd)

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"apple-dev": {"command": "/bin/true"}},
        "preferences": {"sidebarMode": "epitaxy"},
    }))

    assert "registered" in mod.write_entry(cfg)
    data = json.loads(cfg.read_text())
    assert set(data["mcpServers"]) == {"apple-dev", "lexicon"}
    assert data["preferences"] == {"sidebarMode": "epitaxy"}
    assert data["mcpServers"]["lexicon"]["command"] == str(cmd)

    assert mod.write_entry(cfg) == "already registered", "must be idempotent"
    assert len(list(tmp_path.glob("*.bak-*"))) == 1, "and must not pile up backups"


# ---------------------------------------------------------------------------
# D5: the distillation backlog
# ---------------------------------------------------------------------------

@pytest.fixture
def indexed_tree(lexicon_tree, claude_code_archive, codex_archive):
    from lexiconlocal.config import load_config
    from lexiconlocal.indexer import Indexer
    from tests.test_pipeline import FakeEmbedder

    cfg = load_config(lexicon_tree / "config.yaml")
    Indexer(cfg, FakeEmbedder(), verbose=False).run(full=True, batch_size=8)
    return cfg


def test_backlog_excludes_projects_that_already_have_notes(indexed_tree):
    """A project with curated notes is not a backlog item."""
    from lexiconlocal.distill import distillation_backlog
    from lexiconlocal.web import notes as web_notes

    cfg = indexed_tree
    have = {p.name.lower() for p in web_notes.project_dirs(cfg.lexicon_root)}
    assert have, "fixture must have at least one distilled project"
    listed = {e.project.lower() for e in distillation_backlog(cfg)}
    assert not (listed & have)


def test_backlog_folds_a_declared_rename(indexed_tree):
    """A project indexed under a historical name is not undistilled.

    Without this, an old name would sit in the backlog forever while the
    current name's notes sat right there -- wrong in the one way that makes
    people stop reading a backlog.
    """
    from lexiconlocal import distill
    from lexiconlocal.config import load_config

    cfg_path = indexed_tree.lexicon_root / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + textwrap.dedent("""\
        historical_aliases:
          Bellows: forge
        """))
    have = distill.distilled_projects(load_config(cfg_path))
    assert "forge" in have          # its own notes directory
    assert "bellows" in have        # declared to be the same project, renamed


def test_backlog_does_not_fold_lineage_into_identity(indexed_tree):
    """Related work is not the same work, and must keep its own backlog row.

    `INDEX.md` alias resolution carries lineage as well as renames: a family
    row names predecessors, spikes and sub-missions in its prose because they
    belong to the same story, not because they are the same project. Folding
    those into "already distilled" let one distilled sibling swallow its whole
    family, silently -- the projects never appeared at all, which reads exactly
    like having nothing to say about them.
    """
    from lexiconlocal import distill

    cfg = indexed_tree
    cfg.index_md.write_text(textwrap.dedent("""\
        # Lexicon INDEX

        ## Project families (alias groups)

        | Family | Members | Notes |
        |---|---|---|
        | **Workshop line** | `Forge`, `Anvil` | Rebuilt from `Bellows`; the spike `Tongs` sits between them |

        ## Active projects

        | Project | One-liner | Repo path | Last activity | Aliases |
        |---|---|---|---|---|
        | Forge | Workshop tooling | `~/programming/Forge` | 2026-08-17 | Smithy |
        """), encoding="utf-8")

    have = distill.distilled_projects(cfg)
    assert "forge" in have
    for related in ("anvil", "bellows", "tongs", "smithy"):
        assert related not in have, f"{related} is related work, not the same work"


def test_alias_suppression_is_reported(indexed_tree):
    """What the backlog leaves out must be visible, or it cannot be corrected."""
    from lexiconlocal import distill
    from lexiconlocal.config import load_config

    cfg_path = indexed_tree.lexicon_root / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + textwrap.dedent("""\
        historical_aliases:
          Lighthouse: forge
        """))
    cfg = load_config(cfg_path)

    listed = {e.project.lower() for e in distill.distillation_backlog(cfg)}
    assert "lighthouse" not in listed, "suppressed, as declared"

    suppressed = distill.alias_suppressions(cfg)
    entry = next(s for s in suppressed if s.project.lower() == "lighthouse")
    assert entry.distilled_as == "forge"
    assert entry.documents > 0, "a suppressed project still has material to report"


def test_backlog_ranks_recent_work_above_dormant_bulk():
    """Volume alone would rank dead giants above live work."""
    from lexiconlocal.distill import RECENCY_HALF_LIFE_DAYS

    def score(docs, days):
        return docs * (0.5 ** (days / RECENCY_HALF_LIFE_DAYS))

    assert score(300, 3) > score(900, 365)
    assert score(900, 3) > score(300, 3)


def test_distill_prompt_fills_in_real_aliases(indexed_tree):
    from lexiconlocal.distill import distill_prompt

    text = distill_prompt(indexed_tree, "Lighthouse")
    assert "projects/Lighthouse/overview.md" in text
    # The alias index stores alternates lowercased, so compare that way.
    assert "aliases:" in text and "beacon" in text.lower()
    assert "aliases: Lighthouse" not in text, "the project must not alias itself"


def test_distill_prompt_omits_the_alias_clause_when_there_are_none(indexed_tree):
    from lexiconlocal.distill import distill_prompt

    text = distill_prompt(indexed_tree, "NoSuchProjectAnywhere")
    assert "aliases:" not in text


# ---------------------------------------------------------------------------
# lexicon init
# ---------------------------------------------------------------------------

def test_init_creates_a_root_that_the_loader_accepts(tmp_path: Path):
    from lexiconlocal.config import load_config
    from lexiconlocal.init import SUBDIRS, init_root

    root = tmp_path / "Lex"
    lines = init_root(root, repo=tmp_path / "repo", git=False)
    assert lines and "Created" in lines[0]
    for sub in SUBDIRS:
        assert (root / sub).is_dir()
    cfg = load_config(root / "config.yaml")
    assert cfg.lexicon_root == root.resolve()
    assert cfg.source_roots == [], "no source root is configured by default"
    assert cfg.historical_aliases == {}
    assert (root / "private").resolve() in [p.resolve() for p in cfg.never_index]


def test_init_never_overwrites(tmp_path: Path):
    from lexiconlocal.init import init_root

    root = tmp_path / "Lex"
    root.mkdir()
    (root / "precious.md").write_text("do not lose me")
    with pytest.raises(FileExistsError):
        init_root(root, repo=tmp_path, git=False)
    assert (root / "precious.md").read_text() == "do not lose me"


def test_init_index_template_parses_as_empty(tmp_path: Path):
    """The INDEX.md template must be something the alias parser accepts."""
    from lexiconlocal.init import init_root
    from lexiconlocal.projects import load_project_index

    root = tmp_path / "Lex"
    init_root(root, repo=tmp_path, git=False)
    idx = load_project_index(root / "INDEX.md", {})
    assert idx.known == set()
    assert idx.resolve("Anything") == ["Anything"]
