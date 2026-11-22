#!/bin/sh
# The agent command of the long-history benchmark: one headless Claude Code session.
#
#   agent.sh WORKDIR PROMPT_FILE MCP_CONFIG SETTINGS ALLOWED_TOOLS_FILE
#
# The driver starts it with a scrubbed environment (a fresh HOME, auto-memory off unless the
# arm turns it on, proxy and credential variables passed through) and the working directory
# as the current directory; this script adds only the host flags, the same for every arm.
# The model is `sonnet` unless LONGHAUL_MODEL names another (the driver passes it through);
# LONGHAUL_MAX_BUDGET (US dollars) adds a per-session spending cap.
#
# Flags and why:
#   --output-format stream-json --verbose --include-hook-events   the event stream the
#       driver reads canaries, memory use and usage figures from (hook output included)
#   --setting-sources user,project   the fresh HOME and the working directory are the only
#       places settings or CLAUDE.md files can come from; none exist except an arm's own
#   --strict-mcp-config --mcp-config FILE   only the arm's own MCP servers
#   --settings FILE   the arm's hooks
#   --disable-slash-commands --no-session-persistence --permission-mode acceptEdits
#   --permission-prompts none   nothing can wait for an answer
#   --allowed-tools   running the project's tests, plus the arm's own tools
set -eu
workdir=$1
prompt=$2
mcp=$3
settings=$4
arm_tools=$(cat "$5")
cd "$workdir"
tools="Bash(python -m unittest:*),Bash(python3 -m unittest:*),Bash(python:*),Bash(python3:*),Bash(grep:*),Bash(ls:*),Bash(cat:*)"
[ -n "$arm_tools" ] && tools="$tools,$arm_tools"
budget=""
[ -n "${LONGHAUL_MAX_BUDGET:-}" ] && budget="--max-budget-usd ${LONGHAUL_MAX_BUDGET}"
# shellcheck disable=SC2086
exec claude -p --model "${LONGHAUL_MODEL:-sonnet}" \
  --output-format stream-json --verbose --include-hook-events \
  --max-turns 60 --permission-mode acceptEdits --permission-prompts none \
  --setting-sources user,project --disable-slash-commands --no-session-persistence \
  --strict-mcp-config --mcp-config "$mcp" --settings "$settings" \
  $budget --allowed-tools "$tools" -- "$(cat "$prompt")"
