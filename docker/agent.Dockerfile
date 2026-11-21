# Agent image: base + every harness at its pinned version, default config.
FROM hc-base:latest

RUN useradd -m -u 1001 -s /bin/bash agent
COPY harnesses /opt/harnesses
RUN set -e; for f in /opt/harnesses/*/install.sh; do echo "== $f"; sh "$f"; done
COPY task/prompt.txt /opt/task/prompt.txt
COPY docker/agent-entry.sh /opt/agent-entry.sh
RUN chmod 0755 /opt/agent-entry.sh /opt/harnesses/*/run.sh

USER agent
WORKDIR /home/agent
ENTRYPOINT ["/opt/agent-entry.sh"]
