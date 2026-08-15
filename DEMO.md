# codekg — run of show

A ~15 minute demo. Ten beats, one idea: **the cross-repo edges do not exist in
any single-repo tool, and once they exist a different class of question becomes
answerable.**

```bash
./demo.sh              # all ten beats, paced, [enter] between each
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
docker compose run --rm -T loader stats   # sanity: ~166k symbols, 9 repos
PAUSE=0 ./demo.sh              # full dry run, ~2 min, confirms every beat
```

Check these four things:

| check | expected | if wrong |
|---|---|---|
| `CALL gds.license.state()` | `isLicensed: TRUE` | GDS runs community-tier; beats 8–9 degrade. See README prerequisites |
| `make stats` → Repo | 9 | A corpus was not loaded — re-run `make load` for the missing one |
| Studio sign-in | works | Beats 1–10 are terminal-only and unaffected; only the Bloom close-out needs it |
| `./validate.sh` | 14/14 both corpora | Something drifted; do not present until it is green |

Have a second terminal open on `make shell` for the inevitable "can you show
me X?"

---

## The beats

Timings assume you talk while it runs. Beats 2→3 are the whole demo; everything
after is depth. **If you only get five minutes, run 2, 3, 5.**

### 1 — What is loaded  *(1 min)*
Nine real OSS repos: Neo4j's own Python packages and Grafana's Go stack. Three
independent extractors over all of them.

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

### 5 — A defect in Neo4j's own code  *(2 min)*
`llm-graph-builder` imports `GraphDatabase` and `TransientError` directly from
`neo4j` across 4 files, and never declares `neo4j` in `requirements.txt`. It
works only because `langchain-neo4j` pulls the driver in transitively.

> "Seeing this needs manifests *and* source together. An SCA tool has the
> manifest without the calls. A code graph has the calls without the sibling
> repo."

Often the strongest beat in the room, because it's a live bug rather than a
hypothetical.

### 6 — How much should you believe?  *(2 min)*
Three extractors vote on every edge. Only **~15% of call edges are corroborated
by all three**.

> "I'd rather show you this than have you find it. If you trust one tool's call
> graph, most of it is uncorroborated."

### 7 — Blast radius filtered by evidence  *(1 min)*
374 files: 1 corroborated by all three, 182 by two, 191 single-source.

> "That spread is the honest uncertainty. It only exists because there's a third
> opinion in the graph."

### 8 — Subsystems that cross repo boundaries  *(2 min)*
GitNexus and Graphify both run Leiden — per repo, so only intra-repo
communities. Over the joined graph: 24 communities span more than one repo, one
spans all four Go repos.

> "Same algorithm. The difference is the projection, and the projection they'd
> need doesn't exist inside any one of them."

### 9 — The estate's chokepoints  *(1 min)*
`InjectOrgID` near the top by betweenness — and beat 3 ranked it first by
cross-repo fan-in.

> "Two unrelated measures landing on the same symbol. That's corroboration, not
> coincidence."

### 10 — What it does *not* know  *(1 min)*
Dangling references, split into resolution misses vs genuine third-party.

> "Show this unprompted. Someone who has to ask twice stops believing the rest."

---

## Close out in Bloom

Terminal tables prove it; Bloom makes it feel like an estate rather than a
query result.

**Verified in place:** NES runs with `bloom: {Enabled: true}`, and the `reader`
grants Bloom needs (`SHOW INDEXES`, `SHOW CONSTRAINTS`) are applied by
`nes-init`. **Not verified:** the click-path below — walk it once yourself
before presenting.

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
| `make gds` errors | Beats 8–9 only. Skip them; nothing else depends on GDS |
| Studio won't sign in | Stay in the terminal. All ten beats are terminal-only |
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
