"""Automation primitives: the index lock, preflight, and drop-point rules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lexiconlocal.lock import IndexLock, _pid_alive


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------

def test_lock_is_acquired_and_released(tmp_path: Path):
    lock = IndexLock(tmp_path)
    res = lock.acquire()
    assert res.acquired
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()


def test_second_holder_is_refused_while_the_first_lives(tmp_path: Path):
    a, b = IndexLock(tmp_path), IndexLock(tmp_path)
    assert a.acquire().acquired
    res = b.acquire()
    assert not res.acquired
    assert res.holder_pid == os.getpid()
    assert "another index run is in progress" in res.message
    a.release()
    assert b.acquire().acquired
    b.release()


def test_stale_lock_from_a_dead_process_is_broken(tmp_path: Path):
    """A crashed nightly job must not wedge every later run."""
    lock_path = tmp_path / "lexicon-index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A pid that is essentially certain not to exist.
    dead = 999_999
    assert not _pid_alive(dead)
    lock_path.write_text(str(dead), encoding="utf-8")

    res = IndexLock(tmp_path).acquire()
    assert res.acquired
    assert res.broke_stale
    assert "stale" in res.message


def test_unreadable_lock_content_is_treated_as_stale(tmp_path: Path):
    (tmp_path / "lexicon-index.lock").write_text("not-a-pid", encoding="utf-8")
    res = IndexLock(tmp_path).acquire()
    assert res.acquired and res.broke_stale


def test_release_does_not_delete_someone_elses_lock(tmp_path: Path):
    """Breaking a stale lock elsewhere must not let us delete a live holder."""
    a = IndexLock(tmp_path)
    assert a.acquire().acquired
    # Simulate another process taking over the lock file.
    a.path.write_text("12345", encoding="utf-8")
    a.release()
    assert a.path.exists(), "must not remove a lock we no longer own"


def test_context_manager_releases(tmp_path: Path):
    lock = IndexLock(tmp_path)
    with lock as res:
        assert res.acquired
        assert lock.path.exists()
    assert not lock.path.exists()


def test_blocked_index_command_exits_zero(tmp_path: Path, lexicon_tree):
    """A hook firing during the nightly job is a skip, not a failure.

    Exercised through the real CLI so the exit code is the one a shell sees.
    """
    from lexiconlocal.config import load_config
    cfg = load_config(lexicon_tree / "config.yaml")
    holder = IndexLock(cfg.index_dir)
    assert holder.acquire().acquired
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "lexiconlocal.cli", "index",
             "--config", str(lexicon_tree / "config.yaml"), "--no-embed"],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "another index run is in progress" in proc.stdout
    finally:
        holder.release()


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def test_cloud_model_never_satisfies_the_model_check():
    """Phase 2 found a :cloud model as the only one present.

    Accepting it would silently send the corpus off the machine.
    """
    from lexiconlocal.preflight import check_model
    tags = {"models": [{"name": "minimax-m3:cloud"}, {"name": "some-other:cloud"}]}
    check = check_model(tags, "nomic-embed-text")
    assert not check.ok
    assert "must never be used" in check.detail
    assert "ollama pull nomic-embed-text" in check.detail


def test_local_model_satisfies_the_check_regardless_of_tag():
    from lexiconlocal.preflight import check_model
    for name in ("nomic-embed-text", "nomic-embed-text:latest", "nomic-embed-text:v1.5"):
        assert check_model({"models": [{"name": name}]}, "nomic-embed-text").ok


def test_model_check_reports_unreachable_rather_than_guessing():
    from lexiconlocal.preflight import check_model
    check = check_model(None, "nomic-embed-text")
    assert not check.ok and "unreachable" in check.detail


@pytest.mark.parametrize("app_present", [True, False])
def test_preflight_never_pulls_a_model(monkeypatch, tmp_path, app_present):
    """Preflight may start Ollama, but pulling is a human decision.

    Checked on both start paths: launching Ollama.app (preferred) and the bare
    `ollama serve` fallback for a machine without it.
    """
    import lexiconlocal.preflight as pf
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append(list(cmd))

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    app = tmp_path / "Ollama.app"
    if app_present:
        app.mkdir()
    monkeypatch.setattr(pf, "OLLAMA_APP", app)
    monkeypatch.setattr(pf, "FALLBACK_LOG", tmp_path / "logs" / "ollama.log")
    monkeypatch.setattr(pf.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    monkeypatch.setattr(pf.shutil, "which", lambda _: "/usr/local/bin/ollama")
    monkeypatch.setattr(pf, "_tags", lambda *a, **k: None)
    monkeypatch.setattr(pf, "OLLAMA_START_TIMEOUT", 0.05)
    pf.check_ollama("http://localhost:11434", autostart=True)
    assert calls, "should have attempted to start ollama"
    for cmd in calls:
        assert "pull" not in cmd, f"preflight must never pull: {cmd}"
    if app_present:
        assert calls[0][:2] == ["/usr/bin/open", "-ga"], "the app is the preferred supervisor"
    else:
        assert calls[0][1] == "serve"


def test_preflight_reports_missing_lexicon_repo(tmp_path, lexicon_tree):
    from lexiconlocal.config import load_config
    from lexiconlocal.preflight import check_lexicon_repo
    cfg = load_config(lexicon_tree / "config.yaml")
    check = check_lexicon_repo(cfg)
    assert not check.ok and "not a git repo" in check.detail


def test_preflight_database_check_passes_before_first_index(lexicon_tree):
    from lexiconlocal.config import load_config
    from lexiconlocal.preflight import check_database
    cfg = load_config(lexicon_tree / "config.yaml")
    check = check_database(cfg)
    assert check.ok and "does not exist yet" in check.detail


# --------------------------------------------------------------------------
# Export drop point
# --------------------------------------------------------------------------

def test_claude_batch_directory_is_recognised(tmp_path):
    from lexiconlocal.dropbox import classify_drop
    d = tmp_path / "drop" / "data-2caa5c2e-72bb-4207-9fbd-137ea3601d37-1787088677-956666f8-batch-0000"
    d.mkdir(parents=True)
    e = classify_drop(d, tmp_path / "Lexicon")
    assert e.kind == "claude-batch"
    assert e.destination == tmp_path / "Lexicon" / "archive" / "claude" / d.name


def test_renamed_claude_export_is_still_recognised(tmp_path):
    """A hand-extracted or renamed dump must not be lost."""
    from lexiconlocal.dropbox import classify_drop
    d = tmp_path / "drop" / "my-claude-stuff"
    d.mkdir(parents=True)
    (d / "conversations.json").write_text("[]", encoding="utf-8")
    assert classify_drop(d, tmp_path / "Lexicon").kind == "claude-batch"


def test_chatgpt_extracted_directory_is_recognised(tmp_path):
    """The real export arrived extracted, not zipped, with an opaque name.

    Its only recognisable feature is the conversations-NNN.json shards inside,
    so classification has to look at contents rather than at the name.
    """
    from lexiconlocal.dropbox import classify_drop

    root = tmp_path / "Lexicon"
    d = tmp_path / "drop" / "a75d2a5e3887-2026-08-18-20-22-53-6576842365"
    d.mkdir(parents=True)
    (d / "conversations-000.json").write_text("[]", encoding="utf-8")
    (d / "conversations-001.json").write_text("[]", encoding="utf-8")
    e = classify_drop(d, root)
    assert e.kind == "chatgpt-dir"
    assert e.destination == root / "archive" / "chatgpt" / d.name


def test_every_drop_kind_has_a_daily_script_handler():
    """A kind with no `case` branch would leave an export sitting there forever."""
    import re

    script = (Path(__file__).resolve().parents[1] / "scripts" / "lexicon_daily.sh").read_text()
    handled = set(re.findall(r"^\s{8}([a-z-]+)\)$", script, re.MULTILINE))
    assert {"claude-batch", "chatgpt-zip", "chatgpt-dir", "unrecognised"} <= handled
    assert "*)" in script, "no catch-all: an unknown kind would be silently dropped"


def test_chatgpt_zip_is_recognised_and_dated(tmp_path):
    from datetime import date
    from lexiconlocal.dropbox import classify_drop
    z = tmp_path / "drop" / "chatgpt-export-2026-08-18.zip"
    z.parent.mkdir(parents=True)
    z.write_bytes(b"PK\x03\x04")
    e = classify_drop(z, tmp_path / "Lexicon", today=date(2026, 8, 18))
    assert e.kind == "chatgpt-zip"
    assert e.destination.name == "2026-08-18"


def test_unrecognised_drop_is_flagged_not_silently_ignored(tmp_path):
    from lexiconlocal.dropbox import classify_drop
    f = tmp_path / "drop" / "random-notes.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello", encoding="utf-8")
    e = classify_drop(f, tmp_path / "Lexicon")
    assert e.kind == "unrecognised"
    assert e.destination is None


def test_scan_ignores_dotfiles_but_reports_everything_else(tmp_path):
    from lexiconlocal.dropbox import scan_drop_point
    drop = tmp_path / "drop"; drop.mkdir()
    (drop / ".DS_Store").write_bytes(b"junk")
    (drop / ".hidden").write_text("x", encoding="utf-8")
    (drop / "mystery.bin").write_bytes(b"x")
    kinds = [e.kind for e in scan_drop_point(drop, tmp_path / "Lexicon")]
    assert kinds == ["unrecognised"]


# --------------------------------------------------------------------------
# Export freshness
# --------------------------------------------------------------------------

def test_freshness_distinguishes_never_arrived_from_stale(tmp_path):
    from datetime import date
    from lexiconlocal.dropbox import export_freshness
    root = tmp_path / "Lexicon"
    (root / "archive" / "chatgpt").mkdir(parents=True)
    (root / "archive" / "claude" / "export-2026-08-01").mkdir(parents=True)
    got = dict(export_freshness(root, today=date(2026, 8, 18)))
    assert got["chatgpt"] is None, "never arrived must not read as 0 days old"
    assert got["claude"] == 17


def test_stale_sources_wording(tmp_path):
    from datetime import date
    from lexiconlocal.dropbox import stale_sources
    root = tmp_path / "Lexicon"
    (root / "archive" / "chatgpt").mkdir(parents=True)
    (root / "archive" / "claude" / "export-2026-01-01").mkdir(parents=True)
    msgs = stale_sources(root, today=date(2026, 8, 18))
    assert any("no export has ever arrived" in m for m in msgs)
    assert any("days old" in m for m in msgs)


def test_fresh_export_produces_no_warning(tmp_path):
    from datetime import date
    from lexiconlocal.dropbox import stale_sources
    root = tmp_path / "Lexicon"
    for s in ("chatgpt", "claude"):
        (root / "archive" / s / "export-2026-08-15").mkdir(parents=True)
    assert stale_sources(root, today=date(2026, 8, 18)) == []


def test_claude_batch_timestamp_is_parsed_for_freshness(tmp_path):
    """Claude batch dirs carry a unix timestamp, not an ISO date."""
    from datetime import date, datetime
    from lexiconlocal.dropbox import newest_export_date
    root = tmp_path / "Lexicon" / "archive" / "claude"
    root.mkdir(parents=True)
    ts = int(datetime(2026, 8, 18, 12, 0).timestamp())
    (root / f"data-abc-{ts}-hash-batch-0000").mkdir()
    assert newest_export_date(root) == date(2026, 8, 18)


# --------------------------------------------------------------------------
# Content hashing (memories re-snapshot trigger)
# --------------------------------------------------------------------------

def test_dir_hash_is_stable_and_change_sensitive(tmp_path):
    from lexiconlocal.dropbox import dir_content_hash
    d = tmp_path / "mem"; (d / "sub").mkdir(parents=True)
    (d / "a.md").write_text("alpha", encoding="utf-8")
    (d / "sub" / "b.md").write_text("beta", encoding="utf-8")
    h1 = dir_content_hash(d)
    assert h1 and dir_content_hash(d) == h1, "hash must be stable"
    (d / "sub" / "b.md").write_text("beta changed", encoding="utf-8")
    assert dir_content_hash(d) != h1, "content change must change the hash"


def test_dir_hash_ignores_ds_store_and_git(tmp_path):
    from lexiconlocal.dropbox import dir_content_hash
    d = tmp_path / "mem"; d.mkdir()
    (d / "a.md").write_text("alpha", encoding="utf-8")
    h1 = dir_content_hash(d)
    (d / ".DS_Store").write_bytes(b"finder junk")
    (d / ".git").mkdir(); (d / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    assert dir_content_hash(d) == h1, "noise must not trigger a re-snapshot"


# ---------------------------------------------------------------------------
# preflight: the embedder is proved by embedding, and a wedged server is healed
# ---------------------------------------------------------------------------

def test_preflight_embedding_check_fails_on_a_500(monkeypatch):
    """Reachable is not healthy.

    Ollama answered /api/tags normally for 20 hours while every /api/embed
    returned 500. A check that only pings cannot see that.
    """
    from lexiconlocal import preflight as pf

    class R:
        status_code = 500
        text = 'llama-server has terminated: Unable to reach MTLCompilerService'

    class _Client:
        """Stands in for the shared Ollama client, which is now the only seam."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            return R()

    monkeypatch.setattr(pf, "ollama_client", lambda *a, **k: _Client())
    c = pf.check_embedding()
    assert not c.ok
    assert "500" in c.detail
    assert "MTLCompilerService" in c.detail, "the server's own error must reach the operator"


