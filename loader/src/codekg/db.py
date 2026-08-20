"""Neo4j write path.

Everything is MERGE on a deterministic id and batched through UNWIND. Two
consequences that matter:

  - Re-running a load is a no-op rather than a duplicate.
  - The same node arriving from a second extractor updates the `extractors`
    list rather than creating a parallel node, which is what makes the
    agreement matrix a single property read instead of a second pipeline.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from neo4j import GraphDatabase


BATCH = 5_000


class Store:
    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None):
        self._driver = GraphDatabase.driver(
            uri or os.environ["NEO4J_URI"],
            auth=(
                user or os.environ.get("NEO4J_USER", "neo4j"),
                password or os.environ["NEO4J_PASSWORD"],
            ),
        )

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def run_script(self, path: str) -> list[list[dict[str, Any]]]:
        """Execute a semicolon-delimited .cypher file, statement by statement.

        Comments are stripped BEFORE splitting on ';'. Doing it the other way
        round - which this did originally - means a semicolon inside a comment
        splits the file mid-sentence and the remainder of the comment is handed
        to the server as a statement. That failure is confusing to read, because
        the syntax error names a word from prose.

        Still not a real lexer: a ';' inside a string literal would break it.
        These files contain none.
        """
        with open(path) as fh:
            body = fh.read()

        without_comments = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("//")
        )

        results = []
        for raw in without_comments.split(";"):
            stmt = raw.strip()
            if not stmt:
                continue
            with self._driver.session() as session:
                results.append([dict(r) for r in session.run(stmt)])
        return results

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session() as session:
            return [dict(r) for r in session.run(cypher, **params)]

    def write_batched(self, cypher: str, rows: Iterable[dict[str, Any]]) -> int:
        total = 0
        for chunk in _chunks(rows, BATCH):
            with self._driver.session() as session:
                session.execute_write(lambda tx: tx.run(cypher, batch=chunk).consume())
            total += len(chunk)
        return total


# --- MERGE statements for the canonical schema -------------------------------
#
# `extractors` accumulates rather than overwrites. That single decision is what
# turns triangulation from a separate pipeline into a property read: an edge
# seen by all three tools has size(r.extractors) = 3, one seen by a single tool
# has 1, and the agreement matrix is a GROUP BY.

MERGE_REPO = """
UNWIND $batch AS row
MERGE (r:Repo {id: row.id})
  SET r.name       = row.name,
      r.url        = row.url,
      r.commit     = row.commit,
      r.ecosystem  = row.ecosystem,
      r.corpus     = row.corpus,
      r.extractors = coll.distinct(coalesce(r.extractors, []) + row.extractor)
"""

MERGE_PACKAGE = """
UNWIND $batch AS row
MERGE (p:Package {id: row.id})
  SET p.name = row.name, p.ecosystem = row.ecosystem
WITH p, row
MATCH (r:Repo {id: row.repo})
FOREACH (_ IN CASE WHEN row.relation = 'PUBLISHES' THEN [1] ELSE [] END |
  MERGE (r)-[:PUBLISHES]->(p))
FOREACH (_ IN CASE WHEN row.relation = 'DEPENDS_ON' THEN [1] ELSE [] END |
  MERGE (r)-[d:DEPENDS_ON]->(p)
    SET d.version_spec = row.version_spec, d.source = row.source)
"""

MERGE_FILE = """
UNWIND $batch AS row
MERGE (f:File {id: row.id})
  SET f.path       = row.path,
      f.repo       = row.repo,
      f.language   = row.language,
      f.extractors = coll.distinct(coalesce(f.extractors, []) + row.extractor)
WITH f, row
MATCH (r:Repo {id: row.repo})
MERGE (r)-[:CONTAINS]->(f)
"""

MERGE_SYMBOL = """
UNWIND $batch AS row
MERGE (s:Symbol {id: row.id})
  SET s.name       = row.name,
      s.qname      = row.qname,
      s.kind       = row.kind,
      s.path       = row.path,
      s.repo       = row.repo,
      s.start_line = row.start_line,
      s.end_line   = row.end_line,
      s.exported   = row.exported,
      s.docstring  = row.docstring,
      s.extractors = coll.distinct(coalesce(s.extractors, []) + row.extractor)
