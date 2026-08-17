# Data model

Every node label, relationship type and property in the graph, what it means,
which stage creates it, and why it exists. Counts are from the loaded corpora
(9 repos, 3 extractors) as a sense of scale.

The single most important thing to understand before reading the rest: **five
labels do the work, and only two relationship types cross a repository
boundary.** Everything else is per-repo structure that any of the three
extractors could have produced on its own.

---

## Node labels

### `:Repo` — 9

One checked-out repository at one pinned commit.

| property | meaning |
|---|---|
| `id` | `repo:{host}/{owner}/{name}` — e.g. `repo:github.com/grafana/dskit` |
| `name` | short id from `corpus.yaml` (`dskit`) |
| `url` | clone URL |
| `commit` | the pinned SHA this graph was built from |
| `ecosystem` | `python` \| `go` — selects the manifest parser and linking rules |
| `corpus` | `a` \| `b` — scopes scoring so one corpus is never measured against another's edges |
| `extractors` | which tools contributed |

### `:File` — 13,120

A source file. Also the anchor for imports, because **an import statement
belongs to a file, not to a function** — attributing it to a particular
function would mean redoing call resolution, which is the extractor's job.

| property | meaning |
|---|---|
| `id` | `file:{repo_id}:{path}` |
| `path` | repo-relative, forward-slashed |
| `repo` | denormalised `Repo.id`, so filtering never needs a join |
| `language` | file extension |
| `communityId`, `betweenness`, `pagerank`, `isArticulationPoint` | written by `make gds` — see below |

### `:Symbol` — 154,183

A named code entity: function, method, class, struct, interface, variable,
constant, property.

| property | meaning |
|---|---|
| `id` | `sym:{repo_id}:{path}#{qname}` |
| `name` | bare name (`InjectOrgID`) |
| `qname` | **identity name** — `Owner.member` for methods, bare name otherwise |
| `kind` | `function`, `method`, `class`, … |
| `path`, `start_line`, `end_line` | location, for `file:line` citations |
| `repo` | denormalised, as on `:File` |
| `exported` | our own rule, not the tool's: capitalised for Go, no leading underscore for Python |
| `docstring` | when the extractor provides one |
| `extractors` | **the agreement vector** — `size(extractors)` is the vote count |

> **Why `qname` and not the signature.** The three extractors render signatures
> differently, so including one would make the same function look like three
> different nodes and the agreement matrix would measure formatting. The cost is
> that true overloads collapse into one node — acceptable for Python and Go,
> revisit before adding Java or C++.

> **Why `exported` is ours and not the tool's.** CodeGraph reports
> `is_exported = 0` for every Python node. Using a shared rule keeps agreement
> about extraction rather than about differing export conventions.

### `:Package` — 344

A published or consumed package identity — the join key between repos.

| property | meaning |
|---|---|
| `id` | `pkg:{ecosystem}:{normalized_name}` |
| `name` | normalised: Python lowercases and folds `_`/`.` to `-` (PEP 503); Go paths are case-sensitive and untouched |
| `ecosystem` | `python` \| `go` |

### `:ExternalRef` — 19,413

**A reference that pointed outside its own repository.** The dangling end of a
would-be cross-repo edge.

This label is the hinge of the whole design. A single-repo extractor can, at
best, record *that* a reference left the repo; it cannot say what it reached,
because the target was not in scope. Resolving these is what the linking pass
does.

| property | meaning |
|---|---|
| `id` | `ext:{ecosystem}:{module}#{symbol}` |
| `module` | full module path — `github.com/grafana/dskit/ring`, `neo4j_graphrag.schema` |
| `root_module` | Python: top-level package for manifest matching. Go: the full path, since matching is by longest prefix |
| `symbol` | the referenced name |
| `ecosystem` | selects the resolution rule |

---

## Relationship types

### Structural — any single extractor produces these

| type | pattern | count | meaning |
|---|---|---|---|
| `CONTAINS` | `(:Repo)→(:File)` | 13,120 | file membership |
| `DECLARES` | `(:File)→(:Symbol)` | 154,183 | the file defines this symbol |
| `IN_REPO` | `(:Symbol)→(:Repo)` | 154,183 | denormalised shortcut, so repo-scoped queries skip a hop |
| `CALLS` | `(:Symbol)→(:Symbol)` | 276,364 | intra-repo call, inheritance or instantiation |
| `IMPORTS` | `(:File)→(:File)` | 90,104 | **intra-repo module dependency** — what circular-import detection runs on |

`CALLS` carries `extractors`, `confidence` and `evidence` (`file:line`).
Confidence is normalised across the tools' different vocabularies: a resolved
static call is 1.0, an inferred one 0.7, a dynamic-dispatch guess is capped at
0.5 so it can never outrank a resolved call in a blast-radius path.

