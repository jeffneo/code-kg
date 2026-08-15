#!/usr/bin/env bash
# Index one repo with GitNexus and export its graph as JSON.
#
# GitNexus stores its graph in LadybugDB (a Kuzu fork) inside .gitnexus/lbug -
# an embedded database, not a portable file. There is no exporter, so the graph
# comes out through GitNexus's own `cypher` command.
#
# That command emits JSON wrapping a *markdown table*, so the parsing hack lives
# here at the boundary and the mapper receives clean JSON like the others.
# Columns are restricted to identifiers, paths and numbers, none of which
# contain '|'; rows with an unexpected column count are dropped rather than
# guessed at.
#
# Usage: docker compose run --rm -T gitnexus <corpus> <repo-id> [subpath]
set -euo pipefail

CORPUS="${1:?corpus id required}"
REPO_ID="${2:?repo id required}"
SUBPATH="${3:-}"

SRC="/corpus/${CORPUS}/${REPO_ID}"
[[ -n "$SUBPATH" ]] && SRC="${SRC}/${SUBPATH}"
OUT="/out/gitnexus/${CORPUS}/${REPO_ID}"

[[ -d "$SRC" ]] || { echo "source not found: $SRC" >&2; exit 1; }
mkdir -p "$OUT"

# Same filtered-copy discipline as the other extractors; see graphify/extract.sh
# for why vendored code must not reach the indexer.
WORK=/work
rm -rf "$WORK" && mkdir -p "$WORK"
tar -C "$SRC" \
  --exclude='./vendor' --exclude='./node_modules' --exclude='./testdata' \
  --exclude='./.venv' --exclude='./dist' --exclude='./build' \
  --exclude='./generated' --exclude='*.pb.go' --exclude='*_generated.go' \
  --exclude='zz_generated*.go' --exclude='*_pb2.py' --exclude='*_pb2_grpc.py' \
  -cf - . | tar -C "$WORK" -xf -

cd "$WORK"
echo "==> gitnexus $(cat /gitnexus.version) :: ${CORPUS}/${REPO_ID}"
echo "    filtered copy: $(find "$WORK" -type f | wc -l | tr -d ' ') files"

# --skip-git: the filtered copy has no .git. --skip-agents-md: do not write
# AGENTS.md/CLAUDE.md into the tree. Embeddings are off by default, so no API
# key is needed and the run stays deterministic.
gitnexus analyze . --skip-git --skip-agents-md 2>&1 | tail -12 | tee "${OUT}/extract.log"

[[ -f "${WORK}/.gitnexus/lbug" ]] || {
  echo "WARNING: no LadybugDB store at ${WORK}/.gitnexus/lbug" >&2
  ls -la "${WORK}/.gitnexus" 2>/dev/null >&2 || true
  exit 1
}

export OUT
python3 - <<'PY'
import json, os, subprocess, tempfile

LIMIT = "5000000"

# Node tables that carry code symbols. Community / Process / Folder / Section
# are GitNexus-specific aggregates and are deliberately not imported as symbols:
# they have no counterpart in the other extractors and would skew the agreement
# matrix by appearing as "gitnexus only" for structural reasons.
SYMBOL_LABELS = [
    "Function", "Method", "Class", "Interface", "Struct", "Enum", "Variable",
    "Const", "Property", "Constructor", "Trait", "TypeAlias", "Record", "Union",
    "Typedef", "Macro", "Namespace", "Impl", "Delegate", "Static", "Template",
    "Route", "Module",
]


