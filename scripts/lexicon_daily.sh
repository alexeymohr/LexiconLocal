#!/bin/bash
# LexiconLocal daily maintenance. Run by launchd at 03:30; safe to run by hand.
#
# Order matters: capture first, then index. Capture is the irreplaceable half --
# local agent logs expire, and a transcript not copied tonight may not exist
# tomorrow. Indexing can always be redone from the archive.
#
#   1. sync agent transcripts into the archive (copy, never move, append-only)
#   2. re-snapshot Codex memories only if their content changed
#   3. file anything dropped in downloaded_archives/
#   4. preflight -> index -> report
#   5. safety-net commit of ~/Lexicon if a session wrote notes but never committed
#   6. warn if account exports have gone stale
#
# Any non-zero step raises a macOS notification (D-2026-08-18-15). A healthy run
# is silent -- an alarm that fires on good days gets ignored on bad ones.

set -uo pipefail

# The repo is wherever this script lives; LEXICON_REPO overrides it.
REPO="${LEXICON_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LEXICON_ROOT="${LEXICON_ROOT:-$HOME/Lexicon}"
ARCHIVE="$LEXICON_ROOT/archive"
LOG_DIR="$LEXICON_ROOT/index/logs"
LOG="$LOG_DIR/daily.log"
STATE_DIR="$LEXICON_ROOT/index/state"
LEXICON_BIN="${LEXICON_BIN:-$REPO/.venv/bin/lexicon}"
KEEP_RUNS=30
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$LOG_DIR" "$STATE_DIR" "$ARCHIVE"/{claude-code,codex,claude,chatgpt,documents}

PROBLEMS=()
ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "$(ts) $*" | tee -a "$LOG"; }
problem() { PROBLEMS+=("$1"); say "PROBLEM: $1"; }

# osascript is the only alarm channel: local, no external service.
# OSASCRIPT is overridable purely so the Task 6 proofs can count notifications
# instead of merely flashing them past a human.
OSASCRIPT="${OSASCRIPT:-/usr/bin/osascript}"
notify() {
  local title="$1" msg="$2"
  "$OSASCRIPT" -e "display notification \"${msg//\"/\\\"}\" with title \"${title//\"/\\\"}\"" 2>/dev/null || true
}

rotate_log() {
  # Keep roughly KEEP_RUNS runs by trimming on run-separator lines.
  [ -f "$LOG" ] || return 0
  local starts
  starts=$(grep -c '^=== lexicon daily run' "$LOG" 2>/dev/null || echo 0)
  if [ "${starts:-0}" -gt "$KEEP_RUNS" ]; then
    local cut
    cut=$(grep -n '^=== lexicon daily run' "$LOG" | tail -"$KEEP_RUNS" | head -1 | cut -d: -f1)
    if [ -n "$cut" ] && [ "$cut" -gt 1 ]; then
      tail -n +"$cut" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
  fi
}

rotate_log
echo "=== lexicon daily run $(ts) ===" >> "$LOG"
say "starting (dry_run=$DRY_RUN)"

