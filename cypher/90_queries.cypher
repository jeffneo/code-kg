// ============================================================================
// DEMO QUERIES
//
// Run Q1 and Q2 back to back. Q2 is the query with the corpus artificially
// confined to one repo - it is what GitNexus, Graphify, or CodeGraph can tell
// you on their own. Q1 is the same question against the joined graph. The
// delta between the two answers is the demo; do not narrate it, show it.
// ============================================================================


// ----------------------------------------------------------------------------
// Q1 - CROSS-REPO BLAST RADIUS
//
// "I am changing this function. What breaks, and in whose repo?"
//
// :param changed => 'sym:repo:github.com/neo4j/neo4j-python-driver:src/neo4j/_async/driver.py#AsyncDriver'
// ----------------------------------------------------------------------------
// `changed` has to be projected through every aggregating WITH. Referencing it
// only in the final ORDER BY is a syntax error - after an aggregation, the
// variables in scope are exactly the ones the RETURN/WITH declared.
// Reach arrives by two routes and they meet at different node types:
// CALLS_CROSS_REPO starts at a :Symbol, IMPORTS_CROSS_REPO at a :File. The
// file is the common denominator, and it is also the granularity the scoring
// harness measures, so impact is counted in files.
//
// Traversing only CALLS_CROSS_REPO returns nothing on a Go corpus, where the
// extractor artifact yields no symbol-level cross-repo calls at all.
MATCH (changed:Symbol {id: $changed})
CALL (changed) {
    MATCH (s:Symbol)-[:CALLS|CALLS_CROSS_REPO*1..6]->(changed)
    RETURN s.repo AS repo, s.path AS path, 'call-path' AS via
  UNION
    MATCH (f:File)-[:IMPORTS_CROSS_REPO]->(changed)
    RETURN f.repo AS repo, f.path AS path, 'import' AS via
}
WITH changed.repo          AS origin_repo,
     repo                  AS impacted_repo,
     count(DISTINCT path)  AS impacted_files,
     collect(DISTINCT via) AS reached_via
RETURN impacted_repo,
       impacted_files,
       reached_via,
       impacted_repo <> origin_repo AS is_cross_repo
ORDER BY is_cross_repo DESC, impacted_files DESC;


// ----------------------------------------------------------------------------
// Q2 - THE CONTROL: same question, single-repo scope
//
// This is what each tool returns on its own. On corpus A the answer for a
// driver-internal symbol is usually "nothing outside this file's own module",
// which is exactly the false comfort the harness exists to expose.
// ----------------------------------------------------------------------------
// Counted in files, to be directly comparable with Q1.
MATCH (changed:Symbol {id: $changed})
MATCH (caller:Symbol)-[:CALLS*1..6]->(changed)
WHERE caller.repo = changed.repo
RETURN changed.repo                 AS scope,
       count(DISTINCT caller.path)  AS impacted_files_single_repo;


// ----------------------------------------------------------------------------
// Q3 - EVIDENCE FOR ONE IMPACTED REPO
//
// Blast radius counts are not persuasive without a path you can click through
// to real file:line. This returns the shortest concrete path per impacted repo.
//
// :param changed  => as above
// ----------------------------------------------------------------------------
// shortestPath needs BOTH endpoints already bound, so the reaching callers are
// found first and only then shortest-pathed. Doing it in one step - binding
// `caller` inside the shortestPath pattern - is a runtime error, and binding
// every Symbol first would shortest-path across all 13k of them.
MATCH (changed:Symbol {id: $changed})
CALL (changed) {
    // Symbol-level route: a real call chain.
    MATCH (c:Symbol)-[:CALLS|CALLS_CROSS_REPO*1..6]->(changed)
    WHERE c.repo <> changed.repo
    WITH DISTINCT changed, c AS caller
    MATCH path = shortestPath((caller)-[:CALLS|CALLS_CROSS_REPO*1..6]->(changed))
    RETURN caller.repo                     AS impacted_repo,
           length(path)                    AS hops,
           [n IN nodes(path) | n.qname]    AS chain,
           [n IN nodes(path) | n.path + ':' + coalesce(toString(n.start_line), '?')] AS citations,
           reduce(x = 1.0, r IN relationships(path) |
                  x * coalesce(r.confidence, 1.0)) AS conf
  UNION
    // File-level route: an import site. This is the only route available on a
    // Go corpus, where the artifact yields no symbol-level cross-repo calls.
    MATCH (f:File)-[i:IMPORTS_CROSS_REPO]->(changed)
    WHERE f.repo <> changed.repo
    RETURN f.repo AS impacted_repo,
           1      AS hops,
           [f.path, changed.qname] AS chain,
           [f.path + ':' + coalesce(toString(i.line), '?'),
            changed.path + ':' + coalesce(toString(changed.start_line), '?')] AS citations,
           coalesce(i.confidence, 1.0) AS conf
}
WITH impacted_repo, hops, chain, citations, conf
ORDER BY conf DESC, hops ASC
WITH impacted_repo,
     head(collect({hops: hops, chain: chain, citations: citations, conf: conf})) AS best
