#!/bin/bash
# Prepare the empty workspace, let the provider set the harness up for its
# model endpoint, then hand over to the harness adapter.
set -euo pipefail
HARNESS="${1:?usage: agent-entry.sh <harness>}"
mkdir -p /home/agent/work
cd /home/agent/work
git init -q -b main
git -c user.name=bench -c user.email=bench@localhost commit -q --allow-empty -m "empty workspace"
# Mounted by hc from providers/<name>/ or local/providers/<name>/.
if [ -f "/opt/provider/$HARNESS.sh" ]; then
  # shellcheck disable=SC1090
  . "/opt/provider/$HARNESS.sh"
  cd /home/agent/work
fi
exec "/opt/harnesses/$HARNESS/run.sh"
