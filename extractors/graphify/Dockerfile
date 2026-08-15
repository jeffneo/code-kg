# Graphify extractor.
#
# Pinned deliberately: the agreement matrix between extractors is meaningless
# if tool versions drift between runs. Bump GRAPHIFY_VERSION explicitly and
# re-run the whole corpus when you do.
FROM python:3.12-slim

ARG GRAPHIFY_VERSION=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Package name is `graphifyy` (two y's) per the project's install docs; the CLI
# it installs is `graphify`. If this fails, check the current install command
# at https://github.com/Graphify-Labs/graphify
RUN if [ -n "$GRAPHIFY_VERSION" ]; then \
      uv tool install "graphifyy==${GRAPHIFY_VERSION}"; \
    else \
      uv tool install graphifyy; \
    fi

ENV PATH="/root/.local/bin:${PATH}"

# Record the resolved version so artifacts can be tagged with it.
RUN graphify --version > /graphify.version 2>&1 || echo "unknown" > /graphify.version

COPY extract.sh /usr/local/bin/extract.sh
RUN chmod +x /usr/local/bin/extract.sh

ENTRYPOINT ["/usr/local/bin/extract.sh"]