RETURN impacted_repo,
       best.hops       AS hops,
       best.chain      AS call_chain,
       best.citations  AS citations,
       best.conf       AS path_confidence
ORDER BY path_confidence DESC, hops ASC;


// ----------------------------------------------------------------------------
// Q4 - SHARED-LIBRARY FAN-IN  (corpus B / dskit)
//
// "Which symbols in the shared kit are load-bearing across the whole estate?"
// Ranks by how many distinct downstream repos reach each symbol, not by raw
// call count - a function called 400 times inside one service matters less
// than one called once by each of four.
// ----------------------------------------------------------------------------
// Traverses both cross-repo edge types. CALLS_CROSS_REPO originates at a
// :Symbol and IMPORTS_CROSS_REPO at a :File - both carry `repo`, so one pattern
// covers them. Using only CALLS_CROSS_REPO returns nothing on a Go corpus,
// where the artifact yields no symbol-level cross-repo calls at all.
MATCH (lib:Repo {id: $lib_repo})<-[:IN_REPO]-(target:Symbol)
MATCH (consumer)-[:CALLS_CROSS_REPO|IMPORTS_CROSS_REPO]->(target)
WITH target,
     count(DISTINCT consumer.repo) AS consuming_repos,
     count(DISTINCT consumer)      AS consuming_symbols,
     collect(DISTINCT consumer.repo) AS consumers
// Ordering surfaces the multi-consumer symbols; no threshold, because on a
// deep-chain corpus like A the interesting targets have exactly one consuming
// repo and a filter of >1 returns nothing at all.
RETURN target.qname     AS symbol,
       target.path      AS defined_in,
       consuming_repos,
       consuming_symbols,
       consumers
ORDER BY consuming_repos DESC, consuming_symbols DESC
LIMIT 25;


// ----------------------------------------------------------------------------
// Q5 - WHAT IS STILL DANGLING
//
// Honesty check, and the first thing to look at when recall seems low. Every
// unresolved ExternalRef pointing at a package some corpus repo publishes is a
// resolution miss, not a genuine third-party dependency. High counts here mean
// the linking pass needs work, not that the graph is finished.
// ----------------------------------------------------------------------------
MATCH (ref:ExternalRef)
WHERE NOT (ref)-[:RESOLVES_TO]->()
OPTIONAL MATCH (publisher:Repo)-[:PUBLISHES]->(pkg:Package)
WHERE pkg.name = coalesce(ref.root_module, ref.module)
   OR ref.module STARTS WITH pkg.name
WITH ref, publisher
RETURN coalesce(ref.root_module, ref.module)  AS module,
       count(*)                               AS unresolved_refs,
       publisher IS NOT NULL                  AS in_corpus,
       CASE WHEN publisher IS NOT NULL
            THEN 'RESOLUTION MISS - fix the linking pass'
            ELSE 'genuine third-party, expected'
       END                                    AS verdict
ORDER BY in_corpus DESC, unresolved_refs DESC
LIMIT 30;


// ----------------------------------------------------------------------------
// Q6 - CONFIDENCE AUDIT
//
// Every derived cross-repo edge, bucketed by how it was inferred. Show this
// unprompted. A prospect who asks "how do I know these edges are real?" and
// gets a straight answer trusts the rest of the demo; one who has to ask twice
// does not.
// ----------------------------------------------------------------------------
// The total is computed up front and carried as a grouping key. The previous
// version reached for a pattern comprehension over unbound variables to get it,
// which is not a legal way to count a relationship type.
// The aggregation and the arithmetic on it have to be in separate clauses.
// `round(100.0 * count(*) / total)` mixes an aggregate with the grouping key
// `total` in one expression, which Cypher rejects.
MATCH ()-[:CALLS_CROSS_REPO]->()
WITH toFloat(count(*)) AS total
MATCH ()-[r:CALLS_CROSS_REPO]->()
WITH total,
     r.method     AS inference_method,
     r.confidence AS confidence,
     count(*)     AS edges
