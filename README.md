# codekg — multi-repo code knowledge graph on Neo4j

A harness for evaluating what a **unified, cross-repo** code knowledge graph in Neo4j
can answer that single-repo extractors — [GitNexus](https://github.com/abhigyanpatwari/GitNexus),
[Graphify](https://github.com/Graphify-Labs/graphify),
[CodeGraph](https://github.com/colbymchenry/codegraph) — structurally cannot.

## The premise

All three tools ship a multi-repo story, and in all three it is **federation by
switching, not a joined graph**: a registry file, a concatenated JSON, a
`projectPath` parameter. Each tool's parser only ever sees one repo's AST, so
**no tool can emit an edge that crosses a repo boundary** — the other end simply
isn't in scope when the extractor runs.

Neo4j isn't a better backend for these tools. It's the layer where the
cross-repo edges get *created*, by traversal, once all the repos coexist in one
store. Everything downstream — blast radius, contract drift, org-level GDS —
falls out of that one capability.

## Layout

```
corpus/          repo selection, pinned commits, clone script
extractors/      graphify (all languages) + goscan (Go AST, required for Go)
loader/          canonical schema, mappers, Neo4j write path
cypher/          constraints, the cross-repo pass, demo queries
```

The interesting file is [`cypher/10_link_cross_repo.cypher`](cypher/10_link_cross_repo.cypher).
Everything else is translation of work the extractors already did.

## Corpora

**A — `neo4j-python`** (default). Neo4j's own Python dependency DAG:
`neo4j-python-driver` → `langchain-neo4j` / `graph-data-science-client` /
`neo4j-graphrag-python` → `llm-graph-builder`. Two hops with fan-in, ~100–150k LOC,
fast loop, and you can validate the graph by inspection — which matters more than
it sounds, because on an unfamiliar corpus you can't tell a missing edge from a
correct one.

**B — `grafana-go`**. `grafana/dskit` consumed by Mimir, Loki, Tempo. Go import
paths are fully qualified and name the publishing repo, so the answer key is
exact and free. One-to-many fan-out makes the blast-radius result land visually.
`--full` adds Loki and Tempo and turns a fast loop into a slow one.

**C — `sqlalchemy-cycles`**. One repo, one job: circular dependencies that are
real and provable. Kept out of the cross-repo scoring on purpose — a single repo
has no cross-repo truth, and folding it into corpus A would pollute those
numbers. SQLAlchemy earns the slot because the ground truth arrives three
independent ways: the maintainers ship
[`lib/sqlalchemy/util/preloaded.py`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/util/preloaded.py)
whose stated job is resolving circular module imports at runtime; pylint's
`cyclic-import` finds them independently (`make score-cycles CORPUS=c`); and an
AST scanner written separately from the loader agrees on the members.

Ground truth comes from dependency manifests, not from hand-labelling. The
extractors never see manifests as a graph — they parse source and hit an
unresolvable external import — so scoring against them is a fair test. Corpus C
follows the same rule with a different oracle: pylint, which the loader never
reads and which implements a different algorithm.

## Documents

| file | for |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | why it is built this way — control flow, design decisions, extension points |
| [`DATA_MODEL.md`](DATA_MODEL.md) | every node label, relationship type and property, and what it means |
| [`DEMO.md`](DEMO.md) | run of show, talking points, failure recovery |

## Demo

```bash
./demo.sh              # ten beats, paced, formatted tables
./demo.sh 2 3 4        # just the core reveal
PAUSE=0 ./demo.sh      # dry run, ~12s
```

[`DEMO.md`](DEMO.md) is the run of show: pre-flight checks, per-beat talking
points and timings, the Bloom close-out, live failure recovery, and a
"what not to claim" section drawn from the measured boundaries.

A shareable summary of the findings is published as an artifact —
regenerate it from [`findings.html`](findings.html).

## Prerequisites — two license keys, assumed present

This harness **assumes two Neo4j license keys sit at the repo root**. Both are
referenced by path, never copied into an image, and both are gitignored
(`.gitignore` excludes `*.license` and `.env`). Neither is in version control,
so a fresh clone needs them supplied out-of-band:

| file | what it unlocks | without it |
|---|---|---|
| `gds.license` | GDS Enterprise | The plugin still loads but runs community-tier, which caps graph projections and blocks several algorithms |
| `nes.license` | Neo4j Enterprise Studio (Query + Bloom + Dashboards) | `make nes` fails with a clear message |

Override the locations with `GDS_LICENSE_PATH` / `NES_LICENSE_PATH` in `.env`.

Verify GDS Enterprise is actually active — the plugin reports a version either
way, so version alone proves nothing:

```cypher
CALL gds.license.state() YIELD isLicensed, details RETURN isLicensed, details;
```

## Run it

```bash
cp .env.example .env
make up                      # Neo4j + constraints
make corpus CORPUS=a         # clone and pin
make sync                    # copy into the docker volume
make extract CORPUS=a        # graphify over each repo
make goscan CORPUS=b         # Go corpora only - AST scan (see below)
```

```bash
make load CORPUS=a
make enrich CORPUS=a         # source-derived imports the artifact omits (see below)
make link                    # the cross-repo pass
make score CORPUS=a          # precision/recall vs source-derived truth
make stats
```

To re-verify an extractor's artifact shape after a version bump:

```bash
make inspect REPO=neo4j-python-driver
```

## Measured result on corpus A

Verified end to end against **Neo4j 2026.07.1 enterprise** (APOC 2026.07.1, GDS
2026.07.0), graphify 0.9.43, and the pinned commits in `corpus/corpus.lock.yaml`.
All seven queries in `cypher/90_queries.cypher` execute clean, no deprecations.

| stage | result |
|---|---|
| extraction | 5 repos, ~21k nodes / ~71k edges, no API key |
| load | 1,696 files, 13,123 symbols, 17,643 intra-repo calls, 279 distinct dangling refs across 5,553 use sites |
| `DEPENDS_ON_REPO` | **6 — exactly the 6 manifest ground-truth edges** |
| `RESOLVES_TO` | 24 dangling imports bound to sibling-repo symbols |
| `CALLS_CROSS_REPO` | 45 symbol-level cross-repo call edges |

Blast radius on `neo4j.AsyncDriver`:

```
Q2  single-repo scope   →   2 impacted symbols
Q1  joined graph        →  20 impacted symbols, 18 of them in langchain-neo4j
```

Those 18 are invisible to any single-repo extractor by construction.

## Scoring

`make score` compares the graph's cross-repo edges against an answer key built
by [`oracle.py`](loader/src/codekg/oracle.py), which walks the checked-out source
with Python's `ast` and never reads extractor output. That independence is the
whole point: truth derived from the same `ExternalRef` nodes Graphify produced
would make recall 1.0 by construction.

Measured on corpus A — 126 ground-truth cross-repo edges:

| arm | predicted | TP | FP | precision | recall |
|---|---|---|---|---|---|
| raw | 0 | 0 | 0 | n/a | 0.000 |
| single-tool | 0 | 0 | 0 | n/a | 0.000 |
| manifest-only | 0 | 0 | 0 | n/a | 0.000 |
| **unified** | 18 | 18 | 0 | **1.000** | **0.143** |
| unified+source | 99 | 99 | 0 | 1.000 | 0.786 |

The `single-tool` row is **verified from the artifacts**, not assumed — the
scorer counts edges whose endpoints belong to different repos and finds zero.

**Precision is 1.000 across every arm.** The pipeline never invents an edge.
That's the strongest claim available here and the one to lead with.

### Read the two unified arms differently

`unified` uses only what the extractor's artifact supports. It is the honest,
independently-measured number.

`unified+source` adds imports read from source, and shares its method with the
oracle — so its recall is a **ceiling diagnostic**, not an accuracy claim. Quote
`unified` when asked how well this works; quote `unified+source` when asked how
much is reachable.

### Why the gap, and what it means architecturally

Recall of 0.143 is a **ceiling in Graphify's output, not a mapper bug** —
measured, not assumed. Of 103 symbols `langchain-neo4j` imports from sibling-repo
packages, 64 appear nowhere in its `graph.json`, and only 6 appear in the
external-node form an ETL can consume. Graphify does not node-ify module-level
constants at all.

The conclusion that follows applies **to Graphify specifically**, not to
extractors in general — see the CodeGraph section below, which overturns the
general version. Roughly 94% of the cross-repo import surface is not in
graphify's artifact. [`enrich.py`](loader/src/codekg/enrich.py) adds a
source-side pass, which lifts reachable recall to 0.96.

The residual 27 misses fail on the *target* side, for the same reason: module-level
constants (`ADD_MESSAGE_QUERY`, `BASE_ENTITY_LABEL`) aren't symbols in the
publishing repo's graph either, so there's nothing to resolve to.

## Two extractors: agreement, and which one to pick

```bash
make extract-codegraph CORPUS=a
docker compose run --rm -T loader load a --extractor codegraph
make link && make score CORPUS=a
```

[CodeGraph](https://github.com/colbymchenry/codegraph) 1.5.0 is the second
extractor. Its store is SQLite, so the ETL is a `SELECT` rather than a parse.

### Extractor choice dominates everything else

| arm | predicted | TP | FP | precision | recall |
|---|---|---|---|---|---|
| artifact:**codegraph** | 121 | 121 | 0 | 1.000 | **0.960** |
| artifact:graphify | 32 | 32 | 0 | 1.000 | 0.254 |
| unified+source | 121 | 121 | 0 | 1.000 | 0.960 |

**CodeGraph's artifact alone reaches 0.960** — matching the source-derived
ceiling exactly. One design decision explains the gap: CodeGraph keeps the
module path on its `import` nodes (`qualified_name = "neo4j_graphrag.schema"`,
plus the literal statement in `signature`), which is precisely the field
graphify discards.

So with the right extractor, the cross-repo layer **is** a pure ETL, and
`enrich.py` becomes unnecessary on corpus A. That corrects the stronger claim
made earlier in this README: the "cannot be a pure ETL" conclusion was true of
graphify, not of extractors in general.

### Three extractors, and the majority vote

[GitNexus](https://github.com/abhigyanpatwari/GitNexus) 1.6.9 is the third.
`make extract-gitnexus CORPUS=a`, then load with `--extractor gitnexus`.

Its store is an embedded LadybugDB (Kuzu fork), not a portable file, so the
export runs through GitNexus's own `cypher` command —
see [`extractors/gitnexus/extract.sh`](extractors/gitnexus/extract.sh).
Like graphify and unlike codegraph, it drops external imports entirely: its
IMPORTS edges are File→File and internal-only, so its cross-repo arm is zero and
`external_refs()` returns nothing rather than inventing something.

`query q9 --param corpus=a` / `q10`:

| votes | symbols | call edges |
|---|---|---|
| **all three** | 13,505 (**63.7%**) | 5,335 (**15.0%**) |
| two of three | 1,659 (7.8%) | 11,987 (33.7%) |
| single extractor | 6,042 (28.5%) | 18,262 (51.3%) |

High agreement on *what is a node*; heavy divergence on *what calls what*. Only
**15% of call edges are corroborated by all three**, and a bare majority (2 of 3)
still leaves over half single-sourced.

What remains gitnexus-only is categorical, not noise: 2,564 `property` and 1,888
`variable` nodes — class attributes and locals the other two don't emit as
symbols at all.

Read the vote as a trust signal, not a correctness measure: tools can agree and
all be wrong. The Go package-scoping bug below is a live example.

### Agreement on Go (corpus B, full tier)

| votes | symbols | call edges |
|---|---|---|
| all three | 31,478 (23.8%) | 37,387 (17.6%) |
| two of three | 34,768 (26.3%) | 40,989 (19.3%) |
| single extractor | 65,803 (49.9%) | 133,495 (63.0%) |

Lower agreement than Python across the board, and **two known artifacts inflate
the single-source bucket** — read these numbers with both in mind:

- 20,853 graphify-only *methods*. Go methods have receivers
  (`func (b *Backoff) Ongoing()`), and graphify renders them differently again
  from the other two. This is the same identity divergence as the `#N` suffix,
  not yet normalised for Go.
- Shell-script symbols. Graphify indexes `.sh` files; the other two do not. That
  one is genuine coverage, not an artifact.

Fixing the Go method naming would move agreement up materially. Until it is
fixed, treat the Go agreement figures as a lower bound.

### Q11 — what the third vote actually buys

Blast radius answered three times at different evidence thresholds
(`graphdatascience.Graph.name`):

```
corroborated by all three     1 files   ( 0%)
two of three                182 files   (49%)
single extractor - verify   191 files   (51%)
                            ---
TOTAL                       374 files
```

Trust one tool and you act on 374 files with half the evidence uncorroborated.
That spread is the honest uncertainty in the answer, and it only exists once a
third opinion is in the graph.

### The normalization tax, paid three times

Agreement is only meaningful because all three mappers render identity
identically. None of them started that way:

| | method identity |
|---|---|
| CodeGraph | `GraphCypherQAChain::__init__` |
| Graphify | `.__init__()` — leading dot, no owning class |
| GitNexus | `GraphCypherQAChain.__init__` — correct, but `#1`-suffixed on collisions |

Three tools, three conventions for the same method. Graphify's owner is
recoverable only from its `method` edges; GitNexus appends a `#N` disambiguator
to same-named symbols in a file.

That suffix alone was worth ~5,400 phantom rows: 5,547 GitNexus symbols carried
it, **every one matched nothing**, and 5,391 matched an existing symbol once
stripped. Fixing it moved symbol agreement from 30.9% to 63.7% and call-edge
agreement from 6.7% to 15.0% — the measurement was mostly reporting formatting
until then.

All three now emit `Owner.member`, documented as the IDENTITY CONTRACT in
[`mappers/codegraph.py`](loader/src/codekg/mappers/codegraph.py). Normalising
also lifted graphify's own recall from 0.143 to 0.254.

**The lesson for the demo:** an agreement matrix is only as good as its identity
normalisation, and a bad one fails *quietly* — it reports plausible-looking
disagreement instead of erroring.

## Corpus B — Go, and a sharper version of the same story

```bash
make corpus CORPUS=b && make sync && make goscan CORPUS=b
make extract CORPUS=b && make load CORPUS=b && make enrich CORPUS=b && make link
make score CORPUS=b
```

Add `FULL=1` for the full tier (loki + tempo). Extraction of all four repos
takes under two minutes; the vendor filter does the heavy lifting.

**4,979 ground-truth cross-repo edges** across dskit → mimir / loki / tempo
(1,281 consumer files: mimir 603, loki 503, tempo 175):

All three extractors run on corpus B. 4,979 ground-truth edges:

| arm | predicted | TP | FP | precision | recall |
|---|---|---|---|---|---|
| raw / single-tool / manifest-only | 0 | 0 | 0 | n/a | 0.000 |
| graphify-merge | 0 | 0 | 0 | n/a | 0.000 |
| artifact:graphify | 0 | 0 | 0 | n/a | 0.000 |
| artifact:gitnexus | 0 | 0 | 0 | n/a | 0.000 |
| **artifact:codegraph** | 2670 | 2668 | 2 | 0.999 | **0.536** |
| unified+source | 4586 | 4584 | 2 | 0.9996 | 0.921 |

**Two of three extractors score zero on Go; CodeGraph scores 0.536.** In Python,
`from neo4j import Driver` names the symbol in the import statement, so an
extractor at least *sees* it. In Go, `import ".../dskit/ring"` names only the
package — the symbol appears later as the selector `ring.Ring`. Graphify and
GitNexus keep nothing that connects the two.

CodeGraph does: its `unresolved_refs.reference_name` holds the whole selector
(`user.InjectOrgID`), and its import nodes give file → module path. Joining the
selector head against the file's imports reconstructs (module, symbol) from the
artifact alone. So "on Go the artifact-only arm scores zero" is a fact about two
specific tools, not about Go.

That is why [`extractors/goscan`](extractors/goscan/main.go) exists: a small
`go/ast` program that resolves selector expressions against each file's import
block. It's the Go equivalent of what Python's stdlib `ast` gives us for free,
and it runs over both repos in under two seconds.

### Vendoring — the finding to lead with on this corpus

**mimir vendors dskit.** A complete copy of the sibling repo sits at
`mimir/vendor/github.com/grafana/dskit`, among 8,194 vendored `.go` files.

Left in, a single-repo extractor resolves `github.com/grafana/dskit/ring` to the
*local vendored copy*. The edge becomes intra-repo and the cross-repo
relationship disappears — the tool cannot tell that a directory in its own
checkout is another repository. It also inflates the graph roughly 5x.

Extraction now runs from a filtered copy (see
[`extractors/graphify/extract.sh`](extractors/graphify/extract.sh)), which took
mimir from 15,531 files to 5,275. Keep the exclusions in sync with
`exclude.globs` in `corpus/corpus.yaml`.

### The blast-radius demo on corpus B

`dskit.InjectOrgID`:

```
Q2  single-repo scope   →   12 files
Q1  joined graph        →  125 files, 113 of them in mimir
```

A single-repo tool understates this change by about **10x**. Q4 ranks the
load-bearing surface: `InjectOrgID` used from 113 mimir files,
`StartAndAwaitRunning` from 91, `grpcclient.Config` from 86.

At the full tier Q4 becomes a genuine one-to-many fan-out:

```
user.id.InjectOrgID              3 repos   254 sites
services.service.Service         3 repos   158 sites
services.StopAndAwaitTermination 3 repos   151 sites
flagext.register.DefaultValues   3 repos   147 sites
```

### The first false positives in the project

Adding CodeGraph to corpus B produced **89 false positives** — the first this
harness has ever recorded, after every prior arm scored precision 1.000.

All 89 were the same shape: `Ongoing`, `Wait`, `Err`, `Reset` — methods on
dskit's `backoff.Backoff`, not package-level symbols. The cause is a common Go
idiom where a variable shadows the package it came from:

```go
backoff := backoff.New(ctx, cfg)   // variable named after the package
backoff.Ongoing()                  // selector head is the variable now
```

goscan rejects these with go/ast's `ident.Obj != nil` scope check. CodeGraph's
`unresolved_refs` carries no scope information, so the reconstruction reads the
head as a package and binds to a method.

The fix is a Go language rule rather than a heuristic: **a cross-package
reference is `pkg.Symbol`, and a method can never be reached that way** — only
package-level funcs, types, vars and consts can. Excluding method-kind targets
in the Go linker took 89 false positives to 2.

The two survivors (`HTTP` in a mimir test, `CipherSuites` in tempo) are the same
shadowing pattern where the target happens to be a package-level symbol too, and
need real scope analysis to eliminate.

**Correction to a claim made earlier in this README:** precision is no longer
1.000 everywhere. It is 1.000 on corpus A across all arms, and 0.999 on corpus B
for the CodeGraph arm. Quote it that way.

### The bug the full tier exposed, and what it says about the metric

The first full-tier run showed nine different dskit symbols with *identical*
counts — 218 sites, 3 repos, each. Not a coincidence: every dskit package
defines its own `Config`, and the Go linker matched on symbol **name** within
the publishing repo. A single `ring.Config` reference resolved to all nine
`Config` types in dskit, including `backoff/backoff.go` and `grpcclient/`.

Go's import path names the exact package, and we were discarding it. The linker
now strips the publisher's module prefix to get the package directory
(`github.com/grafana/dskit/kv/consul` → `kv/consul`) and constrains the target's
path to it. `ring.Config` now resolves to exactly one symbol.

**The scoring harness never saw this.** Predicted went 4319 → 4316 and recall
stayed 0.867 while roughly 20,000 wrong edges were deleted. Truth tuples are
keyed `(importing_file, target_repo, symbol)` — symbol *name*, no package — so
both the oracle and the linker were wrong in the same direction and the metric
scored it as correct.

That is the honest limit of this harness, and it generalises: a metric can only
catch errors its granularity can express. The over-match was caught by *looking
at the output*, not by the score. Budget for both.

## Org-level GDS

```bash
make gds
```

Requires GDS Enterprise. 166,375 nodes and 812,132 relationships project and
run all four algorithms in **8 seconds**.

The projection is deliberately heterogeneous — Symbol *and* File. Symbol→Symbol
edges alone barely cross repo boundaries, because only CodeGraph's artifact
supports symbol-level cross-repo calls; the bulk of the cross-repo signal is
`IMPORTS_CROSS_REPO`, which runs File→Symbol. Undirected throughout, since
Leiden and articulationPoints require it.

### Q12 — subsystems that cross repo boundaries

GitNexus and Graphify both run Leiden. Per repo. Same algorithm, but a per-repo
projection can only ever return intra-repo communities — the answer is
constrained by the projection, not by the clustering.

Over the joined graph: 6,305 communities at modularity 0.879, of which **24 span
more than one repo**, covering ~94,000 symbols:

```
repos spanned   communities   symbols
      4               3        12,893
      3               9        37,567
      2              12        43,530
```

The largest spans all four Go repos (9,158 symbols across mimir, dskit, loki,
tempo). Another spans the Python chain — driver, langchain-neo4j, graphrag.
These are the organisation's real subsystems, irrespective of how the code is
filed into repositories.

### Q13 — org-level chokepoints

Betweenness over the whole estate. `neo4j.Driver` at 4.79M, `dskit.InjectOrgID`
at 3.24M.

Worth showing as corroboration rather than coincidence: **Q4 independently ranks
`InjectOrgID` first by cross-repo fan-in.** Two unrelated measures, same answer.

### Q14 — fragile seams

Articulation points — nodes whose removal splits the graph — filtered to those
consumed by two or more repos. Every one on the loaded corpora sits inside
dskit, led by `flagext.StringSliceCSV` (3 consuming repos).

Two gotchas worth knowing: `gds.articulationPoints` writes an **INTEGER (0/1),
not a boolean**, so predicates need `> 0`; and PageRank does not converge in 20
iterations on this graph — the ranking is stable but raise `maxIterations` if
you intend to quote the scores.

## Q8 — the finding worth showing

`make score` surfaced a real defect in Neo4j's own code. `llm-graph-builder`
imports `GraphDatabase` and `TransientError` directly from `neo4j` across 4 files,
but never declares `neo4j` in `requirements.txt` — it works only because
`langchain-neo4j` and `graphdatascience` pull the driver in transitively, and it
breaks silently the day either drops it.

Seeing this needs both halves of the graph at once: declared edges from manifests
and actual edges from source. An SCA tool has the manifest without the calls; a
single-repo code graph has the calls without the sibling repo. Neither can
produce it alone.

## The demo

Two queries, back to back:

```bash
make demo SYM='sym:repo:github.com/neo4j/neo4j-python-driver:src/neo4j/_async/driver.py#AsyncDriver'
```

Keep `SYM` single-quoted — symbol ids contain `#` and `:`.

- **Q1** — blast radius across the joined graph: which repos break, how far away, with paths.
- **Q2** — the same question confined to one repo. This is what any single tool returns.
- **Q3** — the shortest concrete path per impacted repo, with `file:line` citations.

The delta between Q1 and Q2 is the demo. Don't narrate it; show it.

Get a real symbol id to pass in with:

```bash
make shell
```
```cypher
MATCH (s:Symbol)<-[:CALLS_CROSS_REPO]-(c:Symbol)
RETURN s.id, s.qname, count(DISTINCT c) AS cross_repo_callers
ORDER BY cross_repo_callers DESC LIMIT 20;
```

Targets with cross-repo callers are the ones worth demoing — a symbol with only
intra-repo callers makes Q1 and Q2 return the same answer, which is the opposite
of the point.

Then `codekg query q5` to see what's still dangling — that's the honesty check,
and the first place to look when recall seems low.

## Exploring in Neo4j Enterprise Studio

```bash
make nes
```

Brings up Studio at http://localhost:8080 with Query, Bloom, and Dashboards over
the codekg graph. Sign in with `neo4j` / your `NEO4J_PASSWORD`, pick the
**codekg** deployment and the **neo4j** database.

Both Studio services sit behind the `nes` compose profile, so plain `make up`
stays fast and doesn't pull the Studio image.

**The license is referenced, not copied.** `NES_LICENSE_PATH` in `.env` points at
it (default `../nes-docker/internal.license`). A license is a secret and doesn't
belong in this tree; `make nes` fails with a clear message if the path is wrong.

`nes-init` applies [`cypher/nes-setup.cypher`](cypher/nes-setup.cypher) once
before Studio starts: the `tools_service` account, the `tools-storage` asset
database, and the `reader` grants Bloom needs. Without those grants Bloom shows
an empty schema, because it reads `SHOW INDEXES` / `SHOW CONSTRAINTS`.

The `toolspassword` credential appears in both `nes-setup.cypher` and
`docker-compose.yml`. If you change one and not the other, Studio fails to start
with an opaque asset-store error.

### One thing to check on first sign-in

Studio maps Neo4j roles to its own `studioAdmin` / `studioCreator` roles through
configuration, and no mapping is set here — the same as in the reference
`nes-docker` setup this was based on. Querying and Bloom exploration are verified
working; **saving** a query or dashboard is the one path not verified.

If saving is unavailable, it's role mapping, not a broken install. The docs page
is the place to get the exact key:
https://neo4j.com/docs/enterprise-studio/current/configuration-examples/

## Design decisions worth knowing

**Identity is the load-bearing piece.** Every node needs an ID that's
deterministic across extractors and across runs, or you can't `MERGE`
idempotently and the agreement matrix is noise. IDs are readable rather than
hashed so a wrong edge explains itself in the query result. Signature is
deliberately excluded from symbol identity — extractors disagree on how they
render signatures, which would make the same function look like different nodes
to different tools.

**One database, not four.** Every node and edge carries an `extractors` list,
appended on `MERGE`. Each comparison arm is a filter over one load rather than a
separate ingest, and triangulation becomes `size(r.extractors)` instead of a
fourth pipeline.

**Confidence is a feature, not a disclaimer.** Cross-repo joins are heuristic —
Go module prefixes are exact, Python symbol matching is not. Every derived edge
records `method` and `confidence`. Q6 shows the breakdown; show it unprompted.

**Corpus lives in a named volume, not a bind mount.** macOS virtiofs drops
inotify events, which silently breaks any extractor that watches files —
relevant the moment CodeGraph joins the harness, since incremental sync is its
whole differentiator.

**Generated code is excluded at extract time.** Otherwise your "god nodes"
become generated deep-copy functions and centrality is meaningless.

## Adding the other two extractors

Add a module under `loader/src/codekg/mappers/` exposing the same six functions
— `load`, `provenance`, `files`, `symbols`, `calls`, `external_refs` — and
register it in `cli.MAPPERS`. Nothing else changes. That abstraction is what
keeps the normalization tax from scaling with tool count.

Order of value:
1. **CodeGraph** — SQLite, so the ETL is a `SELECT`. Its debounced file watcher
   is the natural change-data-capture source for keeping Neo4j fresh without
   full re-indexes, and it emits dynamic-dispatch and cross-language edges the
   others don't.
2. **GitNexus** — needs a LadybugDB/Kuzu read. Contributes Process nodes and
   `route_map`, which are what you need for HTTP-contract joins in a
   service-topology corpus. Native bindings are arch-specific; expect to need
   `--platform linux/amd64` on Apple Silicon.

Only then is the agreement matrix worth building — it's the one capability that
genuinely requires all three.

## What the extractor drops, and why it matters

Graphify records an external import as a node labelled with the **symbol**
(`Driver`) and an empty `source_file`. The **module** it came from (`neo4j`) is
not kept anywhere in `graph.json`.

That isn't a bug. Inside one repo the module name is redundant — the import
either resolved to a local file or it didn't. The field only acquires value once
a sibling repo is in scope and you need to know *which* repo owns the symbol.

It's also the cleanest available illustration of the structural claim this
harness tests: the single-repo extractor discards precisely the field cross-repo
work needs, because in single-repo scope that field is worthless.
[`loader/src/codekg/importmap.py`](loader/src/codekg/importmap.py) recovers it
with a ~40-line `ast` pass over source already on disk. That's recovering one
dropped field, not reimplementing the extractor.

## Head-to-head: `graphify merge-graphs`

```bash
docker run --rm -v codekg_artifacts:/a --entrypoint sh codekg-graphify -c \
  'cd /a/graphify/a && graphify merge-graphs */graph.json --out /a/merged-a.json'
docker compose run --rm -T loader score a --merge /artifacts/merged-a.json
```

Graphify 0.9.43 ships `merge-graphs`, described as merging graph.json files
"into one cross-repo graph". Scored against the same oracle, as a proper arm:

| arm | corpus A pred | corpus B pred | precision | recall |
|---|---|---|---|---|
| **graphify-merge** | **0** | **0** | n/a | **0.000** |
| unified | 18 | 0 | 1.000 | 0.143 / 0.000 |
| unified+source | 99 | 2290 | 1.000 | 0.786 / 0.861 |

**Zero cross-repo edges, on both corpora.** Verified three independent ways:
edges whose endpoint nodes carry different `repo` tags (0), edges whose
namespaced id prefixes differ (0), and the `hyperedges` collection (empty).

`merge-graphs` is a **namespaced union**. Every node gains a `repo::` prefix and
every edge stays inside its source graph. Nothing links across.

The arm is given *perfect repo attribution* — the merge assigns opaque tags
(`repo`, `repo-2`, …) because our checkout directory names collide, so the
scorer recovers the real mapping by matching each tag's file set against the
per-repo artifacts. It is judged on its edges alone, not penalised for tagging.

### The sharp version of the finding

The merged graph *already contains both endpoints of the join*:

```
repo    package neo4j
repo-2  package langchain-neo4j     v0.10.0
repo-3  package graphdatascience
repo-4  package neo4j-graphrag      v1.18.0

repo-2::langchain-neo4j  -depends_on->  repo-2::None
```

Package identity, version, and the `depends_on` edge are all present. But the
dependency target stays inside its own repo namespace as an unlabeled
placeholder, instead of binding to the `repo-4::neo4j-graphrag` node sitting in
the same file.

That is precisely the gap
[`cypher/10_link_cross_repo.cypher`](cypher/10_link_cross_repo.cypher) fills:
step 1 reconciles package identity across repos, steps 2–4 lift it to symbol
level. The raw material is there. No pass runs over it.

**So state the claim plainly and it survives measurement:** no tool in this
space emits an edge that crosses a repo boundary — including the one whose
command is named for doing exactly that.

## What to claim, and what not to

The premise at the top of this README states that no tool emits a cross-repo
edge. That claim was measured, not assumed, and it held on both corpora.

Two things it does **not** entitle you to say:

- *"Graphify can't do cross-repo."* It ships the command, it namespaces cleanly,
  and it carries package identity through the merge. What it doesn't do is run a
  linking pass. That's a smaller and more defensible criticism.
- *"Our recall is 0.79/0.86."* Those are the `unified+source` ceiling
  diagnostics, which share their method with the oracle. The independently
  measured numbers are 0.143 on Python and 0.000 on Go.

The claim that survives all of it: **precision 1.000 across every arm and both
corpora.** The pipeline has never once invented an edge.

## Known gaps

- **Corpus B's `full` tier (loki, tempo) is unfetched.** Configured but not
  extracted; `make score` reports and excludes them rather than counting their
  whole import surface as missed.
- **Go artifact-only extraction yields nothing** (recall 0.000). Structural, not
  fixable in the mapper — see the corpus B section.
- Q3 evidence on Go is a 1-hop import site, not a call chain, because no
  symbol-level cross-repo calls exist to chain.
- Module-level constants are absent from the graph on both sides, which caps
  recall at roughly 0.79 even with the source pass.
- Method symbols are stored as `.close` rather than `Driver.close` — Graphify
  labels methods with a leading dot and no owning class. Harmless for cross-repo
  work (methods aren't imported) but it makes `qname` misleading for intra-repo
  reading.
- Python `exported` is the leading-underscore convention — it will wrongly
  exclude private symbols that other repos import anyway, a real pattern. Known
  recall gap, not papered over.
- True overloads collapse into one symbol node. Fine for Python and Go; revisit
  before adding Java or C++.
- No git history layer yet. None of the three extractors model commits, so
  co-change coupling and ownership queries need a separate GitHub ingest.
- No scoring harness yet — manifests give the answer key, but nothing computes
  precision/recall against it.