# ---------------------------------------------------------------------------
# 1. Archive sync -- copy, never move; never overwrite what is already there.
# ---------------------------------------------------------------------------
sync_tree() {
  local src="$1" dst="$2" label="$3"
  [ -d "$src" ] || { say "sync $label: source $src absent, skipping"; return 0; }
  mkdir -p "$dst"
  local before after
  before=$(find "$dst" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$DRY_RUN" = "1" ]; then
    say "sync $label: DRY RUN"; return 0
  fi
  # --update: transcripts grow by appending, so a newer source replaces an
  # older copy, but an existing newer archive copy is never clobbered.
  if rsync -a --update --exclude='.DS_Store' "$src/" "$dst/" >>"$LOG" 2>&1; then
    after=$(find "$dst" -type f 2>/dev/null | wc -l | tr -d ' ')
    say "sync $label: $before -> $after files (+$((after - before)))"
  else
    problem "rsync failed for $label ($src -> $dst)"
  fi
}

sync_tree "$HOME/.claude/projects"        "$ARCHIVE/claude-code"                "claude-code"
sync_tree "$HOME/.codex/sessions"         "$ARCHIVE/codex/sessions"             "codex sessions"
sync_tree "$HOME/.codex/archived_sessions" "$ARCHIVE/codex/archived_sessions"   "codex archived"
sync_tree "$HOME/.codex/attachments"      "$ARCHIVE/codex/attachments"          "codex attachments"
for f in session_index.jsonl history.jsonl; do
  if [ -f "$HOME/.codex/$f" ] && [ "$DRY_RUN" != "1" ]; then
    cp -p "$HOME/.codex/$f" "$ARCHIVE/codex/$f" 2>>"$LOG" \
      && say "sync codex $f: copied" || problem "failed to copy codex $f"
  fi
done

# ---------------------------------------------------------------------------
# 2. Codex memories -- re-snapshot only when the content actually changed.
# ---------------------------------------------------------------------------
MEM_SRC="$HOME/.codex/memories"
if [ -d "$MEM_SRC" ]; then
  NEW_HASH=$("$REPO/.venv/bin/python" -c "
from pathlib import Path
from lexiconlocal.dropbox import dir_content_hash
print(dir_content_hash(Path('$MEM_SRC')))" 2>/dev/null)
  HASH_FILE="$STATE_DIR/codex-memories.hash"
  OLD_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")
  if [ -z "$NEW_HASH" ]; then
    problem "could not hash $MEM_SRC"
  elif [ "$NEW_HASH" = "$OLD_HASH" ]; then
    say "codex memories: unchanged, no new snapshot"
  elif [ "$DRY_RUN" = "1" ]; then
    say "codex memories: changed (DRY RUN, no snapshot)"
  else
    SNAP="$ARCHIVE/codex/memories-$(date '+%Y-%m-%d')"
    if rsync -a --exclude='.DS_Store' "$MEM_SRC/" "$SNAP/" >>"$LOG" 2>&1; then
      echo "$NEW_HASH" > "$HASH_FILE"
      say "codex memories: changed -> snapshot $(basename "$SNAP")"
    else
      problem "codex memories snapshot failed"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 3. Export drop point.
# ---------------------------------------------------------------------------
DROP="$REPO/downloaded_archives"
if [ -d "$DROP" ]; then
  PLAN=$("$REPO/.venv/bin/python" -c "
import json
from pathlib import Path
from lexiconlocal.dropbox import scan_drop_point
plan = [
    {'path': str(e.path), 'kind': e.kind,
     'destination': str(e.destination) if e.destination else '', 'reason': e.reason}
    for e in scan_drop_point(Path('$DROP'), Path('$LEXICON_ROOT'))
]
print(json.dumps(plan))" 2>>"$LOG")
  COUNT=$(printf '%s' "$PLAN" | "$REPO/.venv/bin/python" -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
  if [ "${COUNT:-0}" -gt 0 ]; then
    say "drop point: $COUNT entr(y|ies) to process"
    while IFS=$'\t' read -r P KIND DEST REASON; do
      [ -z "$P" ] && continue
      case "$KIND" in
        claude-batch)
          if [ "$DRY_RUN" = "1" ]; then say "drop: would file $(basename "$P") -> $DEST"; continue; fi
          mkdir -p "$(dirname "$DEST")"
          if [ -e "$DEST" ]; then
            say "drop: $(basename "$P") already in the archive, leaving the copy in place"
          elif mv "$P" "$DEST" 2>>"$LOG"; then
            say "drop: filed Claude batch $(basename "$P") -> $DEST"
          else
            problem "could not file Claude batch $(basename "$P")"
          fi
          ;;
        chatgpt-zip)
          if [ "$DRY_RUN" = "1" ]; then say "drop: would file+extract $(basename "$P") -> $DEST"; continue; fi
          mkdir -p "$DEST"
          if cp -p "$P" "$DEST/$(basename "$P")" 2>>"$LOG"; then
            # Keep the zip AND extract alongside it: the zip is the untouched
            # artifact, the extraction is what the parser reads.
            if (cd "$DEST" && /usr/bin/unzip -qo "$(basename "$P")" -d extracted) >>"$LOG" 2>&1; then
              say "drop: filed + extracted ChatGPT export $(basename "$P") -> $DEST"
              rm -f "$P"
            else
              problem "unzip failed for $(basename "$P") (zip kept at $DEST)"
            fi
          else
            problem "could not copy ChatGPT export $(basename "$P")"
          fi
          ;;
        chatgpt-dir)
          # The 2026-08-18 export arrived already extracted: an opaquely named
          # directory of conversations-NNN.json shards. Move it whole.
          if [ "$DRY_RUN" = "1" ]; then say "drop: would file $(basename "$P") -> $DEST"; continue; fi
          mkdir -p "$(dirname "$DEST")"
          if [ -e "$DEST" ]; then
            say "drop: $(basename "$P") already in the archive, leaving the copy in place"
          elif mv "$P" "$DEST" 2>>"$LOG"; then
            say "drop: filed ChatGPT export $(basename "$P") -> $DEST"
          else
            problem "could not file ChatGPT export $(basename "$P")"
          fi
          ;;
        unrecognised)
          problem "unrecognised item in downloaded_archives: $(basename "$P") — $REASON"
          ;;
        *)
          # A kind classify_drop knows about but this script does not. Silence
          # here would mean an export sat in the drop point indefinitely.
          problem "drop kind '$KIND' has no handler in lexicon_daily.sh: $(basename "$P")"
          ;;
      esac
    done < <(printf '%s' "$PLAN" | "$REPO/.venv/bin/python" -c '
import json, sys
# Emit "-" for an empty destination: tab is IFS whitespace, so bash `read`
# collapses consecutive tabs and an empty field would silently shift every
# later column -- which swallowed the reason text on unrecognised drops.
for e in json.load(sys.stdin):
    print("\t".join([e["path"], e["kind"], e["destination"] or "-", e["reason"] or "-"]))')
  else
    say "drop point: nothing to process"
  fi
fi

# ---------------------------------------------------------------------------
# 4. preflight -> index -> report
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  say "index: DRY RUN, skipping preflight/index/report"
else
  if "$LEXICON_BIN" preflight >>"$LOG" 2>&1; then
    say "preflight: ok"
  else
    problem "preflight failed — see daily.log (Ollama, the local model, the launch agents, or MCP registration)"
  fi

  # Runs regardless of preflight: stage 1 needs no Ollama (D-2026-08-18-17).
  INDEX_OUT=$("$LEXICON_BIN" index 2>&1); INDEX_CODE=$?
  printf '%s\n' "$INDEX_OUT" >> "$LOG"
  if printf '%s' "$INDEX_OUT" | grep -q 'another index run is in progress'; then
    say "index: skipped, another run holds the lock"
  elif [ $INDEX_CODE -ne 0 ]; then
    problem "lexicon index exited $INDEX_CODE"
  else
    say "index: ok ($(printf '%s' "$INDEX_OUT" | grep -E 'documents written' | tr -s ' '))"
  fi

  REPORT_OUT=$("$LEXICON_BIN" report 2>&1); REPORT_CODE=$?
  printf '%s\n' "$REPORT_OUT" >> "$LOG"
  if [ $REPORT_CODE -ne 0 ]; then
    problem "lexicon report exited $REPORT_CODE — $(printf '%s' "$REPORT_OUT" | grep -E 'VERDICT|PENDING EMBED' | head -1)"
  else
    say "report: ok ($(printf '%s' "$REPORT_OUT" | grep -E '^  VERDICT' | sed 's/^ *//'))"
  fi
fi

# ---------------------------------------------------------------------------
# 4b. Regenerate HOME.md from the index and the curated notes.
#
# Deliberately *before* the safety-net commit below, so the regenerated file is
# picked up by it rather than sitting dirty until tomorrow. The file carries
# `generated: true` and DO NOT EDIT -- it is a landing page for editors and
# agents, never a source.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  say "HOME.md: DRY RUN"
elif "$REPO/.venv/bin/lexicon" dashboard --write-home >>"$LOG" 2>&1; then
  say "HOME.md: regenerated"
else
  problem "could not regenerate HOME.md"
fi

# ---------------------------------------------------------------------------
# 5. Safety-net commit: a session wrote notes but forgot to commit them.
# ---------------------------------------------------------------------------
if [ -d "$LEXICON_ROOT/.git" ]; then
  DIRTY=$(git -C "$LEXICON_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  if [ "${DIRTY:-0}" -gt 0 ]; then
    if [ "$DRY_RUN" = "1" ]; then
      say "safety-net commit: DRY RUN ($DIRTY changed path(s))"
    elif git -C "$LEXICON_ROOT" add -A && \
         git -C "$LEXICON_ROOT" commit -q -m "daily sync: uncommitted changes" 2>>"$LOG"; then
      say "safety-net commit: committed $DIRTY changed path(s)"
    else
      problem "safety-net commit failed"
    fi
  else
    say "safety-net commit: nothing uncommitted"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Export freshness -- warn at most weekly so it stays a signal.
# ---------------------------------------------------------------------------
STALE=$("$REPO/.venv/bin/python" -c "
from pathlib import Path
from lexiconlocal.dropbox import stale_sources
print('; '.join(stale_sources(Path('$LEXICON_ROOT'))))" 2>>"$LOG")
if [ -n "$STALE" ]; then
  STAMP="$STATE_DIR/export-warning.stamp"
  LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  if [ $((NOW - LAST)) -ge 604800 ]; then
    say "export freshness: $STALE"
    [ "$DRY_RUN" = "1" ] || echo "$NOW" > "$STAMP"
    [ "$DRY_RUN" = "1" ] || notify "LexiconLocal: exports are stale" "$STALE"
  else
    say "export freshness: $STALE (notification suppressed, warned within the last 7 days)"
  fi
else
  say "export freshness: ok"
fi

# ---------------------------------------------------------------------------
# Alarm
# ---------------------------------------------------------------------------
if [ ${#PROBLEMS[@]} -gt 0 ]; then
  say "FINISHED WITH ${#PROBLEMS[@]} PROBLEM(S)"
  [ "$DRY_RUN" = "1" ] || notify "LexiconLocal: attention needed" "${#PROBLEMS[@]} problem(s) — see daily.log"
  exit 1
fi
say "finished clean"
exit 0
