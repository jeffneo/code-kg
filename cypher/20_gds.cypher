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
CALL gds.graph.drop('codekg', false) YIELD graphName;

CALL gds.graph.project(
  'codekg',
  ['Symbol', 'File'],
  {
    CALLS:              {orientation: 'UNDIRECTED'},
    CALLS_CROSS_REPO:   {orientation: 'UNDIRECTED'},
    IMPORTS_CROSS_REPO: {orientation: 'UNDIRECTED'},
    DECLARES:           {orientation: 'UNDIRECTED'}
  }
) YIELD graphName, nodeCount, relationshipCount;


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
// Why this exists: naive cycle search does not scale. On this graph
// `MATCH (a:File)-[:IMPORTS*2..5]->(a)` returns in 18 ms, but *2..6 does not
// finish in 90 seconds - the path space explodes combinatorially. SCC is
// O(V+E) and answers the prior question completely: which nodes can be in a
// cycle at all. Every cycle in the graph lies inside one strongly connected
// component, by definition, so enumeration afterwards only ever runs on a tiny
// subgraph.
//
// Measured: 13,120 files and 90,104 import edges reduce to 13 cyclic
// components holding 135 files - a 97x reduction, computed in 15 ms.
// ----------------------------------------------------------------------------
CALL gds.graph.drop('modules', false) YIELD graphName;

CALL gds.graph.project('modules', ['File'], {IMPORTS: {orientation: 'NATURAL'}})
YIELD graphName, nodeCount, relationshipCount;

CALL gds.scc.write('modules', {writeProperty: 'sccId'})
YIELD componentCount, computeMillis;
