# Architecture

Why this is built the way it is. Written for someone who will be asked to
defend the design in a technical evaluation, or who is taking the repo as a
starting point.

Companion documents: [`DATA_MODEL.md`](DATA_MODEL.md) for what every label and
relationship means, [`DEMO.md`](DEMO.md) for the run of show,
[`README.md`](README.md) for measured results.

---

## The thesis in one paragraph

All three extractors ship a multi-repo story, and in all three it is federation
by switching — a registry file, a concatenated JSON, a `projectPath` parameter.
Each tool's parser only ever sees one repository's AST, so **no tool can emit an
edge that crosses a repository boundary**: the far endpoint is not in scope when
it runs. Neo4j is not a better backend for these tools. It is the layer where
the cross-repo edges get *created*, by traversal, once all repositories coexist
in one store. Blast radius, contract drift and org-level graph algorithms all
fall out of that one capability.

That claim was measured, not assumed — including against Graphify's own
`merge-graphs` command, which produces zero cross-repo edges.

---

## Control flow

```
fetch ─→ extract ─→ load ─→ enrich ─→ link ─→ analyze
  │         │          │        │        │        │
pinned   per tool,  canonical  recover  CYPHER   GDS,
 SHAs    filtered    schema,    what     ONLY    queries,
         copy       MERGE by     was              scoring
                    stable id   dropped
```

Each stage is independently re-runnable, and every stage is idempotent.

### 1. Fetch — `corpus/fetch.sh`

Clones at pinned SHAs, writes `corpus.lock.yaml`.

**Why pin.** Scores are version-specific. A dependency bump silently invalidates
the answer key, and a demo whose numbers move between runs is not a demo. The
lock file is committed; the checkouts are not.

**A bug worth knowing about, because it is a good illustration of the failure
mode this whole harness is built to catch.** The lock writer passed its rows to
Python through a pipe while `python -` was already taking its program from
stdin via a heredoc. The rows were silently discarded, every corpus locked as
`{}`, and refetches fell back to the branch ref. The lock file existed, was
committed, and guaranteed nothing — for the entire life of the project until
someone read the file. It now writes through a temp file and refuses to write an
empty lock. Same shape as the `llm-graph-builder` finding: **the artifact that
was supposed to pin the version was present and vacuous.**

### 2. Extract — `extractors/*/extract.sh`

Each tool runs **unmodified**, in its own container, against a **filtered copy**
of the repository.

**Why unmodified.** No forks. Tool upgrades don't break the harness, and a
prospect can swap in their own extractor by writing one mapper.

**Why a copy.** All three tools write state into the tree — `.gitnexus/`,
`.codegraph/`, `graphify-out/`. Never mutate the corpus.

**Why filtered.** This one is not hygiene, it is correctness. Mimir, Loki and
Tempo each vendor a complete copy of `dskit` — 8,194 vendored `.go` files in
mimir alone. Left in, the extractor resolves `github.com/grafana/dskit/ring` to
the *local vendored copy*, the edge becomes intra-repo, and the cross-repo
relationship silently disappears. The tool cannot tell that a directory in its
own checkout is another repository. Filtering also removes generated code, which
otherwise dominates centrality — your "god nodes" become generated deep-copy
functions.

### 3. Load — `loader/src/codekg/`

One mapper per extractor translates its native artifact into the canonical
schema. Everything is `MERGE` on a deterministic id.

### 4. Enrich — `enrich.py`

Recovers from source what a given artifact dropped. Needed for Graphify, not for
CodeGraph. Scored as a separate arm because it shares its method with the
oracle — see *Honest measurement* below.

### 5. Link — `cypher/10_link_cross_repo.cypher`

Pure Cypher. **The only stage that creates something that did not exist
upstream.** Four steps, most-certain first: reconcile package identity, bind
dangling references to real symbols, lift to symbol level, and materialise
file-level cross-repo imports.

### 6. Analyze

GDS (`20_gds.cypher`), the demo queries (`90_queries.cypher`), and the scoring
harness.

---

## The design decisions worth defending

### Stable identity is load-bearing

`sym:{repo}:{path}#{Owner.member}`

Everything downstream depends on this: idempotent reloads, the agreement matrix,
cross-repo resolution. Three specific choices:

**Readable, not hashed.** When a cross-repo edge looks wrong you want to see why
in the query result, not decode a sha1. The debuggability is worth the bytes.

**Signature excluded.** The three tools render signatures differently. Including
one would make the same function look like three different nodes, and the
agreement matrix would measure formatting rather than extraction. Cost: true
overloads collapse. Acceptable for Python and Go.

**`Owner.member` for methods, enforced across all three mappers.** They each
arrived at a different convention — CodeGraph `Owner::member`, Graphify
`.member()` with the owner only recoverable from its `method` edges, GitNexus
`Owner.member` but with a `#N` suffix on collisions. Normalising that suffix
alone moved symbol agreement from 30.9% to 63.7%. **An agreement matrix is only
as good as its identity normalisation, and a bad one fails quietly** — it
reports plausible-looking disagreement instead of erroring.

### Provenance on every node and edge

An `extractors` list that accumulates on `MERGE`, plus `method` and `confidence`
on derived edges.

This is why triangulation is a `GROUP BY` rather than a fourth pipeline, and why
"how do you know this edge is real?" is answerable with a property read. It cost
one design decision — accumulate rather than overwrite — and it is the single
highest-leverage choice in the schema.

### One database, not one per extractor

Comparison arms are filters over one load. Three extractors in three databases
would make agreement a join across databases; in one, it is `size(extractors)`.

### Reach for the right algorithm before the bigger traversal

Circular-dependency detection is the clearest case in the repo. The obvious
Cypher is `MATCH (a:File)-[:IMPORTS*2..n]->(a)`. Measured here: **n=5 returns in
18 ms, n=6 does not finish in 90 seconds** — the path space explodes, and each
cycle is reported once per rotation.

Strongly connected components answer the *prior* question — which nodes can be
in a cycle at all — in O(V+E). On this graph that is 15 ms and reduces 13,120
files to 135, a 97x cut, and it is **complete**: every cycle lies inside one SCC
by definition. Enumeration then runs only inside those components, where
`apoc.nodes.cycles` returns each cycle once rather than once per rotation.

Result: cycles up to length 11 in well under a second, where the direct form
could not reach length 6.

The general shape is worth carrying into any evaluation: **a graph algorithm
that answers a coarser question first is often the way to make the fine-grained
traversal affordable.** GDS is not only for analytics; here it is a query
planner.

### Cycle detection: classify the edge before you count it

Performance was the easy half. Correctness was not, and getting it wrong once is
the most instructive thing in this repo.

None of the three extractors record *where* in a file an import appears, so
three different things arrive as one `IMPORTS` edge: a top-level import that
executes, an `if TYPE_CHECKING:` import that **never** executes, and a
function-body import that runs on call and is usually a deliberate cycle-break.

Conflating them produced a confident, wrong finding: the neo4j-python-driver
reported as one 102-module component with an 11-hop cycle. Classified top-level
and facade-free, the driver has **zero** cycles — the component being quoted was
held together by two `TYPE_CHECKING` edges. The fallback number offered at the
time, "3 real modules," was the same error one level down: all three of those
edges are `TYPE_CHECKING` too.

So `internal_imports.py` derives intra-repo import edges from source with an AST
pass and labels each `toplevel` / `typing` / `deferred`. The projections filter
on it. Three consequences worth defending:

- **The finding is now falsifiable.** `make score-cycles` scores it against
  pylint's `cyclic-import`, a different tool with a different algorithm. On
  SQLAlchemy: **recall 1.000** — every one of the 77 modules pylint names, no
  misses.
- **The arms are a ladder, and the gap is the point.** 184 modules at pylint's
  own granularity (all contexts, facades included) → 159 top-level only → **22
  top-level and facade-free**. Each step removes a class of edge that is not a
  runtime cycle. pylint sits at the top of that ladder because it counts
  function-scoped imports — so it reports cycles the standard cycle-breaking
  idiom has already broken.
- **We name 107 modules pylint does not, and they are real.** Spot-checked end
  to end: `dialects/mssql/pymssql.py → base.py → mssql/__init__.py →
  pymssql.py`, all three edges top-level and confirmed in source. pylint appears
  not to model `from . import X` as executing the package's `__init__`.
