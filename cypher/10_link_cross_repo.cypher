// ============================================================================
// CROSS-REPO RESOLUTION
//
// This file is the entire point of the exercise. Everything above it is
// translation of work the extractors already did. Nothing in here can be
// produced by any single-repo tool, because the other end of every edge it
// creates was out of scope when the extractor ran.
//
// The pass runs in three steps, most-certain first, and every derived edge
// records how it was inferred and how much to trust it. Confidence is not a
// disclaimer - it is what lets a reviewer separate a fact from a guess.
// ============================================================================


// ----------------------------------------------------------------------------
// Step 1: repo-level dependency edges.
//
// Certain, because it is read directly from a manifest. Also the least
// interesting result in the file - repo-level dependency graphs are a solved
// problem and every SCA tool on the market emits one. It exists here only as
// the scaffold that steps 2 and 3 hang off.
// ----------------------------------------------------------------------------
MATCH (consumer:Repo)-[dep:DEPENDS_ON]->(pkg:Package)<-[:PUBLISHES]-(publisher:Repo)
WHERE consumer <> publisher
MERGE (consumer)-[link:DEPENDS_ON_REPO]->(publisher)
  ON CREATE SET link.via            = [pkg.name],
                link.method         = 'manifest',
                link.confidence     = 1.0,
                link.cross_repo     = true
  ON MATCH  SET link.via            = coll.distinct(link.via + pkg.name);


// ----------------------------------------------------------------------------
// Step 2: bind dangling imports to real symbols.
//
// Each extractor produced an :ExternalRef for every import it could not
// resolve inside its own repo. Now that the sibling repos are in the same
// store, most of those can be bound to an actual symbol.
//
// Confidence is graded by how the module path matched:
//   1.0  Go     - import paths are fully qualified and name the publishing
//                 repo, so a longest-prefix match is exact by construction.
//   0.8  Python - the top-level module maps to a declared package, and the
//                 symbol name is unique within the publishing repo.
//   0.5  Python - same, but the symbol name is ambiguous within that repo.
//                 Re-exports through __init__.py are the usual cause.
// ----------------------------------------------------------------------------

// -- Go: exact, by longest-prefix match on the module path.
MATCH (ref:ExternalRef {ecosystem: 'go'})
MATCH (publisher:Repo)-[:PUBLISHES]->(pkg:Package {ecosystem: 'go'})
WHERE ref.module STARTS WITH pkg.name
WITH ref, publisher, pkg
ORDER BY size(pkg.name) DESC
WITH ref, head(collect({repo: publisher, pkg: pkg})) AS best
// The import path names the exact package, not just the repo. Strip the
// publisher's module prefix to get the package directory inside it:
//   github.com/grafana/dskit/kv/consul  ->  kv/consul
WITH ref, best,
     CASE WHEN ref.module = best.pkg.name THEN ''
          ELSE substring(ref.module, size(best.pkg.name) + 1) END AS pkgdir
MATCH (target:Symbol {repo: best.repo.id})
WHERE target.name = ref.symbol
  AND target.exported = true
  // Constrain to the package the import path actually names. Without this,
  // `ring.Config` binds to every Config in the repo - and dskit defines nine,
  // one per package. The scoring harness cannot catch that: its truth tuples
  // are keyed on symbol name with no package, so precision still reads 1.000
  // while eight of nine edges are wrong.
  AND CASE WHEN pkgdir = '' THEN NOT target.path CONTAINS '/'
           ELSE target.path STARTS WITH pkgdir + '/' END
  // A Go cross-package reference is `pkg.Symbol`, and a method can never be
  // reached that way - only package-level funcs, types, vars and consts can.
  //
  // This matters because Go code habitually shadows a package name with a
  // variable of the same name (`backoff := backoff.New(...)`, then
  // `backoff.Ongoing()`). An extractor without scope information reads that
  // selector head as the package, and the reference binds to a method. Every
  // one of the 89 false positives measured on corpus B was this shape.
  AND target.kind <> 'method'
MERGE (ref)-[r:RESOLVES_TO]->(target)
SET r.method     = 'go-module-prefix',
    r.confidence = 1.0,
    r.pkgdir     = pkgdir,
    r.cross_repo = true;