### Declared dependencies — from manifests, not from code

| type | pattern | count | meaning |
|---|---|---|---|
| `PUBLISHES` | `(:Repo)→(:Package)` | 8 | this repo ships this package |
| `DEPENDS_ON` | `(:Repo)→(:Package)` | 516 | declared in `requirements.txt`, `pyproject.toml`, `go.mod`; carries `version_spec` and `source` |

Manifests are read by the loader, never by an extractor. That independence is
what makes them usable as ground truth.

### Dangling references — the raw material

| type | pattern | count | meaning |
|---|---|---|---|
| `USES` | `(:Symbol)→(:ExternalRef)` | 72,001 | a symbol referenced something outside the repo |
| `IMPORTS_EXT` | `(:File)→(:ExternalRef)` | 129,129 | a file imported something outside the repo; carries `line` |

Two anchors because the extractors differ: Graphify resolves from a symbol
node, CodeGraph and the source-derived pass attach to the file.

### Derived — created by the linking pass, exist nowhere upstream

**These four are the entire point of the system.** Every one records `method`
and `confidence`, so a reviewer can separate a fact from an inference.

| type | pattern | count | meaning |
|---|---|---|---|
| `RESOLVES_TO` | `(:ExternalRef)→(:Symbol)` | 9,190 | a dangling reference bound to a real symbol in a sibling repo |
| `IMPORTS_CROSS_REPO` | `(:File)→(:Symbol)` | 5,259 | file-level cross-repo import, with `line` |
| `CALLS_CROSS_REPO` | `(:Symbol)→(:Symbol)` | 97 | symbol-level cross-repo call |
| `DEPENDS_ON_REPO` | `(:Repo)→(:Repo)` | 9 | repo-level dependency, reconciled through `:Package`; `via` lists the packages |

Confidence is graded by *how* the match was made, not guessed:

| `method` | confidence | rule |
|---|---|---|
| `manifest` | 1.0 | read directly from a dependency manifest |
| `go-module-prefix` | 1.0 | Go import paths are fully qualified and name the publishing repo — exact by construction |
| `python-package-symbol` | 0.8 | top-level module matches a declared package and the symbol name is unique in it |
| `python-package-symbol` | 0.5 | same, but ambiguous — usually a re-export through `__init__.py` |
| `source-import` | inherited | file-level import; takes the confidence of the `RESOLVES_TO` it rides on |
| `package-lift` | inherited | symbol-level lift of `USES → RESOLVES_TO` |

> **Why `CALLS_CROSS_REPO` is only 97 while `IMPORTS_CROSS_REPO` is 5,259.**
> Symbol-level cross-repo calls require an extractor that anchors external
> references on a symbol *and* keeps the module. Only Graphify anchors on a
> symbol, and it discards the module. So most cross-repo signal necessarily
> arrives at file granularity. Queries traverse both.

---

## Properties written by GDS

`make gds` projects `:Symbol` and `:File` with `CALLS`, `CALLS_CROSS_REPO`,
`IMPORTS_CROSS_REPO` and `DECLARES`, undirected, and writes back:

| property | algorithm | reading |
|---|---|---|
| `communityId` | Leiden | subsystem membership; **spans repos**, which per-repo clustering cannot express |
| `betweenness` | Betweenness (sampled, 2000) | brokerage — how many shortest paths run through it |
| `pagerank` | PageRank | influence, distinct from brokerage |
| `isArticulationPoint` | Articulation points | **INTEGER 0/1, not boolean** — predicates need `> 0` |

---

## Reading the provenance

Three properties answer "should I believe this?" without leaving the graph:

- **`extractors`** on any node or relationship — which tools saw it.
  `size(extractors) = 3` is corroborated; `= 1` is a lead.
- **`method`** on a derived edge — how it was inferred.
- **`confidence`** — graded by method, never a flat default.

```cypher
// everything the graph knows about why this edge exists
MATCH (a:Symbol)-[r:CALLS_CROSS_REPO]->(b:Symbol)
RETURN a.qname, b.qname, r.method, r.confidence, r.via_module, r.extractors
LIMIT 5;
```

---

## What is deliberately absent

Worth knowing before someone asks:

- **No commits, authors, or PRs.** None of the three extractors model git
  history, and the corpus is shallow-cloned. Co-change coupling and ownership
  need a separate ingest.
- **No CVEs, incidents or deployments.** These are the natural next joins — and
  they attach to nodes that already exist (`:Package` for CVEs, `:Repo` or
  `:Symbol` for incidents).
- **No Community / Process / Route nodes from GitNexus.** It produces them, but
  they have no counterpart in the other two extractors and would appear as
  "gitnexus only" in the agreement matrix for purely structural reasons.
- **No test marker.** Test files are identified by path convention in Q16, not
  by a label. Adjust the pattern for another codebase.