RETURN inference_method,
       confidence,
       edges,
       round(100.0 * edges / total, 1) AS pct_of_cross_repo
ORDER BY confidence DESC, edges DESC;


// ----------------------------------------------------------------------------
// Q7 - CORPUS OVERVIEW
//
// Sanity check after a load. If a repo shows zero symbols the extractor
// silently failed on it - check that repo's extract.log before anything else.
// ----------------------------------------------------------------------------
MATCH (r:Repo)
OPTIONAL MATCH (r)<-[:IN_REPO]-(s:Symbol)
OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
RETURN r.id                     AS repo,
       r.commit                 AS commit,
       count(DISTINCT f)        AS files,
       count(DISTINCT s)        AS symbols,
       r.extractors             AS extracted_by
ORDER BY symbols DESC;


// ----------------------------------------------------------------------------
// Q8 - UNDECLARED DEPENDENCIES
//
// A repo whose source imports from a sibling repo it never declares in its
// manifest. It works today only because something else pulls the package in
// transitively, and it breaks silently the day that intermediary drops it.
//
// This needs both halves of the graph at once: the declared edges from
// manifests and the actual edges from source. Neither an SCA tool nor a
// single-repo code graph can see it - one has the manifest without the calls,
// the other has the calls without the sibling repo.
// ----------------------------------------------------------------------------
// The repos are reached by traversal rather than by re-matching on id. Binding
// them as two disconnected patterns builds a cartesian product, which Neo4j
// warns about - and a performance warning on screen mid-demo is a bad look.
MATCH (consumer:Repo)-[:CONTAINS]->(f:File)-[:IMPORTS_CROSS_REPO]->(t:Symbol)-[:IN_REPO]->(publisher:Repo)
WHERE NOT (consumer)-[:DEPENDS_ON_REPO]->(publisher)
RETURN consumer.id                        AS consumer,
       publisher.id                       AS undeclared_dependency,
       count(DISTINCT f.path)             AS importing_files,
       collect(DISTINCT t.name)[0..8]     AS example_symbols,
       collect(DISTINCT f.path)[0..5]     AS example_files
ORDER BY importing_files DESC;


// ----------------------------------------------------------------------------
// Q9 - EXTRACTOR AGREEMENT MATRIX
//
// The one capability that genuinely requires more than one tool. Every node and
// edge accumulates an `extractors` list on MERGE, so agreement is a property
// read rather than a second pipeline.
//
// Read it as a trust signal: an edge both tools found independently is
// something to act on; an edge only one found is a lead to verify. What it does
// NOT measure is correctness - two tools can agree and both be wrong, and the
// package-scoping bug in the Go linker is a live example of exactly that.
//
// Only meaningful because both mappers normalise identity the same way
// (`Owner.member` for methods). Without that, this measures label formatting.
// ----------------------------------------------------------------------------
// Neo4j has no window functions, so the total is computed up front and carried
// as a grouping key - the same shape Q6 uses.
MATCH (:Repo {corpus: $corpus})<-[:IN_REPO]-(s:Symbol)
WITH toFloat(count(*)) AS total
MATCH (:Repo {corpus: $corpus})<-[:IN_REPO]-(s:Symbol)
WITH total, size(s.extractors) AS votes, s.extractors AS who, count(*) AS symbols
RETURN votes,
       CASE votes WHEN 3 THEN 'all three'
                  WHEN 1 THEN 'only ' + who[0]
                  ELSE apoc.text.join(apoc.coll.sort(who), ' + ') END AS found_by,
       symbols,
       round(100.0 * symbols / total, 1) AS pct
ORDER BY votes DESC, symbols DESC;


// ----------------------------------------------------------------------------
// Q10 - CALL-EDGE AGREEMENT
//
// Divergence concentrates in call edges, which is where the resolution
// heuristics actually differ - cross-file resolution, dynamic dispatch, and
// method binding. Node agreement is mostly a question of what counts as a node.
// ----------------------------------------------------------------------------
MATCH (:Repo {corpus: $corpus})<-[:IN_REPO]-(a:Symbol)-[:CALLS]->(:Symbol)
WITH toFloat(count(*)) AS total
MATCH (:Repo {corpus: $corpus})<-[:IN_REPO]-(a:Symbol)-[c:CALLS]->(:Symbol)
WITH total, size(c.extractors) AS votes, c.extractors AS who, count(*) AS call_edges
RETURN votes,
       CASE votes WHEN 3 THEN 'all three'
                  WHEN 1 THEN 'only ' + who[0]
                  ELSE apoc.text.join(apoc.coll.sort(who), ' + ') END AS found_by,
       call_edges,
       round(100.0 * call_edges / total, 1) AS pct