def test_preflight_restarts_a_wedged_server_and_reports_it(monkeypatch, lexicon_tree):
    """Restart-on-crash cannot help here: the process never died."""
    from lexiconlocal import preflight as pf
    from lexiconlocal.config import load_config

    calls = {"restarts": 0, "probes": 0}

    def fake_probe(model=pf.DEFAULT_MODEL, host=pf.DEFAULT_HOST):
        calls["probes"] += 1
        ok = calls["restarts"] > 0
        return pf.Check("embedding", ok, "probe ok" if ok else "/api/embed returned 500: wedged")

    def fake_restart(host):
        calls["restarts"] += 1
        return True, "relaunched Ollama.app"

    monkeypatch.setattr(pf, "check_ollama",
                        lambda host, autostart=True: (pf.Check("ollama", True, "reachable"), {}))
    monkeypatch.setattr(pf, "check_model", lambda tags, model: pf.Check("embed model", True, "present"))
    monkeypatch.setattr(pf, "check_embedding", fake_probe)
    monkeypatch.setattr(pf, "_restart_ollama", fake_restart)

    cfg = load_config(lexicon_tree / "config.yaml")
    checks, _ = pf.run_preflight(cfg, autostart=True)
    embed = next(c for c in checks if c.name == "embedding")
    assert embed.ok
    assert calls["restarts"] == 1 and calls["probes"] == 2
    assert "wedged" in embed.detail and "restarted" in embed.detail


def test_preflight_never_discards_a_fallback_servers_output():
    """DEVNULL is how the outage stayed invisible: the error existed nowhere."""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "src/lexiconlocal/preflight.py").read_text()
    start = src[src.index("def _start_ollama("):src.index("def _restart_ollama(")]
    assert "FALLBACK_LOG" in start, "a bare server must log to a file"
    assert "OLLAMA_APP" in start, "the vendor app must be preferred over a bare server"
