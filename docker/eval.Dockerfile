# Evaluator image: same Chromium as the agent image, plus ffmpeg for cutting
# the showcase clip and pngjs for the pixel-grid check. Never used for agents.
FROM hc-base:latest
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/evaluator
COPY evaluator/package.json evaluator/package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund
COPY evaluator/ ./
ENTRYPOINT ["/opt/evaluator/entry.sh"]
