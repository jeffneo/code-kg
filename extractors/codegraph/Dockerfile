# CodeGraph extractor (github.com/colbymchenry/codegraph).
#
# Second extractor, added to make the agreement matrix real - the one capability
# that genuinely requires more than one tool. Its store is SQLite rather than
# JSON, so the ETL is a SELECT rather than a parse.
#
# Pinned, like graphify: an agreement matrix computed across drifting tool
# versions measures nothing.
FROM node:22-slim

ARG CODEGRAPH_VERSION=1.5.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates sqlite3 \
 && rm -rf /var/lib/apt/lists/*

RUN npm i -g "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"

ENV CODEGRAPH_TELEMETRY=0
RUN codegraph --version > /codegraph.version 2>&1 || echo "unknown" > /codegraph.version

COPY extract.sh /usr/local/bin/extract.sh
RUN chmod +x /usr/local/bin/extract.sh

ENTRYPOINT ["/usr/local/bin/extract.sh"]