WITH s, row
MATCH (r:Repo {id: row.repo})
MERGE (s)-[:IN_REPO]->(r)
WITH s, row
MATCH (f:File {id: row.file})
MERGE (f)-[:DECLARES]->(s)
"""

MERGE_CALLS = """
UNWIND $batch AS row
MATCH (src:Symbol {id: row.src})
MATCH (dst:Symbol {id: row.dst})
MERGE (src)-[c:CALLS]->(dst)
  SET c.extractors = coll.distinct(coalesce(c.extractors, []) + row.extractor),
      c.confidence = coalesce(row.confidence, c.confidence),
      c.evidence   = coalesce(row.evidence, c.evidence)
"""

MERGE_EXTERNAL_REF = """
UNWIND $batch AS row
MERGE (e:ExternalRef {id: row.id})
  SET e.module      = row.module,
      e.root_module = row.root_module,
      e.symbol      = row.symbol,
      e.ecosystem   = row.ecosystem,
      e.extractors  = coll.distinct(coalesce(e.extractors, []) + row.extractor)
WITH e, row
MATCH (src:Symbol {id: row.src})
MERGE (src)-[u:USES]->(e)
  SET u.extractors = coll.distinct(coalesce(u.extractors, []) + row.extractor)
"""

# Source-derived imports land on the File, not a Symbol - the file is the
# granularity at which an import can be attributed without redoing call
# resolution. See enrich.py.
MERGE_FILE_EXTERNAL_REF = """
UNWIND $batch AS row
MERGE (e:ExternalRef {id: row.id})
  SET e.module      = row.module,
      e.root_module = row.root_module,
      e.symbol      = row.symbol,
      e.ecosystem   = row.ecosystem,
      e.extractors  = coll.distinct(coalesce(e.extractors, []) + row.extractor)
WITH e, row
MATCH (f:File {id: row.file})
MERGE (f)-[u:IMPORTS_EXT]->(e)
  SET u.line       = row.line,
      // Same toplevel/typing/deferred classification as intra-repo IMPORTS, and
      // carried onto IMPORTS_FILE_CROSS_REPO by the linking pass. A cross-repo
      // cycle held together by an `if TYPE_CHECKING:` import is not a runtime
      // cycle, and counting it as one would repeat the intra-repo mistake at a
      // bigger blast radius.
      u.context    = coalesce(row.context, u.context),
      u.guarded    = coalesce(row.guarded, u.guarded),
      u.extractors = coll.distinct(coalesce(u.extractors, []) + row.extractor)
"""

# Intra-repo module dependencies. Distinct from IMPORTS_EXT, which points at an
# :ExternalRef outside the repo. These are File->File and they are what makes
# circular-import detection possible - cycles live at module level far more
# often than at package or symbol level.
#
# `context` is toplevel | typing | deferred and is what makes cycle detection
# trustworthy - see internal_imports.py. coalesce() rather than plain assignment
# because extractor-derived rows carry no context and must not erase the value
# the source pass set.
MERGE_FILE_IMPORTS = """
UNWIND $batch AS row
MATCH (src:File {id: row.src})
MATCH (dst:File {id: row.dst})
MERGE (src)-[i:IMPORTS]->(dst)
  SET i.extractors = coll.distinct(coalesce(i.extractors, []) + row.extractor),
      i.context    = coalesce(row.context, i.context),
      i.guarded    = coalesce(row.guarded, i.guarded),
      i.line       = coalesce(row.line, i.line)
"""

# The dotted module name a Python file is importable as. Set by `enrich` from
# the same AST pass that classifies imports.
#
# This is what makes exact cross-repo resolution possible when the import name
# and the distribution name differ - and it is the ONLY thing that works when
# two distributions publish into one namespace package, as Airflow's core and
# task-sdk both do under `airflow`. Matching the top-level module against
# declared packages cannot separate them; matching the full module path against
# the file that actually provides it is exact.
SET_FILE_MODULE = """
UNWIND $batch AS row
MATCH (f:File {id: row.file})
SET f.module = row.module
"""

DELETE_REPO_SUBGRAPH = """
MATCH (r:Repo {id: $repo})
OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
OPTIONAL MATCH (f)-[:DECLARES]->(s:Symbol)
DETACH DELETE f, s
WITH r
DETACH DELETE r
"""


@contextmanager
def store(**kwargs: Any) -> Iterator[Store]:
    s = Store(**kwargs)
    try:
        s.verify()
        yield s
    finally:
        s.close()


def _chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    buf: list[dict[str, Any]] = []
    for row in rows:
        buf.append(row)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
