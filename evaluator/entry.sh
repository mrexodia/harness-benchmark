#!/bin/bash
# Evaluator entry: copy the read-only workspace, serve it exactly as the task
# says, and run the scripted session. Outputs go to /out.
set -uo pipefail
rm -rf /tmp/ws && cp -a /in /tmp/ws
cd /tmp/ws
python3 -m http.server 8000 --bind 127.0.0.1 > /out/http-server.log 2>&1 &
SERVER=$!
for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 0.1; done
node /opt/evaluator/evaluate.mjs --root /tmp/ws --out /out
CODE=$?
kill $SERVER 2>/dev/null
exit $CODE
