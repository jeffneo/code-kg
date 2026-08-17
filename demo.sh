#!/usr/bin/env bash
# Run-of-show for the codekg demo.
#
# Each beat is a separate function so you can run the whole thing or jump to
# one:   ./demo.sh            all beats, paced
#        ./demo.sh 3          just beat 3
#        ./demo.sh 2 3 4      a subset, in order
#        PAUSE=0 ./demo.sh    no waiting for keypresses (for a dry run)
#
# Output is formatted tables, not raw JSON. A live demo where the presenter
# reads JSON aloud is a demo the room stops watching.
set -uo pipefail
cd "$(dirname "$0")"

PAUSE="${PAUSE:-1}"
COMPOSE="docker compose"

# Demo targets. Both are load-bearing: dskit.InjectOrgID is the widest
# cross-repo fan-in in the corpus, and it is also the symbol betweenness
# independently ranks near the top - which is beat 7.
DSKIT_SYM='sym:repo:github.com/grafana/dskit:user/id.go#InjectOrgID'
DSKIT_REPO='repo:github.com/grafana/dskit'

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
dim()   { printf "\033[2m%s\033[0m\n" "$*"; }
say()   { printf "\n\033[36m▸ %s\033[0m\n" "$*"; }
hr()    { printf "\033[2m%s\033[0m\n" "────────────────────────────────────────────────────────────────"; }
wait_key() { [[ "$PAUSE" == "1" ]] && { printf "\n\033[2m  [enter]\033[0m"; read -r; } || echo; }

# Run one demo query and render it as a table.
#   q <query> [param=value ...]
q() {
  local name="$1"; shift
  local args=(query "$name")
  for p in "$@"; do args+=(--param "$p"); done
  $COMPOSE run --rm -T loader "${args[@]}" </dev/null 2>/dev/null \
    | grep -vE "^ Container|^time=" \
    | python3 -c '
import sys, json
try:
    rows = json.load(sys.stdin)
except Exception:
    print("  (no result - is the graph loaded? try: make stats)"); sys.exit(0)
if not rows:
    print("  (no rows)"); sys.exit(0)
cols = list(rows[0].keys())
def cell(v):
    if isinstance(v, list):
        v = ", ".join(str(x).rsplit("/", 1)[-1] for x in v)
    s = str(v)
    return s[:52] + "…" if len(s) > 53 else s
w = {c: max(len(c), max(len(cell(r.get(c))) for r in rows)) for c in cols}
print("  " + "  ".join(c.ljust(w[c]) for c in cols))
print("  " + "  ".join("-" * w[c] for c in cols))
for r in rows[:25]:
    print("  " + "  ".join(cell(r.get(c)).ljust(w[c]) for c in cols))
if len(rows) > 25:
    print(f"  … {len(rows) - 25} more rows")
'
}

beat1() {
  hr; bold "1. What is loaded"
  say "Nine real open-source repositories. Two ecosystems. Three independent extractors."
  dim "   Nothing synthetic - these are Neo4j's own Python packages and Grafana's Go stack."
  $COMPOSE run --rm -T loader stats </dev/null 2>/dev/null | grep -vE "^ Container|^time=" | sed 's/^/  /'
  wait_key
}

beat2() {
  hr; bold "2. The question, asked the way the tools answer it"
  say "\"I'm changing dskit's InjectOrgID. What breaks?\""
  dim "   Single-repo scope - this is what GitNexus, Graphify or CodeGraph each return."
  q q2 "changed=$DSKIT_SYM"
  wait_key
}

beat3() {
  hr; bold "3. The same question against the joined graph"
  q q1 "changed=$DSKIT_SYM"
  say "12 files becomes 266, across three repositories the single-repo answer never mentions."
  dim "   Those edges cannot exist in any one tool: the other end was out of scope when it ran."
  wait_key
}

beat4() {
  hr; bold "4. Evidence, not a number"
  say "Every impacted repo, with the shortest concrete path and real file:line."
  q q3 "changed=$DSKIT_SYM"
  wait_key
}

beat5() {
  hr; bold "5. So which tests actually cover it?"
  q q16 "changed=$DSKIT_SYM"
  say "The practical follow-through from blast radius: what to run before merging."
  dim "   A mimir test covering a dskit change is invisible to an extractor indexing either alone."
  wait_key
}

beat6() {
  hr; bold "6. A defect this found in Neo4j's own code"
  q q8
  say "llm-graph-builder imports GraphDatabase, TransientError and neo4j.time directly,"
  say "across 4 files - and never declares neo4j in requirements.txt."
  dim "   It works only because langchain-neo4j pulls the driver in transitively."
  dim "   Needs manifests AND source together: an SCA tool has one, a code graph the other."
  wait_key
}

beat7() {
  hr; bold "7. Circular dependencies"
  q q15
  say "An 11-hop import chain in the driver. Naive search cannot even reach length 6."
  dim "   *2..5 returns in 18ms; *2..6 does not finish in 90 seconds. SCC first, in O(V+E),"
  dim "   then enumerate only inside components. That is the transferable technique."
  say "Do NOT sell the 102 as a defect - 23 of those are __init__.py."
  dim "   Strip the package facade and the real cycle is 3 modules. Say so first;"
  dim "   being the one who explains why the big number is benign is worth more."
  wait_key
}

beat8() {
  hr; bold "8. How much of this should you believe?"
  say "Three extractors vote on every edge. Agreement is a property read, not a second pipeline."
  q q10 "corpus=a"
  say "Only ~15% of call edges are corroborated by all three."
  dim "   Trust one tool's call graph and most of it is uncorroborated."
  wait_key
}

beat9() {
  hr; bold "9. The same blast radius, filtered by evidence"
  q q11 "changed=sym:repo:github.com/neo4j/graph-data-science-client:src/graphdatascience/graph/graph_api.py#Graph.name"
  say "That spread is the honest uncertainty in the answer."
  dim "   It only exists once a third opinion is in the graph."
  wait_key
}

beat10() {
  hr; bold "10. Subsystems that cross repository boundaries"
  say "GitNexus and Graphify both run Leiden - per repo, so only intra-repo communities."
  say "Same algorithm over the joined graph answers a question they cannot express."
  q q12
  wait_key
}

beat11() {
  hr; bold "11. The estate's real chokepoints"
  q q13
  say "InjectOrgID ranks near the top by betweenness."
  dim "   Beat 3 ranked it first by cross-repo fan-in. Two unrelated measures, same symbol."
  wait_key
}

beat12() {
  hr; bold "12. What it does NOT know"
  say "Every dangling reference, and whether it is a resolution miss or genuine third-party."
  q q5
  say "Show this unprompted. A prospect who has to ask twice stops believing the rest."
  wait_key
}

BEATS=(beat1 beat2 beat3 beat4 beat5 beat6 beat7 beat8 beat9 beat10 beat11 beat12)

if [[ $# -eq 0 ]]; then
  for b in "${BEATS[@]}"; do $b; done
  hr; bold "Then: Bloom. See DEMO.md for the perspective and the search phrases."
else
  for n in "$@"; do
    idx=$((n - 1))
    [[ $idx -ge 0 && $idx -lt ${#BEATS[@]} ]] && ${BEATS[$idx]} || echo "no beat $n (1-${#BEATS[@]})"
  done
fi