// -- Python: match the top-level module to a declared package, then the symbol
//    name within the publishing repo.
MATCH (ref:ExternalRef {ecosystem: 'python'})
WHERE ref.symbol IS NOT NULL
MATCH (publisher:Repo)-[:PUBLISHES]->(pkg:Package {ecosystem: 'python'})
WHERE pkg.name = ref.root_module
MATCH (target:Symbol {repo: publisher.id})
WHERE target.name = ref.symbol
  AND target.exported = true
WITH ref, collect(DISTINCT target) AS candidates
WHERE size(candidates) > 0
UNWIND candidates AS target
MERGE (ref)-[r:RESOLVES_TO]->(target)
  ON CREATE SET r.method     = 'python-package-symbol',
                r.confidence = CASE WHEN size(candidates) = 1 THEN 0.8 ELSE 0.5 END,
                r.ambiguous  = size(candidates) > 1,
                r.candidates = size(candidates),
                r.cross_repo = true;


// -- Python: exact, by full module path.
//
// The Python analogue of the Go longest-prefix rule, and strictly better than
// the top-level-module rule above wherever it applies: `File.module` is the
// dotted name that file is importable as, so matching an unresolved import's
// full module path against it identifies the providing file exactly.
//
// This exists because the top-level rule cannot work at all when two
// separately published distributions share one namespace package. Airflow's
// apache-airflow-core and apache-airflow-task-sdk both publish under `airflow`,
// so `from airflow.sdk import ...` has root module `airflow`, which matches
// NEITHER distribution name. Declaring both as publishing `airflow` would be
// worse than useless - every import would match both repos and the tool would
// manufacture the very cycle it is supposed to discover.
//
// Runs after the top-level rule so that its ON CREATE wins where both apply.
MATCH (ref:ExternalRef {ecosystem: 'python'})
WHERE ref.symbol IS NOT NULL AND ref.module IS NOT NULL
MATCH (provider:File {module: ref.module})
// enrich.py emits EVERY absolute import, including ones that resolve inside the
// importing repo, so without this guard the rule also binds a repo's own
// internal imports to its own files. Those are not wrong, they are just not
// this pass's job, and they would bury the cross-repo edges in noise. Restrict
// to refs that some OTHER repo actually imports.
WHERE EXISTS {
  MATCH (consumer:File)-[:IMPORTS_EXT]->(ref)
  WHERE consumer.repo <> provider.repo
}
MATCH (provider)-[:DECLARES]->(target:Symbol)
WHERE target.name = ref.symbol
  AND target.exported = true
WITH ref, target, count(DISTINCT provider) AS providers
MERGE (ref)-[r:RESOLVES_TO]->(target)
  ON CREATE SET r.method     = 'python-module-path',
                // Exact when one file provides the module path. More than one
                // means a genuinely ambiguous namespace package, so say so
                // rather than asserting certainty.
                r.confidence = CASE WHEN providers = 1 THEN 1.0 ELSE 0.6 END,
                r.ambiguous  = providers > 1,
                r.providers  = providers,
                r.cross_repo = true;


// -- Python: file-level cross-boundary imports, by exact module path.
//
// Deliberately NOT symbol-mediated, and that is the point. Every other derived
// edge here needs a :Symbol at the far end, which makes it hostage to how well
// an extractor enumerated declarations. The single most common crossing import
// in corpus D is `from airflow.sdk import ...` - 56 statements - and
// `airflow/sdk/__init__.py` is a facade that re-exports from submodules, so it
// DECLARES almost none of those names. Symbol-mediated resolution misses the
// most important edge in the exhibit.
//
// This needs only two things we compute ourselves and exactly: the import
// statement (AST) and the module name each file is importable as (module_index).
// No extractor is in the path, so no extractor can weaken it.
//
// It is also the edge module-level cycle detection needs - cycles live between
// files, not between symbols. See the `modules_xrepo` projection in 20_gds.cypher.
MATCH (consumer:File)-[i:IMPORTS_EXT]->(ref:ExternalRef {ecosystem: 'python'})
WHERE ref.module IS NOT NULL
MATCH (provider:File {module: ref.module})
WHERE consumer.repo <> provider.repo
// Strongest dependency wins per file pair. Two files can be linked by several
// imports; the pair is described by its hardest link.
//
//   4  toplevel, unguarded  - executes on import, no fallback. Must fix.
//   3  toplevel, guarded    - executes, but wrapped in try/except ImportError
//                             with a working fallback. Already handled.
//   2  typing               - never executes.
//   1  deferred             - executes on call.
//
// The guarded tier is not pedantry. In corpus D it moves the actionable cut set
// from 7 import sites to 5: two of the seven sit in `except ModuleNotFoundError:`
// blocks where the SDK already copes with core being absent. Presenting all
// seven as work would have been wrong in front of a maintainer.
WITH consumer, provider, i, ref,
     CASE
       WHEN coalesce(i.context, 'toplevel') = 'toplevel'
            AND NOT coalesce(i.guarded, false) THEN 4
       WHEN coalesce(i.context, 'toplevel') = 'toplevel' THEN 3
       WHEN i.context = 'typing'                         THEN 2
       ELSE 1
     END AS rank
