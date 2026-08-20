# codekg — run of show

A ~21 minute demo. Thirteen beats, one idea: **the cross-repo edges do not exist in
any single-repo tool, and once they exist a different class of question becomes
answerable.**

```bash
./demo.sh              # all thirteen beats, paced, [enter] between each
./demo.sh 2 3 4        # just the core reveal
PAUSE=0 ./demo.sh      # dry run, no waiting
```

---

## Pre-flight

Do this **before** the room is watching. Cold-starting Neo4j in front of a
prospect is a bad four minutes.

```bash
make up                        # Neo4j healthy
make nes                       # Studio at :8080, Bloom included
docker compose run --rm -T loader stats   # sanity: ~178k symbols, 12 repos
PAUSE=0 ./demo.sh              # full dry run, ~2 min, confirms every beat
```

Check these four things:

| check | expected | if wrong |
|---|---|---|
| `CALL gds.license.state()` | `isLicensed: TRUE` | GDS runs community-tier; beats 8–9 degrade. See README prerequisites |
| `make stats` → Repo | 12 | A corpus was not loaded — re-run `make load` for the missing one |
| Studio sign-in | works | Beats 1–13 are terminal-only and unaffected; only the Bloom close-out needs it |
| `./validate.sh` | 17/17 on all four corpora | Something drifted; do not present until it is green |

Have a second terminal open on `make shell` for the inevitable "can you show
me X?"

---

## The beats

Timings assume you talk while it runs. Beats 2→3 are the whole demo; everything
after is depth. **If you only get five minutes, run 2, 3, 6.**

### 1 — What is loaded  *(1 min)*
Twelve real OSS distributions: Neo4j's own Python packages, Grafana's Go stack,
SQLAlchemy, and two Airflow distributions. Three independent extractors over all
of them.

> "Nothing here is synthetic. This is your code and Grafana's."

### 2 — The question, answered the way the tools answer it  *(1 min)*
`InjectOrgID` in `dskit`. Single-repo scope → **12 files**.

> "That's what GitNexus, Graphify or CodeGraph each tell you. Not because
> they're bad — because the rest of the answer wasn't in scope when they ran."

### 3 — The same question, joined  *(2 min)* **← the demo**
**266 files across three repositories.** A ~22x understatement.

Let the table sit. Don't narrate the numbers; they're on screen.

> "Mimir, Loki and Tempo. A single-repo tool cannot mention them — the other end
> of every one of those edges was outside the repo it was indexing."

### 4 — Evidence, not a number  *(1 min)*
Shortest concrete path per impacted repo, with real `file:line`.

> "This is the part that survives contact with an engineer. It's a path they can
> open."

### 5 — So which tests actually cover it?  *(1 min)*
A dskit change reaches **97 mimir, 76 loki, 24 tempo** test files.

> "This is the practical follow-through. Blast radius tells you what to worry
> about; this tells you what to run before merging."

A mimir test covering a dskit change is invisible to an extractor indexing
either repo alone. Test files are matched by path convention — adjust the
pattern in Q16 for their codebase.

### 6 — A defect in Neo4j's own code  *(2 min)*
`llm-graph-builder` imports `GraphDatabase` and `TransientError` directly from
`neo4j` across 4 files, and never declares `neo4j` in `requirements.txt`. It
works only because `langchain-neo4j` pulls the driver in transitively.

> "Seeing this needs manifests *and* source together. An SCA tool has the
> manifest without the calls. A code graph has the calls without the sibling
> repo."

Often the strongest beat in the room, because it's a live bug rather than a
hypothetical.

### 7 — Circular dependencies  *(3 min)*   `CORPUS=c`
Q15 is global, so it lists every component in the graph: four in SQLAlchemy
(corpus C) plus one in `airflow-core`. Talk to the SQLAlchemy ones. **Four**
cyclic components, **22 modules**, the
largest two being **7 modules each in the ORM core**. Longest facade-free cycle
is 3 hops:

```
orm/strategies.py → orm/context.py → orm/loading.py → orm/strategies.py
```

**Open with the maintainers' own evidence, not with a number.** SQLAlchemy ships
[`lib/sqlalchemy/util/preloaded.py`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/util/preloaded.py),
a module whose stated job is resolving circular module imports at runtime. Nobody
writes that unless the cycles are real.

> "This isn't us inferring something. They built a subsystem to manage it. We're
> showing you where it's needed and how big it is."

**The substance is classification, not depth.** One `IMPORTS` edge can mean three
different things, and only one of them is a cycle you can hit:

| context | executes | cycle here means |
|---|---|---|
| top-level | on import | real import-time cycle |
| `if TYPE_CHECKING:` | **never** | design coupling, already broken |
| inside a function | on call | someone deliberately broke a cycle |

Read the ladder downward — each step removes edges that are not runtime cycles:

