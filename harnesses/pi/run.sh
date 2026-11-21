#!/bin/bash
# Run Pi once, non-interactively, with the task prompt as its only user message.
# Events stream to stdout as JSON lines; the run wrapper saves them.
set -uo pipefail
cd /home/agent/work
args=(--print --mode json --model "${HC_MODEL:?HC_MODEL not set}")
if [ -n "${HC_THINKING:-}" ]; then args+=(--thinking "$HC_THINKING"); fi
exec pi "${args[@]}" "$(cat /opt/task/prompt.txt)"
