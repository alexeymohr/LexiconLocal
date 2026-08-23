#!/bin/bash
# Point this clone's git hooks at scripts/hooks/, so the leak guard runs on
# every commit and the push-time review on every push. Idempotent.
set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod +x "$REPO"/scripts/hooks/pre-commit "$REPO"/scripts/hooks/commit-msg "$REPO"/scripts/hooks/pre-push
git -C "$REPO" config core.hooksPath scripts/hooks
echo "hooks installed: $(git -C "$REPO" config core.hooksPath)"
echo "  pre-commit  -> leak guard over staged content"
echo "  commit-msg  -> leak guard over the message"
echo "  pre-push    -> prints every outgoing commit, guards the range, asks for 'yes'"