| what you count | modules implicated |
|---|---|
| every context, facades included *(what a context-blind tool sees)* | 184 |
| top-level only | 159 |
| **top-level, facades removed** | **22** |

> "The top number isn't wrong, it's just not actionable. Twenty-two is the number
> you can hand to a team."

**Then the head-to-head, because it is checkable on the spot.** `make
score-cycles CORPUS=c` runs pylint's `cyclic-import` as an independent oracle:

- **Recall 1.000** — every one of the 77 modules pylint names, no misses.
- We name **107 more**, and they are real. Verified by hand:
  `dialects/mssql/pymssql.py → base.py → mssql/__init__.py → pymssql.py`, all
  three edges top-level, all three visible in source. pylint doesn't model
  `from . import X` as executing the package's `__init__`.
- pylint reports overlapping **chains**, not components — and the chain count is
  not stable, giving 78, 74 and 72 across identical runs while the module set
  stayed at 77. "How many distinct problems do I have" is not answerable from
  that output.

**The performance technique, briefly.** `MATCH (a:File)-[:IMPORTS*2..n]->(a)`
returns in 18 ms at n=5 and **does not finish in 90 seconds at n=6**. SCC answers
"which nodes can be in a cycle at all" in O(V+E) and completely, so enumeration
only ever runs inside tiny components.

**If someone asks about the Neo4j driver** (it is in corpus A, so it may come
up): zero runtime cycles. Say it plainly — and if you want the credibility, say
that an earlier version of this demo reported 102 entangled modules and an 11-hop
cycle for the driver, and that it was **wrong**: the component was held together
by two `TYPE_CHECKING` edges that never execute. Being the person who found and
fixed that is worth more than the number ever was.

**Go's zero is a selling point, so say it out loud.** The Go compiler rejects
circular package imports, so the right answer is known before you run anything —
and across **83,529 top-level Go import edges in 3,559 files, we return zero**.

> "We ran it against a language that makes this impossible, over eighty thousand
> edges, and got the answer the compiler guarantees. That's the control."

Footnote if pressed: those Go edges come from GitNexus. Graphify emits almost
none (mimir 6, loki 1, tempo 0) because it resolves Go imports to external
package refs — so the control only exists because a second extractor was in the
harness.

Also worth a sentence: `mimir`/`loki` once appeared to have cycles that were
Markdown files cross-linking, because Graphify indexes `.md` and treats a link as
an import. Filtered out now.

### 7b — The cycle that crosses a release boundary  *(3 min)*   `CORPUS=d`  **← the strongest single result**
Everything up to here is a *better* answer to a question the tools can at least
ask. This is a question they **cannot express**: when either side is parsed, the
other end of the edge is out of scope.

`apache-airflow-core` and `apache-airflow-task-sdk` are separately published,
separately versioned PyPI distributions that **require each other**:

```
airflow-core/pyproject.toml:154   apache-airflow-task-sdk<1.5.0,>=1.4.0
task-sdk/pyproject.toml:51        apache-airflow-core<3.5.0,>=3.4.0
```

> "An import cycle inside a repo is a refactor — one commit, one reviewer. This
> is a release deadlock. Neither of these can ship without a compatible version
> of the other already existing on PyPI. Somebody is paying for that every
> release, and no single CI job sees it."

Run `q17`. What lands:

| | |
|---|---|
| core → task-sdk | 132 top-level import edges |
| task-sdk → core | **7** |
| declared both ways in manifests | **true** |
| modules entangled across the boundary | 86, in 2 components |
| **cut set** | **5 sites, with line numbers** |

**Two things to draw out.**

*The asymmetry is the finding.* 132 one way, 7 the other. Cut the 7 and a mutual
dependency becomes a clean layering. That is a work item, not an observation.

*Five, not seven — and say why.* Two of the seven sit inside
`except ModuleNotFoundError:` / `except (ImportError, AttributeError):` with
working fallbacks; the SDK already treats core as optional there. The query
reports those separately.

> "We're not handing you seven problems. Two of them your own code already
> handles. Here are the five that actually block you."

That distinction is the whole credibility play. Presenting all seven as work in
front of a maintainer who wrote those `try/except` blocks would lose the room.

**And `declared_both_ways: true` is the corroboration.** The code finding and the
manifests agree, and they come from different places — source AST versus
`pyproject.toml`, which no extractor reads as a graph.

**If asked "isn't this one repo?" — yes, and answer it head-on.** These are two
distributions in `apache/airflow`. What makes them two dependency endpoints is
that they version, publish and release independently, which is exactly what
creates the deadlock. Same shape as two repos in a corporate polyrepo, which is
where your audience will actually meet it.

Worth mentioning what it took: two distributions sharing a clone URL collapsed
into one `:Repo` node, which makes the cycle literally unrepresentable — hence a
`dist` segment in the repo id. And both publish into the same `airflow` namespace
package, so the import name matches *neither* distribution name; resolution had
to move to exact module paths. Declaring both as publishing `airflow` would have
made every import match both sides and **manufactured** the cycle.

