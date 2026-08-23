# Shared by the leak-guard hooks. Sourced, not executed.
#
# The guard is Python and must run inside the project venv (it imports yaml).
# Hooks run with a minimal environment, so locate the venv relative to the
# repo rather than trusting PATH.
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
GUARD="$REPO/.venv/bin/python"
[ -x "$GUARD" ] || GUARD="python3"
run_guard() { "$GUARD" "$REPO/scripts/leak_guard.py" "$@"; }
