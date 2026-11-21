#!/bin/sh
# Build fixtures/broken from fixtures/reference with every BUGS flag on.
cd "$(dirname "$0")" && rm -rf broken && cp -R reference broken
sed -i.bak 's/^const BUGS = .*/const BUGS = { wrongOdds: true, ignoreHook: true, noHiddenPause: true, doubleCatch: true, externalFetch: true, audio: true };/' broken/game.js && rm broken/game.js.bak
