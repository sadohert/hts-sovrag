#!/usr/bin/env bash
# PostToolUse hook: after Claude edits a Python file, format it and apply safe lint fixes.
# Remaining lint errors are sent back to Claude (exit 2) so they get fixed in the same turn.
set -u
file=$(jq -r '.tool_input.file_path // empty')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
uv run ruff format "$file" >/dev/null 2>&1
if ! out=$(uv run ruff check --fix "$file" 2>&1); then
  printf 'ruff check found issues in %s:\n%s\n' "$file" "$out" >&2
  exit 2
fi
