# GitNexus extractor (github.com/abhigyanpatwari/GitNexus).
#
# Third extractor, which turns the agreement matrix from a pairwise comparison
# into a majority vote.
#
# Unlike the other two, its store is an embedded graph database (LadybugDB, a
# Kuzu fork) rather than a portable file, so extraction has to export through
# GitNexus's own `cypher` command rather than by reading the store directly.
# The native bindings are arch-specific; if the build or run fails on Apple
# Silicon, rebuild with --platform linux/amd64 and expect it to be slow.
FROM node:22-slim

ARG GITNEXUS_VERSION=1.6.9

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN npm i -g "gitnexus@${GITNEXUS_VERSION}"

RUN gitnexus --version > /gitnexus.version 2>&1 || echo "unknown" > /gitnexus.version

COPY extract.sh /usr/local/bin/extract.sh
RUN chmod +x /usr/local/bin/extract.sh

ENTRYPOINT ["/usr/local/bin/extract.sh"]