ORDER BY votes DESC, call_edges DESC;


// ----------------------------------------------------------------------------
// Q11 - TRUST-FILTERED BLAST RADIUS
//
// The capability three extractors buy that two cannot: a majority vote.
//
// Same question as Q1, answered three times at different evidence thresholds.
// The spread between them is the honest uncertainty in the answer - and on
// corpus A only ~15% of call edges are corroborated by all three tools, so the
// spread is wide and worth showing rather than hiding behind a single number.
//
// :param changed => a symbol id
// ----------------------------------------------------------------------------
MATCH (changed:Symbol {id: $changed})
CALL (changed) {
    MATCH path = (s:Symbol)-[:CALLS*1..4]->(changed)
    RETURN s.path AS path,
           reduce(m = 3, r IN relationships(path) |
                  CASE WHEN size(r.extractors) < m THEN size(r.extractors) ELSE m END) AS votes
}
WITH path, max(votes) AS votes
RETURN CASE votes WHEN 3 THEN 'corroborated by all three'
                  WHEN 2 THEN 'two of three'
                  ELSE 'single extractor - verify' END AS evidence,
       count(DISTINCT path) AS impacted_files
ORDER BY evidence;


// ----------------------------------------------------------------------------
// Q12 - SUBSYSTEMS THAT CROSS REPO BOUNDARIES   (requires `make gds`)
//
// GitNexus and Graphify both run Leiden. Per repo. Same algorithm, but a
// per-repo projection can only ever return intra-repo communities - the answer
// is constrained by the projection, not by the clustering.
//
// Over the joined graph the question changes kind: which subsystems does this
// organisation actually have, irrespective of how the code is filed into
// repositories? On the loaded corpora, 24 communities span more than one repo.
// ----------------------------------------------------------------------------
MATCH (s:Symbol) WHERE s.communityId IS NOT NULL
WITH s.communityId AS community,
     collect(DISTINCT split(s.repo, '/')[-1]) AS repos,
     count(*) AS members
WHERE size(repos) > 1
RETURN community, members, size(repos) AS repos_spanned, repos
ORDER BY repos_spanned DESC, members DESC
LIMIT 20;


// ----------------------------------------------------------------------------
// Q13 - ORG-LEVEL CHOKEPOINTS   (requires `make gds`)
//
// Betweenness over the whole estate: the symbols the most shortest paths run
// through. Distinct from raw fan-in - a broker can have few callers and still
// sit on every route between two subsystems.
//
// Worth noting as corroboration rather than coincidence: dskit's InjectOrgID
// ranks near the top here, and Q4 independently ranks it first by cross-repo
// fan-in. Two different measures, same answer.
// ----------------------------------------------------------------------------
MATCH (s:Symbol) WHERE s.betweenness > 0
RETURN split(s.repo, '/')[-1] AS repo,
       s.qname                AS symbol,
       s.path                 AS defined_in,
       round(s.betweenness)   AS betweenness,
       s.communityId          AS community
ORDER BY s.betweenness DESC
LIMIT 20;


// ----------------------------------------------------------------------------
// Q14 - FRAGILE SEAMS   (requires `make gds`)
//
// Articulation points - nodes whose removal splits the graph into more
// components - filtered to those consumed by two or more repos. These are the
// single points of structural failure for the estate, and every one on the
// loaded corpora sits inside dskit.
//
// NOTE: gds.articulationPoints writes an INTEGER (0/1), not a boolean, so this
// compares with `> 0` rather than testing truthiness.
// ----------------------------------------------------------------------------
MATCH (s:Symbol) WHERE s.isArticulationPoint > 0
MATCH (s)<-[:CALLS_CROSS_REPO|IMPORTS_CROSS_REPO]-(consumer)
WITH s, count(DISTINCT consumer.repo) AS consuming_repos
WHERE consuming_repos >= 2
RETURN split(s.repo, '/')[-1]        AS defined_in,
       s.qname                       AS symbol,
       s.path                        AS path,
       consuming_repos,
       round(coalesce(s.betweenness, 0)) AS betweenness
