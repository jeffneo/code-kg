"""Map CodeGraph's SQLite store onto the canonical schema.

Verified against codegraph 1.5.0. Storage is SQLite, so the ETL is a SELECT
rather than a parse:

  nodes            id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, docstring, signature, is_exported, ...
  edges            source, target, kind, metadata, line, col, provenance
  files            path, content_hash, language, size, node_count
  unresolved_refs  from_node_id, reference_name, reference_kind, file_path, ...

WHAT THIS EXTRACTOR HAS THAT GRAPHIFY DOES NOT
-----------------------------------------------
`import` nodes keep the **module path** in `qualified_name`, plus the literal
statement in `signature`:

    qualified_name = "neo4j_graphrag.schema"
    signature      = "from neo4j_graphrag.schema import format_schema"

That is precisely the field graphify discards (see importmap.py). It means
CodeGraph's artifact can support the cross-repo arm on its own, with no
source-side recovery pass - which is the sharpest difference between the two
extractors and shows up directly in the scores.

IDENTITY CONTRACT
-----------------
The agreement matrix is only meaningful if both extractors land on the same
node id, so both mappers must render a symbol's identity name identically:

    methods    Owner.member       (GraphCypherQAChain.__init__)
    otherwise  the bare name

CodeGraph gives `Owner::member` in qualified_name; graphify gives `.member()`
with the owner recoverable only from its `method` edges. Normalising both is
the whole normalization tax this harness predicted, made concrete.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .. import ids

EXTRACTOR = "codegraph"

ARTIFACT = "codegraph.db"
EXTERNAL_REF_ANCHOR = "file"

# Structural node kinds we keep. `import` and `file` are handled separately.
SYMBOL_KINDS = {
    "function", "method", "class", "interface", "struct", "enum",
    "type", "variable", "constant", "component", "route",
}

CALL_KINDS = {"calls", "instantiates", "extends", "implements"}

# CodeGraph tags some edges with how they were derived. Dynamic dispatch is a
# guess by construction and is capped so it cannot outrank a resolved call.
CONFIDENCE_BY_PROVENANCE = {
    "static": 1.0,
    "dynamic": 0.5,
    "inferred": 0.7,
}

_IMPORT_NAMES = re.compile(r"\bimport\s+(?P<names>.+)", re.DOTALL)


def load(artifact_dir: Path) -> sqlite3.Connection:
    """Open the store read-only. The file is a copy; nothing here mutates it."""
    conn = sqlite3.connect(f"file:{artifact_dir / 'codegraph.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def provenance(artifact_dir: Path) -> dict[str, Any]:
    path = artifact_dir / "provenance.json"
    return json.loads(path.read_text()) if path.exists() else {}


def files(conn: sqlite3.Connection, repo: str) -> Iterator[dict]:
    for row in conn.execute("SELECT path, language FROM files"):
        yield {
            "id": ids.file_id(repo, row["path"]),
            "path": row["path"],
            "repo": repo,
            "language": row["language"],
            "extractor": EXTRACTOR,
        }


def symbols(conn: sqlite3.Connection, repo: str, ecosystem: str) -> Iterator[dict]:
    placeholders = ",".join("?" * len(SYMBOL_KINDS))
    query = f"""
        SELECT id, kind, name, qualified_name, file_path, start_line, end_line,
               docstring, signature
        FROM nodes WHERE kind IN ({placeholders})
    """
    for row in conn.execute(query, tuple(SYMBOL_KINDS)):
        identity = _identity_name(row["qualified_name"], row["name"])
        yield {
            "id": ids.symbol_id(repo, row["file_path"], identity),
            "name": row["name"],
            "qname": identity,
            "kind": row["kind"],
            "kind_inferred": False,   # codegraph states the kind outright
            "path": row["file_path"],
            "repo": repo,
            "file": ids.file_id(repo, row["file_path"]),
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            # Deliberately our own rule, not codegraph's is_exported column,
            # which is 0 for every Python node in practice. A shared rule is
            # what keeps the agreement matrix about extraction rather than
            # about differing export conventions.
            "exported": _is_exported(row["name"], ecosystem),
            "docstring": row["docstring"],
            "extractor": EXTRACTOR,
        }


def calls(conn: sqlite3.Connection, repo: str, symbol_ids: set[str]) -> Iterator[dict]:
    placeholders = ",".join("?" * len(CALL_KINDS))
    query = f"""
        SELECT e.kind AS kind, e.provenance AS provenance, e.line AS line,
               s.qualified_name AS s_qn, s.name AS s_name, s.file_path AS s_path,
               t.qualified_name AS t_qn, t.name AS t_name, t.file_path AS t_path
        FROM edges e
        JOIN nodes s ON s.id = e.source
        JOIN nodes t ON t.id = e.target
        WHERE e.kind IN ({placeholders})
    """
    for row in conn.execute(query, tuple(CALL_KINDS)):
        src = ids.symbol_id(repo, row["s_path"], _identity_name(row["s_qn"], row["s_name"]))
        dst = ids.symbol_id(repo, row["t_path"], _identity_name(row["t_qn"], row["t_name"]))
        if src not in symbol_ids or dst not in symbol_ids:
            continue
        yield {
            "src": src,
            "dst": dst,
            "extractor": EXTRACTOR,
            "confidence": CONFIDENCE_BY_PROVENANCE.get(row["provenance"] or "static", 0.7),
            "evidence": f"{row['s_path']}:{row['line'] or '?'}",
        }


def external_refs(
    conn: sqlite3.Connection,
    repo: str,
    ecosystem: str,
    symbol_ids: set[str],
    repo_root: Path | None = None,
) -> Iterator[dict]:
    """Imports with their module intact, straight from the artifact.

    No source-side recovery needed - unlike graphify, the module survives
    extraction. `qualified_name` on an import node is the module path, and
    `signature` is the literal statement, so the imported names parse out of it.

    Refs are attached to the importing *file*, matching how the source-derived
    pass models them, since an import statement belongs to a file.
    """
    if ecosystem == "go":
        yield from _external_refs_go(conn, repo, ecosystem)
        return

    query = """
        SELECT qualified_name AS module, signature, file_path, start_line
        FROM nodes WHERE kind = 'import'
    """
    emitted: set[tuple[str, str]] = set()
    for row in conn.execute(query):
        module = row["module"]
        if not module:
            continue
        for symbol in _imported_names(row["signature"] or ""):
            key = (row["file_path"], f"{module}:{symbol}")
            if key in emitted:
                continue
            emitted.add(key)
            yield {
                "id": ids.external_ref_id(ecosystem, module, symbol),
                "module": module,
                "root_module": ids.module_to_package(ecosystem, module),
                "symbol": symbol,
                "ecosystem": ecosystem,
                "file": ids.file_id(repo, row["file_path"]),
                "repo": repo,
                "line": row["start_line"],
                "extractor": EXTRACTOR,
            }


def _external_refs_go(conn: sqlite3.Connection, repo: str, ecosystem: str) -> Iterator[dict]:
    """Go cross-repo references, reconstructed from CodeGraph's own tables.

    In Go the symbol never appears in the import statement - `import
    "github.com/grafana/dskit/user"` names only the package, and the symbol
    shows up later as the selector `user.InjectOrgID`. CodeGraph has already
    done the hard half: `unresolved_refs.reference_name` holds the whole
    selector. Joining its head against the file's imports is mechanical.

    This is why the "on Go the artifact-only arm scores zero" result is specific
    to graphify, not a property of Go: CodeGraph's artifact does carry what the
    cross-repo pass needs.

    KNOWN LIMIT: the bound identifier is taken as the last path segment of the
    module, because CodeGraph's import node records the path but not an alias.
    Aliased imports (`foo "github.com/x/bar"`) are therefore missed. goscan
    handles those via go/ast, so the oracle sees them and they show up as recall
    loss rather than as wrong edges.
    """
    imports: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT qualified_name AS module, file_path FROM nodes WHERE kind = 'import'"
    ):
        module = row["module"]
        if not module:
            continue
        imports.setdefault(row["file_path"], {})[module.rsplit("/", 1)[-1]] = module

    emitted: set[tuple[str, str, str]] = set()
    for row in conn.execute(
        "SELECT reference_name, file_path, line FROM unresolved_refs "
        "WHERE reference_name LIKE '%.%'"
    ):
        parts = (row["reference_name"] or "").split(".")
        if len(parts) < 2:
            continue
        head, symbol = parts[0], parts[1]
        # Exported only - a lowercase selector target is package-private and
        # cannot be a cross-repo reference.
        if not symbol[:1].isupper():
            continue
        module = imports.get(row["file_path"], {}).get(head)
        if not module:
            continue                      # local variable, not a package
        key = (row["file_path"], module, symbol)
        if key in emitted:
            continue
        emitted.add(key)
        yield {
            "id": ids.external_ref_id(ecosystem, module, symbol),
            "module": module,
            "root_module": module,        # Go linker matches by path prefix
            "symbol": symbol,
            "ecosystem": ecosystem,
            "file": ids.file_id(repo, row["file_path"]),
            "repo": repo,
            "line": row["line"],
            "extractor": EXTRACTOR,
        }


# --- helpers -----------------------------------------------------------------

def _identity_name(qualified_name: str | None, name: str) -> str:
    """Canonical identity name. See the IDENTITY CONTRACT in the module docstring."""
    if not qualified_name:
        return name
    # CodeGraph separates owner from member with '::'.
    return qualified_name.replace("::", ".")


def _imported_names(signature: str) -> list[str]:
    """Imported symbols from an import statement.

    Handles `from m import a, b`, the parenthesised multi-line form, and
    `import m` (which binds a module, not a symbol, and yields nothing - the
    same choice the source-derived pass makes).
    """
    signature = signature.strip()
    if not signature.startswith("from "):
        return []
    match = _IMPORT_NAMES.search(signature)
    if not match:
        return []
    body = match.group("names").strip().strip("()")
    out: list[str] = []
    for part in body.split(","):
        token = part.strip().strip("()").strip()
        if not token or token == "*":
            continue
        # `x as y` binds y, but the publishing repo defines x.
        out.append(token.split(" as ")[0].strip())
    return [t for t in out if t.isidentifier()]


def _is_exported(name: str, ecosystem: str) -> bool:
    if not name:
        return False
    if ecosystem == "go":
        return name[0].isupper()
    return not name.startswith("_")
