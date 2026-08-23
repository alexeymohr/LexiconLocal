"""Launch-agent supervision: is the unattended automation actually registered?

Phase 3 bootstrapped three LaunchAgents by hand. On 2026-08-19 all three were
found unloaded, and the daily capture job had silently stopped. The unified log
gave the answer::

    14:18:54  backgroundtaskmanagementd  getItemWithIdentifier: 8.com.lexiconlocal.golden
    14:18:54  launchd  [gui/501] removing service: com.lexiconlocal.golden
    14:18:55  launchd  [gui/501] removing service: com.lexiconlocal.daily
    14:19:20  launchd  [gui/501] removing service: com.lexiconlocal.export-reminder

Three removals seconds apart, each preceded by a Background Task Management
lookup: somebody switched them off in System Settings -> General -> Login Items
& Extensions. In the UI they appear as unsigned ``bash`` and ``osascript``
entries under "Unknown Developer", which is exactly what a careful person turns
off. ``sfltool dumpbtm`` still records them as ``disallowed``.

That is why this module checks **two** things, not one:

* **loaded** -- ``launchctl`` has the service registered in the user's GUI domain.
* **allowed** -- BTM has not disallowed it.

Checking only the first is not enough. ``launchctl bootstrap`` succeeds on a
disallowed agent and the service appears in ``launchctl list``, but macOS
removed it once and will remove it again; only the user can re-allow it, in
System Settings. A check that looked at registration alone would report green
on a machine that will be silent again by morning.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: label -> what stops happening when it is not running.
EXPECTED_AGENTS: dict[str, str] = {
    "com.lexiconlocal.daily": "nightly capture, index, report and safety-net commit",
    "com.lexiconlocal.golden": "weekly golden-query regression guard",
    "com.lexiconlocal.export-reminder": "account-export staleness reminder",
}

LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()

#: Append-only. A detection record is evidence about a moment that has passed;
#: rewriting it would defeat the point (see A2 in docs/PHASE_05.md).
DETECTIONS_NAME = "agent-detections.jsonl"

_BTM_RECORD = re.compile(r"^\s*#\d+:\s*$")
_BTM_FIELD = re.compile(r"^\s*([A-Za-z ]+):\s*(.*?)\s*$")


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


def loaded_labels() -> set[str]:
    """Labels ``launchctl`` currently has registered for this user."""
    out = _run(["launchctl", "list"])
    labels: set[str] = set()
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip():
            labels.add(parts[2].strip())
    return labels


def _btm_read_permitted() -> bool:
    """Whether running ``sfltool dumpbtm`` is acceptable right now.

    ``sfltool dumpbtm`` asks authd for admin rights, and in a GUI login
    session that raises the "sfltool wants to make changes" password dialog.
    Observed 2026-08-21: opening a Claude conversation fired the SessionEnd
    hook, the hook ran the watchdog, and macOS demanded a password out of
    nowhere -- twice, because of the empty-read retry below. An unattended
    caller must never summon that dialog, so the read is attempted only when
    a human plausibly ran the command on purpose (stdin or stdout is a TTY),
    or when ``LEXICON_BTM=force``. ``LEXICON_BTM=skip`` silences it even
    interactively.
    """
    mode = os.environ.get("LEXICON_BTM", "").strip().lower()
    if mode == "force":
        return True
    if mode == "skip":
        return False
    return sys.stdin.isatty() or sys.stdout.isatty()


def btm_dispositions() -> dict[str, str]:
    """Map label -> BTM disposition string, e.g. ``enabled, disallowed, notified``.

    ``sfltool dumpbtm`` prints ``Disposition`` before ``Identifier`` within each
    ``#n:`` record, and prefixes the identifier with a type number
    (``8.com.lexiconlocal.daily``), so records are parsed whole and matched on
    the suffix. An empty result means the tool was unavailable, which is not the
    same as "nothing is disallowed" -- callers must treat it as unknown.

    Unattended callers get the empty/unknown result without the tool running
    at all -- see :func:`_btm_read_permitted` for why.
    """
    if not _btm_read_permitted():
        return {}
    out = _run(["sfltool", "dumpbtm"], timeout=30.0)
    if not out:
        # One retry with a longer window. An empty read is indistinguishable
        # from "nothing is disallowed" to every caller, so a transient miss
        # silently converts a FAIL into a pass -- observed once on a loaded
        # machine. Better to wait than to answer wrongly.
        out = _run(["sfltool", "dumpbtm"], timeout=60.0)
    if not out:
        return {}
    found: dict[str, str] = {}
    fields: dict[str, str] = {}

    def flush() -> None:
        ident = fields.get("Identifier", "")
        disp = fields.get("Disposition", "")
        if ident and disp:
            label = ident.split(".", 1)[1] if re.match(r"^\d+\.", ident) else ident
            found[label] = disp.split("]")[0].lstrip("[").strip()

    for line in out.splitlines():
        if _BTM_RECORD.match(line):
            flush()
            fields = {}
            continue
        m = _BTM_FIELD.match(line)
        if m:
            fields[m.group(1).strip()] = m.group(2)
    flush()
    return found


@dataclass
class AgentState:
    label: str
    purpose: str
    plist: Path
    plist_exists: bool
    loaded: bool
    #: None when BTM could not be read at all (non-macOS, or sfltool missing).
    disposition: str | None = None

    @property
    def allowed(self) -> bool | None:
        if self.disposition is None:
            return None
        return "disallowed" not in self.disposition

    @property
    def ok(self) -> bool:
        return self.plist_exists and self.loaded and self.allowed is not False

    def problem(self) -> str:
        if not self.plist_exists:
            return f"plist missing at {self.plist}"
        if not self.loaded:
            return "not registered with launchd"
        if self.allowed is False:
            return (
                "disallowed in Background Task Management — macOS will remove it "
                "again; re-enable it in System Settings > General > "
                "Login Items & Extensions > Allow in the Background"
            )
        return ""


def agent_states(
    expected: dict[str, str] | None = None,
    *,
    agents_dir: Path | None = None,
) -> list[AgentState]:
    expected = EXPECTED_AGENTS if expected is None else expected
    agents_dir = LAUNCH_AGENTS_DIR if agents_dir is None else agents_dir
    live = loaded_labels()
    disps = btm_dispositions()
    states = []
    for label, purpose in expected.items():
        plist = agents_dir / f"{label}.plist"
        states.append(
            AgentState(
                label=label,
                purpose=purpose,
                plist=plist,
                plist_exists=plist.exists(),
                loaded=label in live,
                disposition=disps.get(label) if disps else None,
            )
        )
    return states


# ---------------------------------------------------------------------------
# Detection records (amendment A2)
# ---------------------------------------------------------------------------

def _boot_time() -> str:
    out = _run(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    if not m:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(m.group(1))))


def _uptime_seconds() -> int | None:
    out = _run(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return int(time.time()) - int(m.group(1)) if m else None


def detection_record(states: list[AgentState]) -> dict:
    """Everything needed to explain a failure that has already happened.

    The reason 2026-08-19's outage had no root cause for several hours is that
    nothing recorded the moment. Uptime distinguishes a reboot from a live
    removal; the GUI session manager distinguishes "ran headless" from "user was
    logged in"; the BTM disposition distinguishes a crash from a deliberate
    switch-off. All three were needed to identify the actual cause.
    """
    bad = [s for s in states if not s.ok]
    return {
        "detected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "boot_time": _boot_time(),
        "uptime_seconds": _uptime_seconds(),
        "session_manager": _run(["launchctl", "managername"]).strip(),
        "console_user": _run(["stat", "-f", "%Su", "/dev/console"]).strip(),
        "problems": [
            {
                "label": s.label,
                "purpose": s.purpose,
                "plist_exists": s.plist_exists,
                "loaded": s.loaded,
                "disposition": s.disposition,
                "problem": s.problem(),
            }
            for s in bad
        ],
        "healthy": [s.label for s in states if s.ok],
    }


def append_detection(state_dir: Path, record: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / DETECTIONS_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def notify(title: str, message: str) -> None:
    """Local notification only. OSASCRIPT is overridable so tests can count."""
    osascript = os.environ.get("OSASCRIPT", "/usr/bin/osascript")
    body = message.replace('"', '\\"')
    head = title.replace('"', '\\"')
    try:
        subprocess.run(
            [osascript, "-e", f'display notification "{body}" with title "{head}"'],
            capture_output=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def watchdog(state_dir: Path, *, quiet: bool = False) -> tuple[list[AgentState], Path | None]:
    """Check the agents; on any problem record it and raise a notification.

    Deliberately not run *by* the agents it watches: the daily job cannot report
    that the daily job is not running. The SessionEnd hook calls this instead,
    because that hook demonstrably kept firing throughout the outage.

    Because the hook is unattended, the BTM read is skipped there and every
    disposition arrives as unknown (see :func:`_btm_read_permitted`) -- the
    watchdog effectively checks plist + registration only. The full
    loaded-AND-allowed check still runs whenever ``lexicon agents`` is invoked
    from a terminal, where the sfltool password dialog is an answerable ask.
    """
    states = agent_states()
    if all(s.ok for s in states):
        return states, None
    record = detection_record(states)
    path = append_detection(state_dir, record)
    bad = [s for s in states if not s.ok]
    if not quiet:
        notify(
            "Lexicon automation is down",
            f"{len(bad)} launch agent(s) not running: "
            f"{', '.join(s.label.rsplit('.', 1)[-1] for s in bad)}. "
            f"See {path.name}.",
        )
    return states, path