ORDER BY consuming_repos DESC, betweenness DESC
LIMIT 20;


// ----------------------------------------------------------------------------
// Q15 - CIRCULAR DEPENDENCIES   (requires `make gds` for sccId / sccCoreId)
//
// WHY THIS IS NOT A VARIABLE-LENGTH MATCH
// ---------------------------------------
// `MATCH (a:File)-[:IMPORTS*2..n]->(a)` returns in 18 ms at n=5 and does not
// finish in 90 seconds at n=6. It also reports each cycle once per rotation.
// SCC answers "which nodes can be in a cycle at all" in O(V+E), completely,
// and enumeration then runs only inside those components.
//
// WHAT COUNTS AS A CYCLE - READ THIS BEFORE PRESENTING THE RESULT
// ---------------------------------------------------------------
// Two filters are applied upstream in the projection, and both are load-bearing.
//
// 1. ONLY `context = 'toplevel'` EDGES. An import inside `if TYPE_CHECKING:`
//    never executes; an import inside a function body runs on call, not on
//    import, and is usually a deliberate cycle-break. Counting them as cycles
//    is simply wrong. This filter is why the driver, once classified, has ZERO
//    runtime cycles - the component we used to quote was held together by two
//    TYPE_CHECKING edges.
//
// 2. FACADES SEPARATED. `sccCoreId` excludes __init__.py. A package facade that
//    re-exports from submodules which import back from the package root is
//    idiomatic and not a defect, but it creates enormous components.
//
// So: `core_modules` is the finding. `entangled` is context, and on its own it
// overstates - which is exactly the mistake this query used to make.
//
// Go is absent from the results by language design, not by omission: the Go
// compiler rejects circular package imports, so zero is the only correct answer
// and finding zero is a check on the method.
//
// Independently verified against pylint's cyclic-import via `make score-cycles`.
// pylint reports overlapping chains with no component structure and no facade
// distinction - 8+ findings on the driver for what is one component.
// ----------------------------------------------------------------------------
MATCH (f:File) WHERE f.sccCoreId IS NOT NULL
WITH f.sccCoreId AS scc, collect(f) AS members, count(*) AS core_modules
WHERE core_modules > 1
CALL apoc.nodes.cycles(members, {relTypes: ['IMPORTS'], maxDepth: 10}) YIELD path
// apoc bounds traversal to `members`, which keeps this fast, but it cannot
// filter on a relationship property - so it will happily route a cycle through
// a TYPE_CHECKING edge. Discarding those here is what keeps the enumeration
// consistent with the projection that selected the component in the first
// place. Without this line the classification is undone at the last step.
WHERE all(r IN relationships(path) WHERE r.context = 'toplevel')
// apoc also expands THROUGH nodes outside the list it was given, so a cycle
// could be routed via a package __init__ - and reporting that under a
// "facade-free" heading is exactly the sleight of hand this query exists to
// avoid. Confining the path to the component keeps the example consistent with
// the number next to it.
  AND all(n IN nodes(path) WHERE n.sccCoreId = scc)
// ORDER BY before aggregating so head(collect(...)) is the LONGEST cycle, not
// an arbitrary one.
WITH scc, members, core_modules, path ORDER BY length(path) DESC
WITH scc, members, core_modules,
     count(path)         AS cycles,
     max(length(path))   AS longest,
     head(collect(path)) AS worst
WHERE cycles > 0
UNWIND members AS m
WITH scc, core_modules, cycles, longest, worst,
     collect(m)[0].repo              AS repo,
     max(coalesce(m.sccId, 0))       AS runtime_component,
     max(coalesce(m.sccDesignId, 0)) AS design_component
// Both imported variables stay listed in every WITH inside the subquery: a WITH
// drops anything it does not name, imported or not.
CALL (runtime_component, design_component) {
  MATCH (a:File) WHERE a.sccId = runtime_component
  WITH count(a) AS entangled_with_facades, design_component
  MATCH (b:File) WHERE b.sccDesignId = design_component
  RETURN entangled_with_facades, count(b) AS design_time_modules
}
RETURN split(repo, '/')[-1]         AS repo,
       core_modules,
       cycles,
       longest,
       entangled_with_facades,
       design_time_modules,
       [n IN nodes(worst) | n.path]  AS longest_cycle
ORDER BY core_modules DESC, cycles DESC;


