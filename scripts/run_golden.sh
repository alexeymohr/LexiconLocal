#!/bin/bash
# Weekly search-quality regression check.
#
# Both ranking defects found in Phase 2 passed every unit test -- they were
# only visible against the real index. This is the guard for that class of
# problem, so its failure has to be as loud as the daily job's.
set -uo pipefail
# The repo is wherever this script lives; LEXICON_REPO overrides it.
REPO="${LEXICON_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="${LEXICON_ROOT:-$HOME/Lexicon}/index/logs/golden.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "=== golden run $(date '+%Y-%m-%d %H:%M:%S') ==="
  "$REPO/.venv/bin/python" "$REPO/scripts/golden_queries.py"
  code=$?
  echo "exit=$code"
  if [ $code -ne 0 ]; then
    "${OSASCRIPT:-/usr/bin/osascript}" -e 'display notification "Search quality regression — see golden.log" with title "LexiconLocal: golden queries FAILED"' 2>/dev/null || true
  fi
  exit $code
} 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
