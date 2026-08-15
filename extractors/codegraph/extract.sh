#!/usr/bin/env bash
# Index one repo with CodeGraph and deposit its SQLite store under
# /out/codegraph/<corpus>/<repo-id>/.
#
# Usage (from the host):
#   docker compose run --rm -T codegraph <corpus> <repo-id> [subpath]
set -euo pipefail

CORPUS="${1:?corpus id required}"
REPO_ID="${2:?repo id required}"
SUBPATH="${3:-}"

SRC="/corpus/${CORPUS}/${REPO_ID}"
[[ -n "$SUBPATH" ]] && SRC="${SRC}/${SUBPATH}"
OUT="/out/codegraph/${CORPUS}/${REPO_ID}"

if [[ ! -d "$SRC" ]]; then
  echo "source not found: $SRC" >&2
  exit 1
fi

mkdir -p "$OUT"

# Same filtered-copy discipline as the graphify extractor. Vendored code is the
# reason: mimir, loki and tempo each carry a full copy of dskit under vendor/,
# and an extractor that indexes it resolves cross-repo imports to the local copy
# - erasing exactly the edge this harness exists to measure.
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

echo "==> codegraph $(cat /codegraph.version) :: ${CORPUS}/${REPO_ID}"
echo "    filtered copy: $(find "$WORK" -type f | wc -l | tr -d ' ') files"

cd "$WORK"

# Headless: no MCP server, no agent, no file watcher.
#
# `init` rather than `index`. Both build a full index, but `index` rebuilds an
# *existing* project and errors out when .codegraph/ is absent - which it always
# is here, because every run starts from a fresh filtered copy. Falling back
# from one to the other would just mask real failures.
codegraph init . --force 2>&1 | tail -20 | tee "${OUT}/extract.log"

DB="${WORK}/.codegraph/codegraph.db"
if [[ ! -f "$DB" ]]; then
  echo "WARNING: no database at ${DB}" >&2
  ls -la "${WORK}/.codegraph" 2>/dev/null >&2 || echo "  no .codegraph dir" >&2
  codegraph --help 2>&1 | head -30 >&2
  exit 1
fi

# WAL mode: checkpoint into the main file before copying, or a reader outside
# this container sees a stale database.
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
cp "$DB" "${OUT}/codegraph.db"

cat > "${OUT}/provenance.json" <<EOF
{
  "extractor": "codegraph",
  "extractor_version": "$(tr -d '\n' < /codegraph.version)",
  "corpus": "${CORPUS}",
  "repo_id": "${REPO_ID}",
  "subpath": "${SUBPATH}",
  "commit": "$(git -C "/corpus/${CORPUS}/${REPO_ID}" rev-parse HEAD 2>/dev/null || echo unknown)"
}
EOF

echo "==> wrote ${OUT}/codegraph.db ($(wc -c < "${OUT}/codegraph.db") bytes)"
