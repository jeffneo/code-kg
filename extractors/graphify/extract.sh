#!/usr/bin/env bash
# Run graphify over one repo and deposit its artifacts under /out/graphify/<corpus>/<repo-id>/.
#
# Usage (from the host):
#   docker compose run --rm graphify <corpus> <repo-id> [subpath]
set -euo pipefail

CORPUS="${1:?corpus id required}"
REPO_ID="${2:?repo id required}"
SUBPATH="${3:-}"

SRC="/corpus/${CORPUS}/${REPO_ID}"
[[ -n "$SUBPATH" ]] && SRC="${SRC}/${SUBPATH}"
OUT="/out/graphify/${CORPUS}/${REPO_ID}"

if [[ ! -d "$SRC" ]]; then
  echo "source not found: $SRC" >&2
  echo "did you run 'make corpus CORPUS=${CORPUS}' first?" >&2
  exit 1
fi

mkdir -p "$OUT"

# Extract from a filtered copy, never the checkout itself.
#
# Vendored code is the reason this exists. mimir carries a complete copy of
# dskit under vendor/github.com/grafana/dskit - 8194 vendored .go files in all.
# Left in, a single-repo extractor resolves `github.com/grafana/dskit/ring`
# to the local vendored copy, the edge becomes intra-repo, and the cross-repo
# relationship disappears entirely. Generated code is excluded for the separate
# reason that it wrecks centrality metrics.
#
# Keep this list in sync with `exclude.globs` in corpus/corpus.yaml, which is
# the documented source of truth.
WORK=/work
rm -rf "$WORK" && mkdir -p "$WORK"
tar -C "$SRC" \
  --exclude='./vendor' \
  --exclude='./node_modules' \
  --exclude='./testdata' \
  --exclude='./.venv' \
  --exclude='./dist' \
  --exclude='./build' \
  --exclude='./generated' \
  --exclude='*.pb.go' \
  --exclude='*_generated.go' \
  --exclude='zz_generated*.go' \
  --exclude='*_pb2.py' \
  --exclude='*_pb2_grpc.py' \
  -cf - . | tar -C "$WORK" -xf -

echo "    filtered copy: $(find "$WORK" -type f | wc -l | tr -d ' ') files (from $(find "$SRC" -type f | wc -l | tr -d ' '))"
SRC="$WORK"
cd "$SRC"

echo "==> graphify $(cat /graphify.version) :: ${CORPUS}/${REPO_ID}"

# `graphify update <path>` is the headless extraction path - the `/graphify`
# skill invocation documented on the website drives an AI assistant and is not
# a CLI command. `update` is pure tree-sitter AST work: no API key, no model
# call, deterministic. --no-cluster skips Leiden, which we recompute with GDS
# over the joined graph where it means something different and more useful.
#
# Output always lands in <path>/graphify-out/ - there is no --output flag - so
# we extract in place and copy the artifact out.
graphify update . --no-cluster 2>&1 | tee "${OUT}/extract.log"

PRODUCED="${SRC}/graphify-out/graph.json"
if [[ ! -f "$PRODUCED" ]]; then
  echo "WARNING: no graph.json at ${PRODUCED}" >&2
  echo "check ${OUT}/extract.log; CLI surface for this version:" >&2
  graphify --help 2>&1 | head -30 >&2
  exit 1
fi
cp "$PRODUCED" "${OUT}/graph.json"
[[ -f "${SRC}/graphify-out/manifest.json" ]] && cp "${SRC}/graphify-out/manifest.json" "${OUT}/"

# Stamp provenance next to the artifact so the loader can attribute edges.
cat > "${OUT}/provenance.json" <<EOF
{
  "extractor": "graphify",
  "extractor_version": "$(cat /graphify.version | tr -d '\n')",
  "corpus": "${CORPUS}",
  "repo_id": "${REPO_ID}",
  "subpath": "${SUBPATH}",
  "commit": "$(git -C "/corpus/${CORPUS}/${REPO_ID}" rev-parse HEAD 2>/dev/null || echo unknown)"
}
EOF

echo "==> wrote ${OUT}/graph.json ($(wc -c < "${OUT}/graph.json") bytes)"
