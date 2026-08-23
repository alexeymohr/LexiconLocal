#!/bin/bash
# Install (or repair) the three LexiconLocal LaunchAgents. Idempotent.
#
# Phase 3 bootstrapped these by hand. That made them undocumented state: there
# was no way to restore them, no way to check them, and when macOS removed all
# three on 2026-08-19 nothing noticed for most of a day. This script is the
# missing half -- the agents are now reproducible from the repo.
#
# What it does NOT do, and cannot: re-allow an agent that Background Task
# Management has disallowed. That flag is set by the user in System Settings and
# is deliberately not writable from a script. This script detects it and says so
# instead of reporting a success that would evaporate.
#
#   ./scripts/install_agents.sh          install and verify
#   ./scripts/install_agents.sh --check  verify only, change nothing

set -uo pipefail

# The repo is wherever this script lives; LEXICON_REPO overrides it.
REPO="${LEXICON_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SRC="$REPO/scripts/launchd"
DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(com.lexiconlocal.daily com.lexiconlocal.golden com.lexiconlocal.export-reminder)
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

fail=0
note() { printf '%s\n' "$*"; }

mkdir -p "$DEST"

if [ "$CHECK_ONLY" = "0" ]; then
  LEXICON_ROOT="${LEXICON_ROOT:-$HOME/Lexicon}"
  for label in "${LABELS[@]}"; do
    src="$SRC/$label.plist.template"
    if [ ! -f "$src" ]; then
      note "MISSING  $src — cannot install $label"
      fail=1
      continue
    fi
    # Render the template for this machine. The plists cannot ship with real
    # paths: launchd needs absolute ones and they differ for every operator.
    rendered=$(sed -e "s|__REPO__|$REPO|g" \
                   -e "s|__LEXICON_ROOT__|$LEXICON_ROOT|g" \
                   -e "s|__HOME__|$HOME|g" "$src")
    # Write only when it differs, so an unchanged agent is not disturbed.
    if [ "$rendered" != "$(cat "$DEST/$label.plist" 2>/dev/null)" ]; then
      printf '%s\n' "$rendered" > "$DEST/$label.plist" || { note "FAILED   writing $label"; fail=1; continue; }
      note "rendered $label.plist"
    fi
    # bootout first: bootstrap on an already-loaded label is an error, and a
    # stale definition would otherwise survive a plist change.
    launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1
    if launchctl bootstrap "$DOMAIN" "$DEST/$label.plist" >/dev/null 2>&1; then
      note "loaded   $label"
    else
      note "FAILED   launchctl bootstrap $label"
      fail=1
    fi
  done
  note ""
fi

# ---------------------------------------------------------------------------
# Verify. Registration alone is not proof -- see the BTM note at the top.
# ---------------------------------------------------------------------------
note "verifying:"
loaded=$(launchctl list 2>/dev/null | awk -F'\t' '{print $3}')
# `sfltool dumpbtm` raises the macOS admin-password dialog. Never summon it
# from an unattended caller: read it only when a human plausibly ran this on
# purpose (a TTY is attached), or LEXICON_BTM=force. LEXICON_BTM=skip silences
# it even interactively. Mirrors the gate in lexiconlocal/agents.py.
btm=""
case "${LEXICON_BTM:-}" in
  force) btm=$(sfltool dumpbtm 2>/dev/null) ;;
  skip)  ;;
  *)     if [ -t 0 ] || [ -t 1 ]; then btm=$(sfltool dumpbtm 2>/dev/null); fi ;;
esac
[ -n "$btm" ] || note "(Background Task Management state not read -- unattended, or LEXICON_BTM=skip)"

for label in "${LABELS[@]}"; do
  status="ok"
  if ! printf '%s\n' "$loaded" | grep -qx "$label"; then
    status="NOT LOADED"
    fail=1
  elif printf '%s' "$btm" | grep -A 2 "Identifier: [0-9]*\.$label" >/dev/null 2>&1 \
       && printf '%s' "$btm" | grep -B 2 "Identifier: [0-9]*\.$label" \
          | grep -q 'Disposition:.*disallowed'; then
    status="DISALLOWED in Background Task Management"
    fail=1
  fi
  printf '  %-38s %s\n' "$label" "$status"
done

if [ "$fail" -ne 0 ]; then
  note ""
  note "One or more agents are not runnable."
  note "If any says DISALLOWED, only you can fix it — a script cannot:"
  note "  System Settings > General > Login Items & Extensions >"
  note "  Allow in the Background  ->  enable the 'bash' and 'osascript' entries."
  note "They appear under 'Unknown Developer' because the agents run unsigned"
  note "shell and osascript, which is also why they are easy to switch off."
  exit 1
fi

note ""
note "All ${#LABELS[@]} agents installed, loaded and allowed."
exit 0
