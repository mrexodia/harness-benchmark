# Shared base for the agent and evaluator images: pinned Node, Python 3, git,
# and Playwright with headless Chromium installed globally.
FROM node:24.22.3-bookworm@sha256:9d266751ca3d7fdbe651c878ffd5ad0a42fed49a18c283509267c97f025b7496

ARG PLAYWRIGHT_VERSION=1.65.1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_PATH=/usr/local/lib/node_modules \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip python3-venv ca-certificates \
 && npm install -g playwright@${PLAYWRIGHT_VERSION} \
 && npx -y playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium \
 && pip3 install --no-cache-dir --break-system-packages playwright==${PLAYWRIGHT_VERSION} \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

# Python Playwright of the same version may pin a different browser build.
RUN python3 -m playwright install chromium && chmod -R a+rX /ms-playwright

# Make globally installed packages resolvable from any folder, for both
# require() (NODE_PATH) and ESM import (which walks up to /node_modules).
RUN ln -s /usr/local/lib/node_modules /node_modules
