// ============================================================================
// ORG-LEVEL GRAPH DATA SCIENCE
//
// GitNexus and Graphify both run Leiden already - per repo. Same algorithm,
// but a per-repo projection can only ever produce intra-repo communities. Run
// it over the joined graph and the answer changes kind: subsystems that cut
// ACROSS repository boundaries, chokepoints ranked against the whole estate,
// and the seams whose removal actually disconnects it.
//
// That is not a better version of what the tools do. It is a question they
// cannot express, because the projection they would need does not exist inside
// any one of them.
//
// Requires GDS Enterprise (gds.license at the repo root). Verify with:
//   CALL gds.license.state() YIELD isLicensed RETURN isLicensed;
// ============================================================================


// ----------------------------------------------------------------------------
// Projection.
//
// Heterogeneous on purpose. Symbol->Symbol edges alone would barely cross repo
// boundaries: CALLS_CROSS_REPO is small, because only codegraph's artifact
// supports symbol-level cross-repo calls. The bulk of the cross-repo signal is
// IMPORTS_CROSS_REPO, which runs File->Symbol - so Files are projected too and
// DECLARES stitches each file to the symbols it holds.
//
// UNDIRECTED throughout: Leiden and articulationPoints require it, and for
// "what clusters with what" the direction of a call is not the question.
// ----------------------------------------------------------------------------
// A Cypher projection, not a native one. The native form names relationship
// types up front and fails outright if any is absent - which it is whenever the
// cross-repo pass has not run, or when only a single-repo corpus is loaded
// (corpus C). Projecting from a MATCH takes whatever exists.
CALL gds.graph.drop('codekg', false) YIELD graphName;

MATCH (src)-[r]->(dst)
WHERE (src:Symbol OR src:File) AND (dst:Symbol OR dst:File)
  AND type(r) IN ['CALLS', 'CALLS_CROSS_REPO', 'IMPORTS_CROSS_REPO', 'DECLARES']
WITH gds.graph.project(
  'codekg', src, dst,
  {},
  {undirectedRelationshipTypes: ['*']}   // Leiden and articulationPoints require it
) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;


// ----------------------------------------------------------------------------
// Leiden - subsystem detection over the whole estate.
// ----------------------------------------------------------------------------
CALL gds.leiden.write('codekg', {
  writeProperty: 'communityId',
  randomSeed: 42,              // deterministic: a demo that renumbers between
  maxLevels: 10,               // runs is not a demo
  tolerance: 0.0001
}) YIELD communityCount, modularity, ranLevels, didConverge;


// ----------------------------------------------------------------------------
// Betweenness - the estate's real chokepoints.
//
// Sampled. Exact betweenness is O(V*E), which on ~166k nodes and ~500k
// relationships is minutes of CPU for a ranking that barely moves.
// ----------------------------------------------------------------------------
CALL gds.betweenness.write('codekg', {
  writeProperty: 'betweenness',
  samplingSize: 2000,
  samplingSeed: 42
}) YIELD nodePropertiesWritten, computeMillis;


// ----------------------------------------------------------------------------
// PageRank - influence, as distinct from brokerage.
// ----------------------------------------------------------------------------
CALL gds.pageRank.write('codekg', {
  writeProperty: 'pagerank',
  maxIterations: 20,
  dampingFactor: 0.85
}) YIELD ranIterations, didConverge;


// ----------------------------------------------------------------------------
// Articulation points - the fragile seams.
//
// A node whose removal splits the graph into more components. On a multi-repo
// code graph these are the single points of structural failure for the estate.
// ----------------------------------------------------------------------------
CALL gds.articulationPoints.write('codekg', {
  writeProperty: 'isArticulationPoint'
}) YIELD articulationPointCount, nodePropertiesWritten;


// ----------------------------------------------------------------------------
// Strongly connected components over module imports - the cycle pre-filter.
//
// A SEPARATE projection from 'codekg', and deliberately DIRECTED. SCC is
// meaningless on the undirected projection above: reciprocity is what makes a
// cycle, so orientation must be preserved.
//
// Why SCC at all: naive cycle search does not scale. On this graph
// `MATCH (a:File)-[:IMPORTS*2..5]->(a)` returns in 18 ms, but *2..6 does not
// finish in 90 seconds. SCC is O(V+E) and answers the prior question
// completely - which nodes can be in a cycle at all - so enumeration afterwards
// only ever runs on a tiny subgraph.
//
// THE EDGE FILTER IS THE WHOLE FIX. `context = 'toplevel'` restricts the
// projection to imports that actually EXECUTE on import. Without it the result
// is not conservative, it is wrong: the driver was reported as one 102-module
// component with an 11-hop cycle, and two of the three edges in the part we
// were quoting are `if TYPE_CHECKING:` blocks that never run. Classified
// top-level and facade-free, the driver has ZERO cycles.
// See loader/src/codekg/internal_imports.py.
//
// Markdown never reaches these projections either. Graphify indexes .md and
// treats a link as an import, which made documentation cross-links look like
// dependency cycles in mimir and loki.
//
// THREE projections, because one number cannot carry the answer:
//
//   modules        toplevel only. Executes on import. Includes __init__.py, so
//                  it is the like-for-like comparison against pylint, which
//                  makes no facade distinction. -> sccId
//
//   modules_core   toplevel, EXCLUDING __init__.py. A package facade that
//                  re-exports from submodules which import back from the
//                  package root is idiomatic and not a defect, but it creates
//                  enormous components. THIS IS THE NUMBER TO ACT ON. -> sccCoreId
//
//   modules_design toplevel + TYPE_CHECKING, facade-free. Design-time coupling,
//                  already broken at runtime. Real architectural information,
//                  but never a runtime risk - do not present it as one.
//                  -> sccDesignId
// ----------------------------------------------------------------------------
CALL gds.graph.drop('modules', false) YIELD graphName;