- **It beats the standard tool on structure, not volume.** pylint reports
  overlapping *chains*, and the chain count is not even stable — 78, 74 and 72
  across identical runs, while the module set held at 77. SCC reports components
  and enumerates each cycle once. "How many distinct problems do I have" is not
  recoverable from pylint's output at all.

**Go's zero is a real check on the method.** The Go compiler rejects circular
package imports, so the correct answer is known in advance — and over **83,529
top-level Go import edges across 3,559 files, the pipeline returns zero cyclic
components.** A false-positive bug in cycle detection would almost certainly
surface somewhere in a corpus that size, so agreeing with a compile-time
guarantee at that scale is worth more than any single positive finding.

Worth knowing where those edges come from: Graphify emits almost no Go File→File
imports (mimir 6, loki 1, tempo 0) because it resolves Go imports to external
package references. GitNexus does emit them (loki 41,481, tempo 15,538). So the
Go check exists only because a second extractor was in the harness — which is
the argument for three extractors, arriving from an unexpected direction.

The transferable lesson: **an edge type that merges semantically different
relationships will produce results that are precise, fast, and wrong.** Ours did
for several days.

### Confidence graded by method, never flat

A Go module-prefix match is exact by construction and gets 1.0. A Python
symbol-name match that is ambiguous gets 0.5. A dynamic-dispatch guess is capped
so it can never outrank a resolved call in a blast-radius path. Presenting all
inferences at one confidence would be the easy thing and would make the whole
graph unfalsifiable.

---

## Why these datasets

**The principle:** you do not need synthetic repositories to get ground truth.
You need repositories whose cross-repo edges are **already declared in a
machine-readable artifact** — `go.mod`, `requirements.txt`, `pyproject.toml`.
That declaration is the answer key. The extractors never see manifests as a
graph — they parse source and hit an unresolvable import — so scoring against
them is a fair test rather than a leak.

**Corpus A — Neo4j's own Python packages.** `neo4j-python-driver` →
`langchain-neo4j` / `graph-data-science-client` / `neo4j-graphrag-python` →
`llm-graph-builder`.

- Recognisable to the audience, and it is *their* code.
- Small enough to iterate in seconds (~100–150k LOC).
- **You can eyeball whether the graph is correct.** On unfamiliar code you
  cannot distinguish a missing edge from a correct one. This is worth more than
  it sounds.
- A genuine three-hop chain with fan-in, not a flat pair.
- Python is the *hard* case for resolution, so it is an honest test.

**Corpus B — `grafana/dskit` into Mimir, Loki and Tempo.**

- Go import paths are fully qualified and name the publishing repo, so the
  answer key is **exact by construction and free**.
- One-to-many fan-out — the shape that lands visually.
- Proves the approach is not Python-specific.
- Contains the vendoring failure mode.

**Corpus C — `sqlalchemy/sqlalchemy`, one repo, for circular dependencies.**

Deliberately outside the cross-repo scoring: a single repo has no cross-repo
truth, and folding it into corpus A would pollute those numbers.

It is here because cycle ground truth arrives three independent ways, which is
the same standard the manifest oracle is held to:

1. **The maintainers say so, as a named subsystem.**
   `lib/sqlalchemy/util/preloaded.py` exists to resolve circular module imports
   at runtime. A module whose stated job is the problem is not arguable.
2. **pylint's `cyclic-import` reports them independently** — `make score-cycles`.
3. **Two distinct 7-module top-level, facade-free cycles in the ORM core**,
   confirmed against an AST scanner written separately from the loader.

The earlier exhibit used the neo4j-python-driver and was wrong — see *Cycle
detection* below.

---

## The three extractors

| | store | uniquely contributes | critically drops |
|---|---|---|---|
| **Graphify** 0.9.43 | `graph.json` | 36 languages; non-code sources (SQL schemas, Terraform, docs); EXTRACTED/INFERRED confidence tags | **the import module** — fatal for cross-repo |
| **CodeGraph** 1.5.0 | SQLite + FTS5 | `qualified_name`, `signature`, `is_exported`; an `unresolved_refs` table that keeps the module; incremental file-watcher sync; cross-language bridges | — |
| **GitNexus** 1.6.9 | LadybugDB (Kuzu fork) | 33 typed node tables; Process nodes (traced execution flows); Route nodes; per-edge numeric confidence; opt-in PDG/CFG | external imports entirely |

