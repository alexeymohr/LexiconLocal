"""Preflight checks for unattended runs.

A nightly job has nobody watching it, so the failure that matters is the silent
one. Phase 2 found Ollama installed but **not running**, with only
``minimax-m3:cloud`` present locally -- exactly the state in which a careless
implementation would either do nothing or, far worse, quietly embed the corpus
through a cloud model.

Hence two rules this module enforces:

* Ollama may be **started** automatically, but a model is never **pulled**.
  Pulling is a network fetch of an artifact nobody reviewed; it needs a human.
* A ``:cloud`` model is never an acceptable substitute for the local one. If
  ``nomic-embed-text`` is absent, exit 2 with the exact command to fix it.
* Reachability is not health. The embedder is proved by embedding, not by a
  ping -- see ``check_embedding``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .agents import agent_states
from .config import Config
from .embed import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    EmbedTargetRefused,
    require_local_host,
    require_local_model,
)
from .registration import registrations

OLLAMA_START_TIMEOUT = 20.0
OLLAMA_POLL_INTERVAL = 0.5


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"  [{'OK ' if self.ok else 'FAIL'}] {self.name:<22} {self.detail}"


def _tags(host: str, timeout: float = 5.0) -> dict | None:
    try:
        r = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001 - unreachable is the answer, not an error
        return None


#: The vendor's supervisor. Launching the app gets a serve process that is
#: restarted when it dies, logged to ~/.ollama/logs/server.log, and visible in
#: the menu bar. A bare `ollama serve` gets none of that.
OLLAMA_APP = Path("/Applications/Ollama.app")

#: Where a fallback bare server's output goes. Never DEVNULL: a headless server
#: whose output was discarded is exactly how a wedged embedder went unnoticed
#: for 20 hours on 2026-08-19.
FALLBACK_LOG = Path("~/Lexicon/index/logs/ollama-fallback.log").expanduser()


def _wait_ready(host: str, timeout: float | None = None) -> float | None:
    """Poll until the server answers. Returns seconds waited, or None.

    ``timeout`` is resolved at call time rather than bound as a default, so
    OLLAMA_START_TIMEOUT stays overridable — a default argument would freeze
    the module constant at import.
    """
    timeout = OLLAMA_START_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _tags(host) is not None:
            return timeout - (deadline - time.time())
        time.sleep(OLLAMA_POLL_INTERVAL)
    return None


def _start_ollama(host: str) -> tuple[bool, str]:
    """Try to bring Ollama up, preferring its own app to a bare server.

    Launching Ollama.app hands supervision, restart-on-crash, logging and
    visibility to the vendor. The bare `ollama serve` fallback exists only for
    a machine without the app, and even then its output is written to a file:
    the previous version sent it to DEVNULL, so when that server later answered
    /api/tags while failing every embed, the error existed nowhere on disk.
    """
    if OLLAMA_APP.is_dir():
        try:
            subprocess.run(
                ["/usr/bin/open", "-ga", "Ollama"],
                check=True, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not launch {OLLAMA_APP.name}: {e}"
        waited = _wait_ready(host)
        if waited is not None:
            return True, f"was down; launched {OLLAMA_APP.name} (ready in {waited:.1f}s)"
        return False, (
            f"launched {OLLAMA_APP.name} but it was not serving within "
            f"{OLLAMA_START_TIMEOUT:.0f}s — check ~/.ollama/logs/server.log"
        )

    exe = shutil.which("ollama")
    if not exe:
        return False, "ollama not installed and not on PATH — install it, then rerun"
    try:
        FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = FALLBACK_LOG.open("a", encoding="utf-8")
    except OSError:
        log = None
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=log or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return False, f"could not start ollama: {e}"
    waited = _wait_ready(host)
    if waited is not None:
        where = f", logging to {FALLBACK_LOG}" if log else ""
        return True, f"was down; started a bare `ollama serve` (ready in {waited:.1f}s){where}"
    return False, f"started ollama but it was not ready within {OLLAMA_START_TIMEOUT:.0f}s"


def _restart_ollama(host: str) -> tuple[bool, str]:
    """Stop whatever is serving and bring it back through the normal path.

    The failure this exists for is a server that is **alive and wrong**: on
    2026-08-19 one answered /api/tags for 20 hours while returning 500 on every
    embed, because its llama-server could not reach macOS's Metal compiler
    service. Restart-on-crash would never have fired — the process never died.
    """
    try:
        subprocess.run(["/usr/bin/pkill", "-f", "ollama serve"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not stop the running server: {e}"
    deadline = time.time() + 10
    while time.time() < deadline and _tags(host, timeout=1.0) is not None:
        time.sleep(OLLAMA_POLL_INTERVAL)
    return _start_ollama(host)


def check_ollama(host: str = DEFAULT_HOST, *, autostart: bool = True) -> tuple[Check, dict | None]:
    # Refuse a non-local target before the first request, not after it: this is
    # the same gate `Embedder` applies, so preflight cannot bless a host that
    # ordinary indexing would reject.
    try:
        host = require_local_host(host)
    except EmbedTargetRefused as e:
        return Check("ollama", False, str(e)), None
    tags = _tags(host)
    if tags is not None:
        return Check("ollama", True, f"reachable at {host}"), tags
    if not autostart:
        return Check("ollama", False, f"not reachable at {host}"), None
    started, detail = _start_ollama(host)
    if not started:
        return Check("ollama", False, detail), None
    return Check("ollama", True, detail), _tags(host)


def check_model(tags: dict | None, model: str = DEFAULT_MODEL) -> Check:
    if tags is None:
        return Check("embed model", False, "cannot check — Ollama unreachable")
    names = [m.get("name", "") for m in tags.get("models", [])]
    base = model.split(":")[0]
    local = [n for n in names if not n.endswith(":cloud")]
    if any(n.split(":")[0] == base for n in local):
        return Check("embed model", True, f"{model} present locally")
    cloud_only = [n for n in names if n.endswith(":cloud")]
    extra = (
        f" (only cloud models present: {', '.join(cloud_only)} — these must never be used"
        f" for the Lexicon)" if cloud_only else ""
    )
    return Check(
        "embed model", False,
        f"{model} is NOT available locally{extra}. Fix with:  ollama pull {model}",
    )


def check_embedding(model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> Check:
    """Actually embed a probe string.

    Reachability and model presence are not the same thing as a working
    embedder. On 2026-08-19 Ollama answered ``/api/tags`` normally while every
    call to ``/api/embed`` returned 500 -- its llama-server could not reach
    macOS's Metal compiler service. Preflight passed, the daily job ran, and
    2,793 prose chunks silently piled up unembedded. The only check that would
    have caught it is the one that does the actual work.
    """
    try:
        host = require_local_host(host)
        require_local_model(model)
    except EmbedTargetRefused as e:
        return Check("embedding", False, str(e))
    try:
        r = httpx.post(
            f"{host.rstrip('/')}/api/embed",
            json={"model": model, "input": ["preflight probe"]},
            timeout=60.0,
        )
    except Exception as e:  # noqa: BLE001
        return Check("embedding", False, f"{host}/api/embed unreachable: {e}")
    if r.status_code != 200:
        detail = r.text.strip().replace("\n", " ")[:300]
        return Check(
            "embedding", False,
            f"/api/embed returned {r.status_code}: {detail} — "
            f"restart the ollama server (`kill` it, then `ollama serve` from a "
            f"logged-in session) and rerun",
        )
    try:
        vec = r.json()["embeddings"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return Check("embedding", False, f"/api/embed returned an unusable body: {r.text[:200]}")
    if not vec:
        return Check("embedding", False, "/api/embed returned an empty vector")
    return Check("embedding", True, f"{model} embedded a probe ({len(vec)} dims)")


def check_database(cfg: Config) -> Check:
    db = cfg.db_path
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Check("database", False, f"cannot create {db.parent}: {e}")
    if not db.exists():
        return Check("database", True, f"{db} does not exist yet (will be created)")
    import sqlite3
    try:
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
    except sqlite3.Error as e:
        return Check("database", False, f"{db} not openable: {e}")
    probe = db.parent / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check("database", False, f"{db.parent} not writable: {e}")
    size = db.stat().st_size / 1048576
    return Check("database", True, f"{db.name} openable and writable ({size:,.0f} MB)")


def check_lexicon_repo(cfg: Config) -> Check:
    root = cfg.lexicon_root
    if not root.exists():
        return Check("lexicon repo", False, f"{root} does not exist")
    if not (root / ".git").exists():
        return Check("lexicon repo", False, f"{root} is not a git repo — history is the paper trail")
    return Check("lexicon repo", True, f"{root} present and git-initialised")


def check_launch_agents() -> Check:
    """Prove the unattended automation is registered *and* permitted to run.

    Two states, not one. ``launchctl bootstrap`` succeeds on an agent that
    macOS's Background Task Management has disallowed, and the service then
    shows up in ``launchctl list`` while still being removed again later -- so a
    check that only asked "is it loaded" would report green on the exact machine
    state that stopped capture on 2026-08-19. See ``agents.py`` for the log
    evidence.
    """
    states = agent_states()
    bad = [s for s in states if not s.ok]
    if not bad:
        unknown = [s for s in states if s.allowed is None]
        note = " — WARNING: could not read Background Task Management state" if unknown else ""
        return Check("launch agents", True, f"{len(states)} agents loaded and allowed{note}")
    # Three agents switched off together produce three identical remedies, which
    # buries the one sentence that matters. Group by problem instead.
    grouped: dict[str, list[str]] = {}
    for s in bad:
        grouped.setdefault(s.problem(), []).append(s.label)
    return Check(
        "launch agents", False,
        " | ".join(f"{', '.join(labels)}: {problem}" for problem, labels in grouped.items()),
    )


def check_mcp_registration() -> Check:
    """Prove every installed client can still reach the server.

    Registration is not write-once state. Claude Desktop rewrites its config
    file wholesale -- the same file stores its live UI preferences -- and the
    ``lexicon`` entry Phase 2 recorded as added was simply not there afterwards,
    with no log line anywhere to mark its going.
    """
    regs = registrations()
    bad = [r for r in regs if not r.ok]
    if not bad:
        present = [r for r in regs if r.config_exists]
        return Check("mcp registration", True,
                     f"registered in {len(present)} client(s): "
                     f"{', '.join(r.client for r in present)}")
    return Check("mcp registration", False, "; ".join(r.detail() for r in bad))


def run_preflight(
    cfg: Config,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    autostart: bool = True,
    check_automation: bool = True,
) -> tuple[list[Check], int]:
    ollama_check, tags = check_ollama(host, autostart=autostart)
    model_check = check_model(tags, model)
    checks = [ollama_check, model_check]
    if ollama_check.ok and model_check.ok:
        embed_check = check_embedding(model, host)
        if not embed_check.ok and autostart:
            # A server that is up but cannot embed is the failure mode that
            # actually happened, and the only cure for it is a restart. Try
            # once, then report whichever answer the second probe gives.
            restarted, detail = _restart_ollama(host)
            if restarted:
                retry = check_embedding(model, host)
                embed_check = Check(
                    retry.name, retry.ok,
                    f"{retry.detail}  [server was wedged; restarted it — {detail}]",
                )
            else:
                embed_check = Check(
                    embed_check.name, False,
                    f"{embed_check.detail}  [restart also failed: {detail}]",
                )
        checks.append(embed_check)
    else:
        checks.append(Check("embedding", False, "not probed — Ollama or model unavailable"))
    checks += [check_database(cfg), check_lexicon_repo(cfg)]
    if check_automation:
        checks += [check_launch_agents(), check_mcp_registration()]
    return checks, (0 if all(c.ok for c in checks) else 2)