MATCH (src:File)-[r:IMPORTS]->(dst:File)
WHERE src.language IN ['py', 'go'] AND dst.language IN ['py', 'go']
  AND r.context = 'toplevel'
WITH gds.graph.project('modules', src, dst) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;

// Clear first. gds.scc.write only writes nodes that are IN the projection; it
// does not remove the property from nodes that have since dropped out. After
// narrowing the projection to code files, every Markdown and shell file kept a
// stale sccId from the previous run and went on being reported as cyclic.
MATCH (f:File) WHERE f.sccId IS NOT NULL REMOVE f.sccId;

CALL gds.scc.write('modules', {writeProperty: 'sccId'})
YIELD componentCount, computeMillis;

CALL gds.graph.drop('modules_core', false) YIELD graphName;

MATCH (src:File)-[r:IMPORTS]->(dst:File)
WHERE src.language IN ['py', 'go'] AND dst.language IN ['py', 'go']
  AND r.context = 'toplevel'
  AND NOT src.path ENDS WITH '__init__.py'
  AND NOT dst.path ENDS WITH '__init__.py'
WITH gds.graph.project('modules_core', src, dst) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;

MATCH (f:File) WHERE f.sccCoreId IS NOT NULL REMOVE f.sccCoreId;

CALL gds.scc.write('modules_core', {writeProperty: 'sccCoreId'})
YIELD componentCount, computeMillis;

// EVERY context, facades included - the arm that reproduces what a
// context-blind tool sees. pylint's cyclic-import counts function-scoped
// imports, so it reports cycles that the standard cycle-breaking idiom has
// already broken. Having this arm turns "our precision looks bad" into a
// measured explanation of exactly which edges account for the difference.
CALL gds.graph.drop('modules_all_ctx', false) YIELD graphName;

MATCH (src:File)-[r:IMPORTS]->(dst:File)
WHERE src.language IN ['py', 'go'] AND dst.language IN ['py', 'go']
  AND r.context IS NOT NULL
WITH gds.graph.project('modules_all_ctx', src, dst) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;

MATCH (f:File) WHERE f.sccAllCtxId IS NOT NULL REMOVE f.sccAllCtxId;

CALL gds.scc.write('modules_all_ctx', {writeProperty: 'sccAllCtxId'})
YIELD componentCount, computeMillis;

// Facade-INCLUSIVE design-time, so the scoring arms form a clean 2x2 (facades
// on/off x typing on/off) and vary one thing at a time. Without this, comparing
// "runtime with facades" against "runtime + design-time facade-free" moves two
// variables at once and the result is uninterpretable - which is exactly what
// the first version of score-cycles did.
CALL gds.graph.drop('modules_all_design', false) YIELD graphName;

MATCH (src:File)-[r:IMPORTS]->(dst:File)
WHERE src.language IN ['py', 'go'] AND dst.language IN ['py', 'go']
  AND r.context IN ['toplevel', 'typing']
WITH gds.graph.project('modules_all_design', src, dst) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;

MATCH (f:File) WHERE f.sccAllDesignId IS NOT NULL REMOVE f.sccAllDesignId;

CALL gds.scc.write('modules_all_design', {writeProperty: 'sccAllDesignId'})
YIELD componentCount, computeMillis;

CALL gds.graph.drop('modules_design', false) YIELD graphName;

MATCH (src:File)-[r:IMPORTS]->(dst:File)
WHERE src.language IN ['py', 'go'] AND dst.language IN ['py', 'go']
  AND r.context IN ['toplevel', 'typing']
  AND NOT src.path ENDS WITH '__init__.py'
  AND NOT dst.path ENDS WITH '__init__.py'
WITH gds.graph.project('modules_design', src, dst) AS g
RETURN g.graphName AS graphName, g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount;

MATCH (f:File) WHERE f.sccDesignId IS NOT NULL REMOVE f.sccDesignId;

CALL gds.scc.write('modules_design', {writeProperty: 'sccDesignId'})
YIELD componentCount, computeMillis;