// ----------------------------------------------------------------------------
// Q16 - TEST IMPACT ACROSS REPOSITORIES
//
// "This change is in a shared library. Which test suites - in which repos -
// actually exercise it?"
//
// The cross-repo half is the part no single-repo tool can answer: a test in
// mimir that covers a change in dskit is invisible to an extractor indexing
// either one alone.
//
// Test files are identified by path convention rather than by a marker in the
// graph, so the pattern list is the thing to adjust for another codebase.
//
// :param changed => a symbol id
// ----------------------------------------------------------------------------
MATCH (changed:Symbol {id: $changed})
CALL (changed) {
    MATCH (s:Symbol)-[:CALLS|CALLS_CROSS_REPO*1..5]->(changed)
    RETURN s.repo AS repo, s.path AS path
  UNION
    MATCH (f:File)-[:IMPORTS_CROSS_REPO]->(changed)
    RETURN f.repo AS repo, f.path AS path
}
WITH repo, path
WHERE path =~ '(?i).*(_test\\.go|test_.*\\.py|.*_test\\.py|/tests?/.*|.*\\.spec\\..*)'
WITH split(repo, '/')[-1]     AS repo,
     count(DISTINCT path)     AS test_files,
     collect(DISTINCT path)[0..4] AS examples
RETURN repo, test_files, examples
ORDER BY test_files DESC;


// ----------------------------------------------------------------------------
// Q17 - CROSS-BOUNDARY DEPENDENCY CYCLES   (requires `make gds` for sccXRepoId)
//
// The one finding in this file that NO single-repo tool can produce even in
// principle. Every other query is a better answer to a question the tools can
// at least ask; this one is a question they cannot express, because when either
// side is parsed the other end of the edge is out of scope.
//
// WHY THIS IS WORSE THAN AN INTRA-REPO CYCLE
// ------------------------------------------
// An intra-repo import cycle is a refactor - one commit, one reviewer. A cycle
// across a published-artifact boundary is a RELEASE DEADLOCK: neither side can
// be released without a compatible version of the other already existing. Teams
// pay that permanently, in coordinated releases and pre-release pins, and no
// single CI job ever sees it.
//
// The asymmetry is the actionable part. Measured on Airflow: 109 top-level
// import statements from core into task-sdk, and 7 the other way. The cycle is
// those 7. Cut them and a mutual dependency becomes a clean layering - so the
// query reports the minority direction with file:line, which is a work item
// rather than an observation.
//
// `declared` cross-checks the code finding against the manifests: both
// pyproject.toml files require each other, so the cycle is not an artifact of
// our resolution. Code and manifest agreeing is the strongest form this can take.
// ----------------------------------------------------------------------------
// IMPORTS_FILE_CROSS_REPO, not the symbol-mediated IMPORTS_CROSS_REPO: this edge
// is resolved by exact module path with no extractor in the path, so the counts
// are not hostage to how completely a tool enumerated declarations. It also
// matters here specifically - the biggest crossing import in this corpus goes
// through a facade `__init__.py` that declares almost nothing.
// Top-level only. An `if TYPE_CHECKING:` import across the boundary is design
// coupling, not a release deadlock, and counting it here would be the same
// mistake the intra-repo query used to make.
MATCH (a:File)-[x:IMPORTS_FILE_CROSS_REPO]->(b:File)
WHERE x.context = 'toplevel'
WITH a.repo AS src_repo, b.repo AS dst_repo,
     count(DISTINCT x)                       AS import_edges,
     count(DISTINCT a.path)                  AS importing_files,
     // Split the sites by whether the code already copes with the import
     // failing. Only the unguarded ones are work.
     collect(DISTINCT CASE WHEN NOT coalesce(x.guarded, false)
                           THEN a.path + ':' + toString(x.line) END)  AS hard_raw,
     collect(DISTINCT CASE WHEN coalesce(x.guarded, false)
                           THEN a.path + ':' + toString(x.line) END)  AS guarded_raw
WITH src_repo, dst_repo, import_edges, importing_files,
     [s IN hard_raw    WHERE s IS NOT NULL] AS hard_sites,
     [s IN guarded_raw WHERE s IS NOT NULL] AS guarded_sites
WITH collect({src: src_repo, dst: dst_repo, edges: import_edges,
              files: importing_files,
              hard: hard_sites, guarded: guarded_sites}) AS flows