ORDER BY rank DESC
WITH consumer, provider,
     head(collect({ctx: coalesce(i.context, 'toplevel'),
                   guarded: coalesce(i.guarded, false),
                   line: i.line, module: ref.module})) AS best
MERGE (consumer)-[x:IMPORTS_FILE_CROSS_REPO]->(provider)
SET x.method     = 'python-module-path',
    x.confidence = 1.0,
    x.via_module = best.module,
    x.line       = best.line,
    x.context    = best.ctx,
    x.guarded    = best.guarded,
    x.cross_repo = true;


// ----------------------------------------------------------------------------
// Step 3: lift repo-level dependency to a symbol-level call path.
//
// This is the step that produces the capability no individual tool has. A
// repo-level dependency says "A uses B somewhere". This says "this function in
// A reaches that function in B", which is what blast radius actually needs.
//
// The materialised CALLS_CROSS_REPO edge is a shortcut for the two-hop path
// USES -> ExternalRef -> RESOLVES_TO -> Symbol. Materialising it keeps the
// blast-radius traversal to a single relationship type and avoids a
// variable-length pattern that alternates edge types, which is both slower to
// run and much harder to read on a projected screen.
// ----------------------------------------------------------------------------
MATCH (caller:Symbol)-[u:USES]->(ref:ExternalRef)-[r:RESOLVES_TO]->(callee:Symbol)
WHERE caller.repo <> callee.repo
MERGE (caller)-[x:CALLS_CROSS_REPO]->(callee)
SET x.method     = 'package-lift',
    x.confidence = r.confidence,
    x.via_module = ref.module,
    x.cross_repo = true,
    x.extractors = u.extractors;


// ----------------------------------------------------------------------------
// Step 4: file-level cross-repo imports.
//
// Source-derived refs (extractor 'source') hang off :File rather than :Symbol,
// because an import statement belongs to a file and attributing it to a
// particular function would mean redoing call resolution.
//
// This exists because the extractor's artifact only carries a small fraction of
// the real import surface - see enrich.py for the measurement. Without it the
// cross-repo layer is thin enough that blast radius understates badly.
// ----------------------------------------------------------------------------
MATCH (f:File)-[i:IMPORTS_EXT]->(ref:ExternalRef)-[r:RESOLVES_TO]->(target:Symbol)
WHERE f.repo <> target.repo
MERGE (f)-[x:IMPORTS_CROSS_REPO]->(target)
// Plain SET, not ON CREATE. With ON CREATE, re-running the pass after fixing
// upstream data leaves the old properties in place - which is exactly how the
// line numbers stayed null after goscan started emitting them.
SET x.method     = 'source-import',
    x.confidence = r.confidence,
    x.via_module = ref.module,
    x.line       = i.line,
    x.cross_repo = true;


// ----------------------------------------------------------------------------
// Report. Run after the pass to see what it actually created.
// ----------------------------------------------------------------------------
MATCH ()-[r]->()
WHERE r.cross_repo = true
RETURN type(r)          AS relationship,
       r.method         AS method,
       count(*)         AS edges,
       round(avg(r.confidence), 3) AS avg_confidence
ORDER BY edges DESC;
