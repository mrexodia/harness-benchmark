#!/bin/bash
# Evaluator entry: copy the read-only workspace, serve it exactly as the task
# says, and run the scripted session. Outputs go to /out.
#
# If the workspace has no index.html but exactly one top-level folder that has
# one (the agent built the game in a subfolder such as fishing/), that folder
# is served instead, as if the agent had built it at the root. The
# game_folder check reports this; it is not scored.
set -uo pipefail
rm -rf /tmp/ws && cp -a /in /tmp/ws
GAME=/tmp/ws
SERVED_FROM=.
if [ ! -f /tmp/ws/index.html ]; then
  mapfile -t CANDS < <(find /tmp/ws -mindepth 2 -maxdepth 2 -name index.html \
    -not -path '/tmp/ws/.*' -not -path '/tmp/ws/node_modules/*' -printf '%h\n')
  if [ "${#CANDS[@]}" -eq 1 ]; then
    GAME="${CANDS[0]}"
    SERVED_FROM="${GAME#/tmp/ws/}"
    echo "no index.html at the root; serving the only game folder: $SERVED_FROM"
  fi
fi
cd "$GAME"
python3 -m http.server 8000 --bind 127.0.0.1 > /out/http-server.log 2>&1 &
SERVER=$!
for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 0.1; done
node /opt/evaluator/evaluate.mjs --root "$GAME" --out /out --served-from "$SERVED_FROM"
CODE=$?
kill $SERVER 2>/dev/null
exit $CODE
