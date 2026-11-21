#!/bin/sh
# Pi at a pinned version, default settings. The model provider is not part of
# the harness: it is set up at run time by the provider's pi.sh hook.
set -e
VERSION="$(cat /opt/harnesses/pi/VERSION)"
npm install -g --no-audit --no-fund "@earendil-works/pi-coding-agent@${VERSION}"