UNWIND flows AS forward
// The reverse flow is what makes it a cycle. Reaching for it inside the same
// collected list keeps this to one pass over the cross-repo edges.
WITH flows, forward,
     head([f IN flows WHERE f.src = forward.dst AND f.dst = forward.src]) AS back
WHERE back IS NOT NULL
  AND forward.src < forward.dst          // report each pair once, not twice
// Manifest corroboration, and the count of modules SCC says are entangled
// across the boundary.
CALL (forward, back) {
  OPTIONAL MATCH (ra:Repo {id: forward.src})-[:DEPENDS_ON_REPO]->(rb:Repo {id: forward.dst})
  OPTIONAL MATCH (rb2:Repo {id: forward.dst})-[:DEPENDS_ON_REPO]->(ra2:Repo {id: forward.src})
  RETURN (ra IS NOT NULL AND rb2 IS NOT NULL) AS declared_both_ways
}
WITH forward, back, declared_both_ways
CALL (forward, back) {
  MATCH (f:File) WHERE f.repo IN [forward.src, forward.dst] AND f.sccXRepoId IS NOT NULL
  WITH f.sccXRepoId AS scc, count(*) AS n, count(DISTINCT f.repo) AS repos
  WHERE n > 1 AND repos > 1
  RETURN sum(n) AS entangled_modules, count(*) AS spanning_components
}
RETURN split(forward.src, '/')[-1]  AS distribution_a,
       split(forward.dst, '/')[-1]  AS distribution_b,
       forward.edges                AS a_to_b_imports,
       back.edges                   AS b_to_a_imports,
       declared_both_ways,
       entangled_modules,
       spanning_components,
       // The cut set: the smaller direction is the cheaper one to remove, and
       // within it only the UNGUARDED sites are actual work.
       CASE WHEN back.edges <= forward.edges
            THEN split(forward.dst, '/')[-1] + ' -> ' + split(forward.src, '/')[-1]
            ELSE split(forward.src, '/')[-1] + ' -> ' + split(forward.dst, '/')[-1] END
                                    AS cut_direction,
       CASE WHEN back.edges <= forward.edges THEN back.hard
            ELSE forward.hard END   AS cut_sites_must_fix,
       CASE WHEN back.edges <= forward.edges THEN back.guarded
            ELSE forward.guarded END AS cut_sites_already_guarded
ORDER BY entangled_modules DESC, a_to_b_imports DESC;


// ----------------------------------------------------------------------------
// Q18 - REACHABLE VULNERABILITIES   (requires `make vulns`)
//
// The join an SCA tool cannot make and a code graph has no data for.
//
// An SCA tool reports "you depend on X, and X has CVE-Y". It never parsed your
// source, so it cannot say whether you touch X at all. A code graph knows every
// import site but has no advisory feed. Put both in one store and the finding
// becomes "X has CVE-Y, you pinned an affected version, AND these 4 files
// import it" - with file:line.
//
// THREE CELLS, AND THE THIRD IS THE ONE NOBODY ELSE HAS
// -----------------------------------------------------
//   declared + imported      real exposure, with import sites
//   declared + NOT imported  unused dependency - attack surface you can delete
//   imported + NOT declared  PHANTOM (see Q8). Not in the SBOM, so an SCA scan
//                            of this repo never attributes the CVE to it.
//
// STATUS IS GRADED, AND `indeterminate` IS NOT A BUG
// --------------------------------------------------
// We know what a manifest DECLARES, not what a build RESOLVED. An exact pin
// gives a definite verdict; a floating range (`>=5.25.0,<7.0.0`) genuinely
// cannot be decided until something resolves it. That is a security finding in
// its own right, not a gap in the scan - so the three statuses are never
// collapsed into one number. See vulns.py.
// ----------------------------------------------------------------------------
MATCH (r:Repo)-[a:AFFECTED_BY]->(v:Vulnerability)
WHERE a.status IN ['affected', 'indeterminate']
  AND v.withdrawn IS NULL
