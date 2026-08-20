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
  hr; bold "7. Circular dependencies  (corpus C - SQLAlchemy)"
  q q15
  say "Four cyclic components, 22 modules. The two biggest are 7 modules each in the ORM core."
  dim "   Open with THEIR evidence: sqlalchemy ships util/preloaded.py, a module whose stated"
  dim "   job is resolving circular module imports at runtime. Nobody writes that for fun."
  say "The substance is classification. One IMPORTS edge means three different things:"
  dim "   toplevel runs on import; 'if TYPE_CHECKING:' NEVER runs; in-function runs on call"
  dim "   and is usually a deliberate cycle-break. Only the first is a cycle you can hit."
  say "Read the ladder down: 184 modules counting everything -> 159 toplevel -> 22 facade-free."
  dim "   The top number is not wrong, it is not actionable. 22 is what you hand a team."
  say "Checkable on the spot: 'make score-cycles CORPUS=c' runs pylint as an oracle."
  dim "   Recall 1.000 - all 77 modules pylint names. We name 107 more; spot-verified in"
  dim "   source. And pylint's chain COUNT is unstable: 78, 74, 72 on identical runs."
  dim "   Chains cannot answer 'how many distinct problems'. Components can."
  say "Go returns zero across 83,529 toplevel edges - the answer the compiler guarantees."
  dim "   That is the control. Perf note: *2..5 is 18ms, *2..6 does not finish in 90s;"
  dim "   SCC is O(V+E) first, then enumerate only inside tiny components."
  say "If the driver comes up: zero runtime cycles. An earlier build of this demo claimed"
  dim "   102 entangled modules and an 11-hop cycle for it. That was WRONG - the component"
  dim "   was held together by two TYPE_CHECKING edges that never execute. Own it; being"
  dim "   the one who found and fixed that is worth more than the number ever was."
  wait_key
}

beat7b() {
  hr; bold "7b. The cycle that crosses a RELEASE boundary  (corpus D - Airflow)"
  q q17
  say "Everything so far is a better answer to a question the tools can ask."
  dim "   This is one they CANNOT express: when either side is parsed, the other end"
  dim "   of the edge is out of scope. No single-repo tool can produce this edge."
  say "apache-airflow-core and apache-airflow-task-sdk REQUIRE EACH OTHER on PyPI:"
  dim "   airflow-core/pyproject.toml:154  apache-airflow-task-sdk<1.5.0,>=1.4.0"
  dim "   task-sdk/pyproject.toml:51       apache-airflow-core<3.5.0,>=3.4.0"
  say "An intra-repo cycle is a refactor. This is a RELEASE DEADLOCK - neither can"
  dim "   ship without a compatible version of the other already on PyPI. Somebody"
  dim "   pays for that every release, and no single CI job ever sees it."
  say "The asymmetry is the finding: 132 import edges one way, 7 the other."
  dim "   Cut the 7 and a mutual dependency becomes a clean layering."
  say "But it is FIVE, not seven - and saying why is the credibility play."
  dim "   Two of the seven sit in 'except ModuleNotFoundError:' with working"
  dim "   fallbacks; the SDK already treats core as optional there. Handing a"
  dim "   maintainer seven problems when their own code handles two loses the room."
  say "declared_both_ways=true is corroboration from a different source: the code"
  dim "   finding (source AST) and the manifests (pyproject.toml) agree, and no"
  dim "   extractor reads manifests as a graph."
  say "If asked 'isn't this one repo?' - yes. Answer it head-on."
  dim "   Two distributions in apache/airflow. They version, publish and release"
  dim "   independently, which is what creates the deadlock. Same shape your"
  dim "   audience meets in a corporate polyrepo. Say 'distribution', not 'repo'."
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

BEATS=(beat1 beat2 beat3 beat4 beat5 beat6 beat7 beat7b beat8 beat9 beat10 beat11 beat12)

if [[ $# -eq 0 ]]; then
  for b in "${BEATS[@]}"; do $b; done
  hr; bold "Then: Bloom. See DEMO.md for the perspective and the search phrases."
else
  for n in "$@"; do
    idx=$((n - 1))
    [[ $idx -ge 0 && $idx -lt ${#BEATS[@]} ]] && ${BEATS[$idx]} || echo "no beat $n (1-${#BEATS[@]})"
  done
fi
