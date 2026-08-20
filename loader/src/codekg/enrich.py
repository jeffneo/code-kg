"""Source-derived external imports.

WHY THIS EXISTS
---------------
Measured on corpus A: of 103 symbols langchain-neo4j imports from sibling-repo
packages, 64 appear nowhere in graphify's graph.json, and only 6 appear in the
external-node form the mapper can consume. Graphify does not node-ify
module-level constants at all, and misses a number of classes.

So the cross-repo layer cannot be a pure ETL over the extractor's artifact.
Roughly 94% of the cross-repo import surface simply is not in there. This pass
enumerates imports directly from source and materialises the missing
:ExternalRef nodes.

WHAT THIS DOES TO THE SCORE - READ BEFORE QUOTING NUMBERS
----------------------------------------------------------
This pass and scoring/oracle.py both enumerate imports with Python's `ast`.
They are therefore NOT independent, and recall for the `unified+source` arm
approaches 1.0 by construction. That number is a CEILING DIAGNOSTIC - it says
what the cross-repo layer can reach when the import surface is complete. It is
not evidence that the pipeline is accurate.

The independently measurable number is the `unified` arm, which uses only what
the extractor's artifact supports. Quote that one when asked how well this
works, and quote this one when asked how much is reachable.

Granularity is the file, not the calling function. Which function inside a file
uses an imported name requires call resolution, which is the extractor's job.
Edges land on :File and the demo queries traverse through them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from . import ids, internal_imports
from .oracle import SKIP_DIRS, python_files


def external_refs_go(repo: str, scan: dict, ecosystem: str = "go") -> Iterator[dict]:
    """Go external references, from goscan's AST output.

    Unlike Python, the symbol is not in the import statement - it comes from a
    selector expression resolved against the file's import block. goscan does
    that resolution; this just shapes the rows.

    `root_module` carries the full package path because the Go linker matches by
    longest module-path prefix, not by a top-level name.
    """
    for ref in scan.get("refs", []):
        yield {
            "id": ids.external_ref_id(ecosystem, ref["module"], ref["symbol"]),
            "module": ref["module"],
            "root_module": ref["module"],
            "symbol": ref["symbol"],
            "ecosystem": ecosystem,
            "file": ids.file_id(repo, ref["file"]),
            "repo": repo,
            "line": ref.get("line"),
            "extractor": "source",
        }


def external_imports(repo: str, repo_root: Path, ecosystem: str) -> Iterator[dict]:
    """Every absolute import in a repo, as a File -> ExternalRef row.

    All imports are emitted, not just those that look corpus-relevant. Filtering
    to the corpus here would bias the graph toward finding what we hoped to
    find, and it would gut Q5's "genuine third-party" bucket, which is what
    makes the dangling-reference report readable.
    """
    if ecosystem != "python":
        return

    for path in python_files(repo_root):
        rel = str(path.relative_to(repo_root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue

        # Execution context, same classification as intra-repo imports. Without
        # it a cross-boundary cycle can be held together by an
        # `if TYPE_CHECKING:` import that never runs - which is precisely the
        # bug that made the intra-repo cycle finding wrong. The context rides
        # on IMPORTS_EXT and is carried onto the derived cross-repo edge.
        contexts = internal_imports.import_contexts(tree)
        # A module-level import inside `try: ... except ImportError:` executes,
        # so it is a real top-level edge - but the code tolerates its absence,
        # which is the difference between "must fix" and "already handled".
        guarded_of = internal_imports.guarded_nodes(tree)

        seen: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level or not node.module:
                continue  # relative - resolves inside this repo
            context = contexts.get(id(node), internal_imports.TOPLEVEL)
            guarded = id(node) in guarded_of
            root_module = node.module.split(".")[0]
            for alias in node.names:
                if alias.name == "*":
                    continue
                key = (node.module, alias.name)
                if key in seen:
                    continue
                seen.add(key)
                yield {
                    "id": ids.external_ref_id(ecosystem, node.module, alias.name),
                    "module": node.module,
                    "root_module": ids.normalize_package_name(ecosystem, root_module),
                    "symbol": alias.name,
                    "ecosystem": ecosystem,
                    "file": ids.file_id(repo, rel),
                    "repo": repo,
                    "line": node.lineno,
                    "context": context,
                    "guarded": guarded,
                    "extractor": "source",
                }
