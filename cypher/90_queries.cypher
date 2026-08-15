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
