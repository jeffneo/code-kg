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
| `id` | `repo:{host}/{owner}/{name}`, plus `/{dist}` when set — e.g. `repo:github.com/grafana/dskit`, `repo:github.com/apache/airflow/task-sdk` |
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
| `language` | file extension, normalised by **us** — see below |
| `module` | Python only: the dotted name this file is importable as (`airflow.sdk.definitions.dag`). Set by `make enrich` |
| `communityId`, `betweenness`, `pagerank`, `isArticulationPoint` | written by `make gds` — see below |

> **Why `language` is ours and not the extractor's.** Graphify and GitNexus
> derive it from the extension (`py`); CodeGraph reports its own detected
> language (`python`). `MERGE_FILE` assigns rather than coalesces, so whichever
> extractor loaded last silently won, and the cycle projections — which filter
> `language IN ['py','go']` — matched nothing at all. Normalised centrally in
> `ids.language_of`, same reasoning as `exported`.

> **`module` is what makes exact cross-boundary resolution possible.** It is the
> Python analogue of Go's fully qualified import path: match an unresolved
> import's full module path against the file that provides it and the answer is
> exact, with no package declaration needed. It is the *only* rule that works
> when two distributions publish into one namespace package, as Airflow's core
> and task-sdk both do under `airflow` — there, the import name matches neither
> distribution name, and declaring both as publishing `airflow` would make every
> import match both repos and manufacture the very cycle we are looking for.

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
| `IMPORTS` | `(:File)→(:File)` | 90,104 | **intra-repo module dependency** — what circular-import detection runs on; carries `context` and `line` |

> **`IMPORTS.context` is the most consequential property in the schema for
> anything cycle-related.** It is `toplevel`, `typing`, or `deferred`, and no
> extractor provides it — it comes from a source-derived AST pass
> (`internal_imports.py`), which is also what resolves the import to a *file*
> rather than a module string.
>
> | `context` | executes | a cycle here means |
> |---|---|---|
> | `toplevel` | on import | a real import-time cycle |
> | `typing` | never (`if TYPE_CHECKING:`) | design coupling, already broken at runtime |
> | `deferred` | on call (inside a function) | someone deliberately broke a cycle |
>
> Without this field the three are indistinguishable and cycle findings are
> simply wrong. Measured: the neo4j-python-driver was reported as one
> 102-module component with an 11-hop cycle; classified top-level and
> facade-free it has **zero** cycles, because the part being quoted was held
> together by two `TYPE_CHECKING` edges that never run.
>
> Python edges get their context from `make enrich`. **Cycle queries return
> nothing if `enrich` has not run** — a safe failure rather than a wrong
> answer. Go edges are labelled `toplevel` at load time, which is exact: the
> language has no conditional and no function-scoped imports.

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
| `IMPORTS_EXT` | `(:File)→(:ExternalRef)` | 129,129 | a file imported something outside the repo; carries `line`, `context`, `guarded` |

`IMPORTS_EXT` carries the same `context` classification as `IMPORTS`, plus
`guarded` — see the note under the derived edges below.

Two anchors because the extractors differ: Graphify resolves from a symbol
node, CodeGraph and the source-derived pass attach to the file.

### Derived — created by the linking pass, exist nowhere upstream

**These four are the entire point of the system.** Every one records `method`
and `confidence`, so a reviewer can separate a fact from an inference.

| type | pattern | count | meaning |
|---|---|---|---|
| `RESOLVES_TO` | `(:ExternalRef)→(:Symbol)` | 10,036 | a dangling reference bound to a real symbol in a sibling repo |
| `IMPORTS_CROSS_REPO` | `(:File)→(:Symbol)` | 8,705 | file-level cross-repo import, with `line` |
| `IMPORTS_FILE_CROSS_REPO` | `(:File)→(:File)` | 863 | **cross-boundary file dependency**, resolved by exact module path; carries `context`, `guarded`, `via_module`, `line` |
| `CALLS_CROSS_REPO` | `(:Symbol)→(:Symbol)` | 862 | symbol-level cross-repo call |
| `DEPENDS_ON_REPO` | `(:Repo)→(:Repo)` | 12 | repo-level dependency, reconciled through `:Package`; `via` lists the packages |

> **Why `IMPORTS_FILE_CROSS_REPO` exists alongside `IMPORTS_CROSS_REPO`.** Every
> other derived edge terminates on a `:Symbol`, which makes it hostage to how
> completely an extractor enumerated declarations. The single most common
> crossing import in corpus D is `from airflow.sdk import ...` (56 statements),
> and `airflow/sdk/__init__.py` is a facade that re-exports — so it declares
> almost none of those names and symbol-mediated resolution misses the most
> important edge in the exhibit.
>
> This edge needs only two things we compute ourselves and exactly: the import
> statement (AST) and `File.module`. No extractor is in the path, so no
> extractor can weaken it. It is also the granularity module-level cycle
> detection needs — cycles live between files, not between symbols.

> **`guarded`** marks an import wrapped in `try: … except ImportError:`. It is
> orthogonal to `context`, not another context value: a guarded module-level
> import really does execute, so for cycle detection it is a genuine top-level
> edge. What changes is what you can ask someone to *do*. In corpus D it moves
> the actionable cut set from 7 import sites to **5** — two sit in
> `except ModuleNotFoundError:` blocks with working fallbacks, where the SDK
> already copes with core being absent.

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

Three further projections cover `:File` and `IMPORTS` only, and are all
**directed** — orientation is what makes a cycle a cycle, so the undirected
projection above is useless for them. All three filter to `context = 'toplevel'`
(plus `typing` for the last), and to `language IN ['py','go']` so that Markdown
cross-links cannot masquerade as imports:

| property | projection | includes | reading |
|---|---|---|---|
| `sccId` | `modules` | top-level, **with** `__init__.py` | like-for-like against pylint, which makes no facade distinction |
| `sccCoreId` | `modules_core` | top-level, facade-free | **the number to act on** |
| `sccDesignId` | `modules_design` | top-level + `typing`, facade-free | design coupling; real, but never a runtime risk |
| `sccXRepoId` | `modules_xrepo` | top-level, **plus cross-boundary edges** | components that span published artifacts — see below |

The gap between `sccId` and `sccCoreId` is package-facade inflation: a package
`__init__.py` that re-exports from submodules which import back from the package
root is idiomatic and not a defect, but it produces enormous components. The gap
between `sccCoreId` and `sccDesignId` is `TYPE_CHECKING` coupling.

Q15 pre-filters on `sccCoreId` and reports the other two as context. Go is
absent from the results by language design, not omission — the Go compiler
rejects circular package imports, so zero is the only correct answer, and
getting zero is a check on the method.

`modules_xrepo` is the one projection **no single-repo tool could build**: it
unions intra-repo `IMPORTS` with the derived cross-boundary edges, so SCC finds
components spanning separately published artifacts. Q17 filters to those. The
distinction matters because an intra-repo cycle is a refactor, while a cycle
across a published boundary is a release deadlock — neither side can ship
without a compatible version of the other already existing.

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