### 8 — How much should you believe?  *(2 min)*
Three extractors vote on every edge. Only **~15% of call edges are corroborated
by all three**.

> "I'd rather show you this than have you find it. If you trust one tool's call
> graph, most of it is uncorroborated."

### 9 — Blast radius filtered by evidence  *(1 min)*
374 files: 1 corroborated by all three, 182 by two, 191 single-source.

> "That spread is the honest uncertainty. It only exists because there's a third
> opinion in the graph."

### 10 — Subsystems that cross repo boundaries  *(2 min)*
GitNexus and Graphify both run Leiden — per repo, so only intra-repo
communities. Over the joined graph: 24 communities span more than one repo, one
spans all four Go repos.

> "Same algorithm. The difference is the projection, and the projection they'd
> need doesn't exist inside any one of them."

### 11 — The estate's chokepoints  *(1 min)*
`InjectOrgID` near the top by betweenness — and beat 3 ranked it first by
cross-repo fan-in.

> "Two unrelated measures landing on the same symbol. That's corroboration, not
> coincidence."

### 12 — What it does *not* know  *(1 min)*
Dangling references, split into resolution misses vs genuine third-party.

> "Show this unprompted. Someone who has to ask twice stops believing the rest."

---

## Close out in Bloom

Terminal tables prove it; Bloom makes it feel like an estate rather than a
query result.

**Verified in place:** NES runs with `bloom: {Enabled: true}`; the `reader`
grants Bloom needs (`SHOW INDEXES`, `SHOW CONSTRAINTS`) are applied by
`nes-init`; and the server log confirms `neo4j`'s `admin` role maps to
`roles/studio-admin`, so **saving queries, dashboards and Perspectives works**
with no role-mapping configuration. **Not verified:** the click-path below —
walk it once yourself before presenting.

1. http://localhost:8080 → sign in → deployment **codekg**, database **neo4j**.
2. Bloom → new Perspective on `neo4j`. Let it generate from the schema.
3. Styling that makes the point visible:
   - **Symbol** — colour by `repo`. The repo boundary becomes the thing you see.
   - **Symbol** — size by `betweenness`. Chokepoints get large.
   - **CALLS_CROSS_REPO** and **IMPORTS_CROSS_REPO** — a distinct, heavy colour.
     These are the edges no tool produced; they should be unmistakable.
   - Optional: colour by `communityId` as a second scene to show subsystems
     ignoring repo colour entirely.
4. Search phrases to have ready:
   - `Symbol` where `name` is `InjectOrgID` → expand → the fan-out across three repos.
   - `Repo` → `CONTAINS` → orient the room before diving in.

The single most effective moment: colour by `repo`, then expand `InjectOrgID`
and let the other three colours appear.

### Studio dashboard (optional)

Cards worth building, all Cypher already in `cypher/90_queries.cypher`:
`DEPENDS_ON_REPO` count as a KPI, Q10 vote distribution as a pie, Q12 as a
table, Q13 as a bar chart.

---

## If it breaks live

| symptom | do this |
|---|---|
| A query returns 0 rows | Wrong symbol id. `./demo.sh 1` then `make shell` and re-find it — ids contain `#` and must be single-quoted |
| `make gds` errors | Beats 7, 10 and 11. Q15 needs `sccId` from GDS; the others need communities and centrality |
| Studio won't sign in | Stay in the terminal. All thirteen beats are terminal-only |
| Studio page blank / connection refused | It exits when Neo4j restarts under it. `docker compose --profile nes up -d` — it now has `restart: unless-stopped` |
| Neo4j unhealthy | `make up` and wait for healthy; do not present against a starting DB |
| Numbers differ from this doc | Expected if the corpus was re-fetched. `corpus.lock.yaml` pins commits — say so, it's a strength |

---

## What not to claim

Every one of these was measured, and each has a boundary that a sharp prospect
will find if you overstate it.

- **Not** "these tools can't do cross-repo." Graphify ships `merge-graphs`. It
  produces **zero** cross-repo edges — it's a namespaced union with no linking
  pass — but it exists, and its author would say so.
- **Not** "our recall is 0.96." That's CodeGraph's artifact on corpus A.
  Graphify is 0.254; on Go, Graphify and GitNexus are **0.000** and CodeGraph is
  0.536. Extractor choice dominates — say which one you mean.
- **Not** "precision is always 1.000." It is on corpus A. On corpus B it's 0.999
  — 2 false positives survive, from Go code shadowing a package name with a
  variable.
- **Not** "the agreement numbers are clean." On Go they're a **lower bound**:
  ~20,853 graphify-only methods are an unfixed naming divergence, not real
  disagreement.
- **Do** lead with what survives all of it: **the cross-repo edges exist nowhere
  else, and every one carries its inference method and confidence.**