def cypher(query, label=""):
    """Run one Cypher query. Errors are reported, never silently swallowed.

    Swallowing them is how an earlier version of this script shipped an export
    with zero edges and three of twenty-three node labels, and looked fine.
    """
    # Stdout goes to a FILE, never a pipe.
    #
    # GitNexus is a Node CLI, and Node's stdout is asynchronous on a pipe but
    # synchronous on a file. The process exits with output still buffered, so a
    # piped capture truncates at the 64 KiB pipe buffer - silently, with exit
    # code 0. The edge export is ~300 KiB and came back as 65536 bytes of
    # valid-looking-but-truncated JSON until this was tracked down.
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as fh:
        out = subprocess.run(
            ["gitnexus", "cypher", query, "-l", LIMIT],
            stdout=fh, stderr=subprocess.PIPE, text=True,
        )
        fh.flush()
        fh.seek(0)
        stdout = fh.read()
    if out.returncode != 0:
        ERRORS.append(f"{label or query[:40]}: exit {out.returncode} {(out.stderr or '').strip()[:120]}")
        return []
    payload = _last_json_value(stdout)
    if payload is None:
        ERRORS.append(f"{label or query[:40]}: unparseable output {stdout[:120]}")
        return []
    if isinstance(payload, dict) and "error" in payload:
        ERRORS.append(f"{label or query[:40]}: {payload['error'][:120]}")
        return []
    # A query with rows returns {"markdown": ..., "row_count": n}; a query with
    # no rows returns a bare []. Both are valid, neither is an error.
    if not isinstance(payload, dict) or "markdown" not in payload:
        return []
    return parse_markdown(payload["markdown"])


def columns_of(label):
    """Actual columns on one node table.

    Node tables do not share a schema - Variable and Property have no
    isExported, for instance - so the projection has to be built per table
    rather than assumed.
    """
    rows = cypher(f'CALL table_info("{label}") RETURN *', f"table_info({label})")
    return {r["name"] for r in rows}


def _last_json_value(text):
    """Extract the result object from stdout.

    GitNexus interleaves structured log lines on stdout, so the stream can be
    two concatenated JSON documents. json.loads rejects that outright, which is
    how the whole edge export silently came back empty. Decode every top-level
    value and take the one that carries a result.
    """
    decoder = json.JSONDecoder()
    idx, found = 0, None
    text = text.strip()
    while idx < len(text):
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(value, list) or (isinstance(value, dict)
                                       and ("markdown" in value or "error" in value)):
            found = value
        idx = end
    return found


def parse_markdown(md):
    lines = [l for l in md.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return []
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    rows = []
    for line in lines[2:]:                       # skip header + separator
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue                             # malformed - drop, don't guess
        rows.append(dict(zip(header, cells)))
    return rows


ERRORS = []

nodes, absent = [], []
for label in SYMBOL_LABELS:
    cols = columns_of(label)
    if not cols:
        absent.append(label)          # table does not exist in this index
        continue
    want = [("id", "id"), ("name", "name"), ("filePath", "path"),
            ("startLine", "sl"), ("endLine", "el"), ("isExported", "exp")]
    proj = ", ".join(f"n.{c} AS {alias}" for c, alias in want if c in cols)
    rows = cypher(f"MATCH (n:`{label}`) RETURN {proj}", label)
    for r in rows:
        r["label"] = label
    nodes.extend(rows)

files = cypher("MATCH (f:File) RETURN f.id AS id, f.name AS name, f.filePath AS path", "File")
edges = cypher(
    "MATCH (a)-[r:CodeRelation]->(b) "
    "RETURN a.id AS src, b.id AS dst, r.type AS type, r.confidence AS conf",
    "edges",
)

doc = {"nodes": nodes, "files": files, "edges": edges,
       "labels_absent": absent, "errors": ERRORS}
with open(os.path.join(os.environ["OUT"], "graph.json"), "w") as fh:
    json.dump(doc, fh)

print(f"    exported {len(nodes)} symbols, {len(files)} files, {len(edges)} edges")
for e in ERRORS:
    print(f"    ERROR {e}")
if not edges:
    raise SystemExit("no edges exported - refusing to write a structurally empty graph")
PY

cat > "${OUT}/provenance.json" <<EOF
{
  "extractor": "gitnexus",
  "extractor_version": "$(tr -d '\n' < /gitnexus.version)",
  "corpus": "${CORPUS}",
  "repo_id": "${REPO_ID}",
  "subpath": "${SUBPATH}",
  "commit": "$(git -C "/corpus/${CORPUS}/${REPO_ID}" rev-parse HEAD 2>/dev/null || echo unknown)"
}
EOF

echo "==> wrote ${OUT}/graph.json ($(wc -c < "${OUT}/graph.json") bytes)"
