#!/bin/bash
# Claude Code SessionEnd hook for the Lexicon.
#
# Copies the finishing session's transcript directory into the archive, then
# kicks an incremental index. This is the automatic backstop described in
# DESIGN.md 5.2: the convention block is the curated path, but a session must
# not be lost because an agent forgot to write.
#
# Two properties matter more than anything this does:
#
#   1. It returns in well under a second. Everything real is detached, because
#      a hook that blocks makes ending a session feel broken.
#   2. It can never fail the session. Every path exits 0.
#
# Indexing is guarded by the single-instance lock inside `lexicon index`, so a
# hook firing during the nightly job is a silent skip (D-2026-08-18-16).
# Copying into the archive is NOT gated on that lock: losing a transcript is
# permanent, skipping an index is not.

set -uo pipefail

LEXICON_ROOT="${LEXICON_ROOT:-$HOME/Lexicon}"
ARCHIVE_DIR="$LEXICON_ROOT/archive/claude-code"
LOG_DIR="$LEXICON_ROOT/index/logs"
LOG="$LOG_DIR/hook.log"
STATE_DIR="$LEXICON_ROOT/index/state"
# The binary lives beside this script's repo; LEXICON_BIN overrides it.
LEXICON_BIN="${LEXICON_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/lexicon}"

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$STATE_DIR" 2>/dev/null

# Claude Code delivers the hook payload as JSON on stdin. Read it with a short
# timeout so a missing stdin can never hang the session.
#
# NB: `read -d ''` returns non-zero when it stops at EOF rather than at a NUL,
# which is the normal case here -- so the exit status must be ignored, or the
# payload it just read gets thrown away.
PAYLOAD=""
read -r -t 2 -d '' PAYLOAD 2>/dev/null || true

TRANSCRIPT=$(printf '%s' "$PAYLOAD" | /usr/bin/python3 -c '
import json, sys
try:
    print((json.load(sys.stdin) or {}).get("transcript_path") or "")
except Exception:
    print("")
' 2>/dev/null)

SESSION_DIR=""
if [ -n "$TRANSCRIPT" ] && [ -e "$TRANSCRIPT" ]; then
  SESSION_DIR=$(dirname "$TRANSCRIPT")
fi

# Detach: the session is free to end the moment this subshell is backgrounded.
{
  ts() { date '+%Y-%m-%d %H:%M:%S'; }

  if [ -z "$SESSION_DIR" ]; then
    echo "$(ts) hook: no usable transcript_path in payload; nothing copied" >> "$LOG"
  else
    # --ignore-existing keeps the archive append-only: an existing copy is
    # never overwritten, only new files land. Transcripts grow by appending,
    # so also allow size-differing files through with --update.
    if rsync -a --update --exclude='.DS_Store' "$SESSION_DIR/" \
        "$ARCHIVE_DIR/$(basename "$SESSION_DIR")/" >>"$LOG" 2>&1; then
      n=$(find "$ARCHIVE_DIR/$(basename "$SESSION_DIR")" -type f 2>/dev/null | wc -l | tr -d ' ')
      echo "$(ts) hook: archived $(basename "$SESSION_DIR") ($n files)" >> "$LOG"
    else
      echo "$(ts) hook: rsync FAILED for $SESSION_DIR" >> "$LOG"
    fi
  fi

  # Watchdog (Phase 5 D2). Deliberately here and not in the daily job: the
  # daily job cannot report that the daily job is not running. This hook kept
  # firing throughout the 2026-08-19 outage, so it is the one place that can
  # notice. At most once a day -- an alarm on every session end gets ignored.
  STAMP="$STATE_DIR/agent-watchdog.stamp"
  TODAY=$(date '+%Y-%m-%d')
  if [ "$(cat "$STAMP" 2>/dev/null)" != "$TODAY" ]; then
    printf '%s' "$TODAY" > "$STAMP" 2>/dev/null
    if [ -x "$LEXICON_BIN" ]; then
      if "$LEXICON_BIN" agents --watchdog --quiet >/dev/null 2>&1; then
        echo "$(ts) hook: launch agents ok" >> "$LOG"
      else
        echo "$(ts) hook: LAUNCH AGENTS DOWN — detection recorded, notification raised" >> "$LOG"
        "$LEXICON_BIN" agents >> "$LOG" 2>&1
      fi
    fi
  fi

  if [ -x "$LEXICON_BIN" ]; then
    out=$("$LEXICON_BIN" index --quiet 2>&1)
    code=$?
    if printf '%s' "$out" | grep -q 'another index run is in progress'; then
      echo "$(ts) hook: index skipped, another run holds the lock" >> "$LOG"
    elif [ $code -ne 0 ]; then
      echo "$(ts) hook: index exited $code" >> "$LOG"
      printf '%s\n' "$out" | tail -5 >> "$LOG"
    else
      echo "$(ts) hook: index ok" >> "$LOG"
    fi
  else
    echo "$(ts) hook: lexicon binary not found at $LEXICON_BIN" >> "$LOG"
  fi
} </dev/null >/dev/null 2>&1 &
disown 2>/dev/null

exit 0
