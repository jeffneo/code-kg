#!/usr/bin/env bash
# Clone corpus repos at pinned commits.
#
# First run:  resolves each `ref` to a SHA and writes corpus.lock.yaml.
# Later runs: fetches the exact SHA from the lock file, ignoring `ref`.
#
# Usage:
#   ./fetch.sh a            # corpus A, core tier
#   ./fetch.sh b --full     # corpus B including loki + tempo
#   ./fetch.sh a --relock   # re-resolve refs to new SHAs (invalidates ground truth)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${HERE}/src"
LOCK="${HERE}/corpus.lock.yaml"
CONFIG="${HERE}/corpus.yaml"

# Hermetic python. Don't require pyyaml in the host interpreter - uv fetches it
# into a throwaway env. Falls back to plain python3 if uv isn't installed.
if command -v uv >/dev/null 2>&1; then
  PY=(uv run --quiet --with pyyaml python3)
else
  PY=(python3)
  python3 -c "import yaml" 2>/dev/null || {
    echo "need either uv (preferred) or pyyaml in python3" >&2
    exit 1
  }
fi

CORPUS="${1:-}"
shift || true
TIER="core"
RELOCK="false"
for arg in "$@"; do
  case "$arg" in
    --full)   TIER="full" ;;
    --relock) RELOCK="true" ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$CORPUS" ]]; then
  echo "usage: $0 <corpus-id> [--full] [--relock]" >&2
  echo "available corpora:" >&2
  "${PY[@]}" - "$CONFIG" >&2 <<'PY'
import sys, yaml
for k, v in yaml.safe_load(open(sys.argv[1]))["corpora"].items():
    print(f"  {k}: {v['name']} ({len(v['repos'])} repos)")
PY
  exit 2
fi

mkdir -p "$SRC_DIR"

# Emit "id<TAB>url<TAB>ref<TAB>subpath" for repos in this corpus at or below TIER.
read_repos() {
  "${PY[@]}" - "$CONFIG" "$CORPUS" "$TIER" <<'PY'
import sys, yaml
config, corpus, tier = sys.argv[1], sys.argv[2], sys.argv[3]
doc = yaml.safe_load(open(config))
if corpus not in doc["corpora"]:
    sys.exit(f"no such corpus: {corpus}")
wanted = {"core"} if tier == "core" else {"core", "full"}
for r in doc["corpora"][corpus]["repos"]:
    if r.get("tier", "core") in wanted:
        print("\t".join([r["id"], r["url"], r.get("ref", "main"), r.get("subpath", "")]))
PY
}

locked_sha() {
  [[ -f "$LOCK" ]] || return 1
  "${PY[@]}" - "$LOCK" "$CORPUS" "$1" <<'PY'
import sys, yaml
try:
    doc = yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    sys.exit(1)
sha = (doc.get(sys.argv[2]) or {}).get(sys.argv[3], {}).get("sha")
if not sha:
    sys.exit(1)
print(sha)
PY
}

declare -a RESOLVED=()

while IFS=$'\t' read -r id url ref subpath; do
  dest="${SRC_DIR}/${CORPUS}/${id}"
  sha=""
  if [[ "$RELOCK" != "true" ]]; then
    sha="$(locked_sha "$id" || true)"
  fi

  if [[ -d "${dest}/.git" ]]; then
    echo "==> ${id}: already present"
  elif [[ -n "$sha" ]]; then
    echo "==> ${id}: fetching pinned ${sha:0:12}"
    mkdir -p "$dest"
    git -C "$dest" init -q
    git -C "$dest" remote add origin "$url"
    # GitHub allows fetching an arbitrary SHA (allowAnySHA1InWant).
    git -C "$dest" fetch -q --depth 1 --filter=blob:none origin "$sha"
    git -C "$dest" checkout -q FETCH_HEAD
  else
    echo "==> ${id}: cloning ${ref}"
    git clone -q --depth 1 --filter=blob:none --branch "$ref" "$url" "$dest"
  fi

  head="$(git -C "$dest" rev-parse HEAD)"
  RESOLVED+=("${id}"$'\t'"${url}"$'\t'"${head}"$'\t'"${subpath}")
  echo "    ${id} @ ${head:0:12}${subpath:+  (subpath: ${subpath})}"
done < <(read_repos)

# Merge resolved SHAs back into the lock file, preserving other corpora.
printf '%s\n' "${RESOLVED[@]}" | "${PY[@]}" - "$LOCK" "$CORPUS" <<'PY'
import sys, yaml, datetime, os
lock_path, corpus = sys.argv[1], sys.argv[2]
doc = {}
if os.path.exists(lock_path):
    doc = yaml.safe_load(open(lock_path)) or {}
entry = doc.setdefault(corpus, {})
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        continue
    rid, url, sha, subpath = line.split("\t")
    entry[rid] = {"url": url, "sha": sha, "subpath": subpath or None}
doc["_locked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
with open(lock_path, "w") as fh:
    yaml.safe_dump(doc, fh, sort_keys=True, default_flow_style=False)
print(f"\nlock written: {lock_path}")
PY