MATCH (p:Package {id: a.package})
// Reachability: does this repo's code actually import the package, at a point
// that executes on import? `context` matters here for the same reason it does
// for cycles - a TYPE_CHECKING-only import is not a runtime exposure.
CALL (r, p) {
  MATCH (f:File {repo: r.id})-[i:IMPORTS_EXT]->(e:ExternalRef)
  WHERE e.root_module = p.name AND i.context = 'toplevel'
  RETURN count(DISTINCT f)                                        AS importing_files,
         collect(DISTINCT f.path + ':' + toString(i.line))[0..4]  AS import_sites,
         collect(DISTINCT e.symbol)[0..6]                          AS symbols_used
}
// Collapse by advisory identity. OSV aggregates several databases, so one CVE
// arrives as a GHSA record AND a PYSEC record - the same finding twice, with
// severity populated on only one of them. Grouping on the CVE (falling back to
// the OSV id when there is no CVE alias) and taking the best-populated severity
// is what stops the output double-counting.
WITH split(r.id, '/')[-1]       AS repo,
     p.name                     AS package,
     coalesce(v.cve, v.id)      AS advisory,
     a.status                   AS status,
     a.declared                 AS declared,
     importing_files, import_sites, symbols_used,
     collect(DISTINCT v.severity) AS severities
WITH repo, package, advisory, status, declared,
     importing_files, import_sites, symbols_used,
     head([s IN ['CRITICAL','HIGH','MODERATE','LOW'] WHERE s IN severities]) AS severity
RETURN repo, package, advisory, severity, status, declared,
       importing_files,
       // NOT "unused". Zero direct imports does not mean the package is
       // unused - urllib3 shows zero here while `requests` uses it internally
       // on every call. Telling someone to drop it on this evidence would break
       // their build. What this actually narrows is *first-party* exposure:
       // whether YOUR code touches the vulnerable surface directly.
       CASE WHEN importing_files = 0 THEN 'no direct import - may still be used transitively'
            WHEN status = 'affected' THEN 'REACHABLE and affected - act on this'
            ELSE 'reachable, but status needs the version resolved' END AS assessment,
       symbols_used,
       import_sites
// Definite findings first, then by how much code touches them. The reverse -
// which the first version did - buries a confirmed hit under a widely-imported
// maybe.
ORDER BY CASE status WHEN 'affected' THEN 0 ELSE 1 END,
         CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                       WHEN 'MODERATE' THEN 2 ELSE 3 END,
         importing_files DESC
LIMIT 30;


// ----------------------------------------------------------------------------
// Q19 - VULNERABILITIES IN A PHANTOM DEPENDENCY
//
// The cell neither tool category can reach. A package this repo IMPORTS but
// never DECLARES, that also carries a known advisory.
//
// Why it is invisible elsewhere: an SCA tool reads the manifest, and the package
// is not in it, so the CVE is never attributed to this repo. A single-repo code
// graph sees the import but has no advisory feed and no sibling repo to resolve
// it against. The finding exists only where manifests, source, and an advisory
// feed are in the same store.
// ----------------------------------------------------------------------------
MATCH (consumer:Repo)-[:CONTAINS]->(f:File)-[i:IMPORTS_EXT]->(e:ExternalRef)
WHERE i.context = 'toplevel'
MATCH (v:Vulnerability)-[:AFFECTS]->(p:Package {name: e.root_module})
WHERE v.withdrawn IS NULL
  AND NOT (consumer)-[:DEPENDS_ON]->(p)
  // A package does not declare itself. Without this, SQLAlchemy's own absolute
  // self-imports (`from sqlalchemy import ...`) read as an undeclared
  // dependency on itself - a false positive, and an obvious one on stage.
  AND NOT (consumer)-[:PUBLISHES]->(p)
WITH consumer, p,
     count(DISTINCT v)                              AS advisories,
     collect(DISTINCT coalesce(v.cve, v.id))[0..5]  AS examples,
     collect(DISTINCT f.path)                       AS all_files
// Test-only imports are a much weaker finding: a missing test dependency is a
// CI problem, not shipped exposure. Separated rather than dropped, because
// "only tests import it" is itself the useful answer.
WITH consumer, p, advisories, examples, all_files,
     [x IN all_files WHERE NOT x =~ '(?i).*(^|/)(tests?|conftest)(/|\\.|_).*'
                       AND NOT x =~ '(?i).*(_test\\.py|test_.*\\.py)$'] AS shipped
RETURN split(consumer.id, '/')[-1]  AS repo,
       p.name                       AS undeclared_package,
       advisories,
       examples,
       size(shipped)                AS shipped_files,
       size(all_files)              AS total_files,
       CASE WHEN size(shipped) = 0 THEN 'test-only - a CI dependency gap, not shipped exposure'
            ELSE 'SHIPPED CODE imports an undeclared package with known CVEs' END AS assessment,
       shipped[0..4]                AS example_files
ORDER BY size(shipped) DESC, advisories DESC
LIMIT 20;
