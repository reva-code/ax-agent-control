#!/bin/sh
# Stream an agent's transcript: steps, Claude's messages and tool calls.
# Tokens are masked. Pass --raw for the unfiltered (still masked) log.
#   ./logs.sh agent-webhook [--raw]
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
stream() { "$HOME/go/bin/kubectl-ate" --context kind-kind logs actors "$1" -a default -f 2>&1; }
if [ "${2:-}" = "--raw" ]; then
  stream "$1" | sed -E \
    -e 's/(ghp_|gho_|github_pat_)[A-Za-z0-9_]+/\1***/g' \
    -e 's/sk-ant-[A-Za-z0-9_-]+/sk-ant-***/g' \
    -e 's#//[^/@ ]+@#//***@#g'
else
  stream "$1" | python3 "$HERE/format_logs.py"
fi
