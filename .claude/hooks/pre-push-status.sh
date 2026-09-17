#!/usr/bin/env bash
# PreToolUse hook: before any `git push`, show uncommitted changes so nothing is left behind.
set -u
cmd=$(jq -r '.tool_input.command // empty')
case "$cmd" in
  *"git push"*)
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    echo "[hook] git status before push:"
    git status --porcelain | head -10
    ;;
esac
exit 0