`goscan` is not a fourth competitor. It is a purpose-built `go/ast` pass, because
in Go the symbol appears in a *selector expression* rather than the import
statement, and resolving that needs real scope analysis.

### What Neo4j adds on top

1. **The cross-repo edges themselves** — nothing upstream can produce them.
2. **One store where all three coexist** — which is what makes agreement
   computable at all.
3. **Variable-depth traversal** — blast radius is a path query, not a join.
4. **GDS over the joined projection** — the tools run Leiden per repo; the
   answer is constrained by the projection, not the clustering.
5. **A governed, shared asset** — not a per-laptop index.

---

## Why three extractors rather than one

**It is how we found that extractor choice dominates.** With Graphify alone,
recall was 0.143 and the conclusion drawn was "the cross-repo layer cannot be a
pure ETL." CodeGraph proved that wrong — 0.960 from its artifact alone, because
it keeps the module path. A single-extractor demo would have shipped a
confidently wrong architectural conclusion, and anyone building on it would have
inherited the mistake.

**Triangulation is a capability, not a nicety.** Only 15% of call edges are
corroborated by all three. Demo one tool and you are implicitly asserting its
call graph is right. The vote is the only honest answer to "how much of this
should I believe," and it is what makes the graph decision-grade.

**It de-risks the tool choice.** The schema is the contract; extractors are
pluggable. That is a far easier architecture to approve than one betting on a
single young project.

**The honest concession:** for a 30-minute demo, one extractor tells the story
fine. Three is for the evaluation, not the pitch.

---

## Honest measurement

`make score` compares the graph's cross-repo edges against an answer key built
by `oracle.py`, which walks checked-out source and **never reads extractor
output**. That independence is the whole point: truth derived from the same
`ExternalRef` nodes an extractor produced would make recall 1.0 by construction.

Two arms are reported separately and must not be conflated:

- **`artifact:<tool>`** — only what that extractor's artifact supports. The
  independent measurement.
- **`unified+source`** — plus imports read from source. Shares its method with
  the oracle, so its recall is a **ceiling diagnostic**, not an accuracy claim.

The harness also has a limit worth stating: truth tuples are keyed
`(file, target_repo, symbol)` — symbol *name*, no package. A Go over-matching
bug once deleted ~20,000 wrong edges while the score moved by 3. **A metric only
catches errors its granularity can express.** That one was caught by looking at
the output.

---

## Extending it

**Adding an extractor** — write a module under `loader/src/codekg/mappers/`
exposing `load`, `provenance`, `files`, `symbols`, `calls`, `file_imports`,
`external_refs`, plus `ARTIFACT` and `EXTERNAL_REF_ANCHOR`; register it in
`cli.MAPPERS`. Roughly 150 lines. Nothing else changes.

**Adding a corpus** — add it to `corpus/corpus.yaml` with `publishes` declared
per repo. For a non-Python/Go ecosystem you also need a manifest parser in
`manifests.py` and a resolution rule in `10_link_cross_repo.cypher`.

**Adding a data source** (CVEs, incidents, deployments) — these attach to nodes
that already exist: CVEs to `:Package`, incidents and deployments to `:Repo` or
`:Symbol`. They are *easier* joins than what is already proven, because they are
ID-based rather than requiring name resolution.

---

## Known limits

- **No git history.** No extractor models commits and the corpus is
  shallow-cloned. Co-change coupling and ownership need a separate ingest.
- **Go method naming is not normalised.** ~20,853 graphify-only methods are a
  rendering divergence, not real disagreement — Go agreement figures are a lower
  bound.
- **Two false positives survive on corpus B**, from Go code shadowing a package
  name with a variable. Eliminating them needs real scope analysis.
- **Cycle detection runs on source-derived edges, not extractor edges.** The
  `context` label that makes it correct does not exist in any artifact, and the
  same AST pass resolves imports to files rather than module strings. `make
  enrich` is therefore a prerequisite for Q15; without it the cycle queries
  return nothing, which is a safe failure rather than a wrong answer.
- **Cycle scoring is Python-only.** Go needs no oracle — the compiler is the
  oracle, and it rejects package cycles outright.
- **Extraction is full re-index.** CodeGraph ships debounced incremental sync
  that the harness does not yet use.
