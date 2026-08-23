"""Single-instance lock for index-writing commands.

Three things can now trigger an index: the SessionEnd hook, the daily launchd
job, and a human at a terminal. Two writers on one SQLite file is a corruption
risk, so index writes are serialised by a pid lockfile.

A blocked caller exits **0**, not an error (D-2026-08-18-16). A hook firing
during the nightly job should be a silent skip -- the work happens on the next
run regardless -- and making it look like a failure would train the alarm to be
ignored.

Capture (rsync into the archive) is deliberately *not* guarded by this lock:
losing a transcript is permanent, skipping an index is not.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LOCK_NAME = "lexicon-index.lock"


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* exists and we may signal it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        # EPERM means it exists but belongs to someone else -- still alive.
        return e.errno == errno.EPERM
    return True


@dataclass
class LockResult:
    acquired: bool
    holder_pid: int | None = None
    broke_stale: bool = False
    path: Path | None = None
    #: Free-text the holder left behind -- `lexicon web` stores its URL here so
    #: a second invocation can point at the server already running rather than
    #: just refusing.
    payload: str = ""
    label: str = "index run"

    @property
    def message(self) -> str:
        if self.acquired:
            return (
                f"acquired {self.label} lock (broke a stale lock)"
                if self.broke_stale else f"acquired {self.label} lock"
            )
        extra = f" at {self.payload}" if self.payload else ""
        return f"another {self.label} is in progress (pid {self.holder_pid}){extra}"


class IndexLock:
    """Cooperative pid lock. Use as a context manager or via ``acquire()``."""

    def __init__(self, lock_dir: Path, name: str = DEFAULT_LOCK_NAME,
                 label: str = "index run") -> None:
        self.path = Path(lock_dir) / name
        self.label = label
        self._held = False
        self.result: LockResult | None = None

    def acquire(self, payload: str = "") -> LockResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        broke_stale = False
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self._read_pid()
                if holder is not None and _pid_alive(holder):
                    self.result = LockResult(False, holder_pid=holder, path=self.path,
                                             payload=self._read_payload(), label=self.label)
                    return self.result
                # Stale: the writer died without cleaning up. Break it and retry
                # once -- a crashed nightly job must not wedge every later run.
                try:
                    self.path.unlink()
                    broke_stale = True
                except FileNotFoundError:
                    pass
                continue
            else:
                with os.fdopen(fd, "w") as fh:
                    fh.write(str(os.getpid()))
                    if payload:
                        fh.write("\n" + payload)
                self._held = True
                self.result = LockResult(True, holder_pid=os.getpid(),
                                         broke_stale=broke_stale, path=self.path,
                                         payload=payload, label=self.label)
                return self.result
        # Lost a race to another process that recreated the lock.
        holder = self._read_pid()
        self.result = LockResult(False, holder_pid=holder, path=self.path,
                                 payload=self._read_payload(), label=self.label)
        return self.result

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _read_pid(self) -> int | None:
        # First line only: the rest of the file is the holder's payload. A
        # single-line lockfile -- every one written before Phase 4 -- parses
        # exactly as it always did.
        lines = self._lines()
        if not lines:
            return None
        try:
            return int(lines[0].strip())
        except ValueError:
            return None  # unreadable content == stale

    def _read_payload(self) -> str:
        lines = self._lines()
        return "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    def release(self) -> None:
        if not self._held:
            return
        # Only remove a lock we still own, so breaking a stale lock elsewhere
        # cannot make us delete a live holder's file.
        if self._read_pid() == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False

    def __enter__(self) -> LockResult:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
