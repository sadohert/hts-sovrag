#!/usr/bin/env bash
# PreToolUse hook: before any `git push`, hand Claude the working-tree status so
# uncommitted changes are noticed before the push. PreToolUse stdout is not shown
# to anyone unless it is JSON with additionalContext, hence the JSON envelope.
set -u
cmd=$(jq -r '.tool_input.command // empty')
case "$cmd" in
  *"git push"*) ;;
  *) exit 0 ;;
esac
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
status=$(git status --porcelain | head -10)
if [ -z "$status" ]; then
  msg="Working tree is clean before push."
else
  msg="Uncommitted changes present before push:"$'\n'"$status"
fi
jq -n --arg msg "$msg" '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $msg}}'
