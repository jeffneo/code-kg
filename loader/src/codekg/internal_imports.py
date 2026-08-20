"""Source-derived intra-repo module imports, classified by execution context.

WHY THIS EXISTS
---------------
None of the three extractors record *where* in a file an import appears, and
that single missing field makes every cycle finding unreliable. Three very
different things arrive as one undifferentiated `IMPORTS` edge:

  toplevel   module level. Executed on import. A cycle here is a real
             import-time cycle, surviving only because Python registers a module
             in sys.modules before it finishes executing.

  typing     guarded by `if TYPE_CHECKING:`. NEVER executed. A cycle here is
             real architectural coupling and is already broken at runtime.

  deferred   inside a function or method body. Executed on call, not on import.
             Almost always a deliberate cycle-break, which makes it *evidence*
             that a design cycle exists and the maintainers knew.

How much this matters, measured on the neo4j-python-driver: reported as one
component of 102 modules with an 11-hop cycle. Classify the edges and the
top-level, facade-free answer is ZERO cycles - two of the three edges in the
component we were quoting are `if TYPE_CHECKING:` and never run.

So this is not a refinement. Without it the cycle exhibit is wrong.

WHY SOURCE AND NOT THE ARTIFACT
-------------------------------
The context is not in any artifact to be recovered, and the AST gives it
exactly. The same parse also resolves the import to a *file* rather than a
module string, which is what a File->File edge needs. Independence is preserved
where it counts: the cycle oracle is pylint, a different tool with a different
algorithm - see cyclescore.py.

GO IS DIFFERENT ON PURPOSE
--------------------------
Go has no conditional and no function-scoped imports; the language puts every
import in one block at the top of the file. So every Go import edge is
`toplevel` by definition and is labelled at load time rather than here. Go also
forbids circular *package* imports at compile time, which is why corpus B
correctly finds no cycles - that zero is a check on the method, not a
disappointing result.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from . import ids
from .oracle import python_files

TOPLEVEL = "toplevel"
TYPING = "typing"
DEFERRED = "deferred"


def module_index(repo_root: Path) -> dict[str, str]:
    """Importable dotted module name -> repo-relative path, for one repo.

    A package's `__init__.py` is indexed under the package's own name
    (`sqlalchemy.orm`), not `sqlalchemy.orm.__init__`, because that is the name
    an import statement actually uses.

    The package root is found by walking up while directories contain
    `__init__.py`, which handles src-layouts (`src/neo4j/...` -> `neo4j...`)
    without needing to be told about them.
    """
    index: dict[str, str] = {}
    for path in python_files(repo_root):
        parts: list[str] = []
        parent = path.parent
        while (parent / "__init__.py").is_file() and parent != repo_root:
            parts.append(parent.name)
            parent = parent.parent
        parts.reverse()

        if path.name == "__init__.py":
            dotted = ".".join(parts)
        else:
            dotted = ".".join(parts + [path.stem])

        if dotted:
            # First writer wins. A duplicate dotted name means two files claim
            # one module (a stub tree, a vendored copy); neither is more
            # correct, and guessing would silently move edges.
            index.setdefault(dotted, str(path.relative_to(repo_root)))
    return index


def _contexts(tree: ast.Module) -> dict[int, str]:
    """id(import node) -> execution context.

    Walks with an inherited context rather than using ast.walk, because the
    answer depends entirely on ancestry. Nesting rule: once deferred, always
    deferred - a TYPE_CHECKING block inside a function body still only runs if
    the function is called, and it never type-checks at runtime either way.
    """
    out: dict[int, str] = {}

    def descend(node: ast.AST, ctx: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_ctx = ctx
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                child_ctx = DEFERRED
            elif isinstance(child, ast.If) and ctx == TOPLEVEL and _type_checking(child.test):
                child_ctx = TYPING
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                out[id(child)] = ctx
            descend(child, child_ctx)

    descend(tree, TOPLEVEL)
    return out


def _type_checking(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:`, and the `not` inverse.

    The inverse matters: `if not TYPE_CHECKING:` guards code that runs only at
    runtime, so imports inside it are toplevel, not typing. Treating the two the
    same would misclassify in the direction that overstates cycles.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return False
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    if isinstance(test, ast.BoolOp):
        return any(_type_checking(v) for v in test.values)
    return False


def _package_of(dotted: str, is_package: bool) -> str:
    """The `__package__` a relative import resolves against."""
    if is_package:
        return dotted
    return dotted.rsplit(".", 1)[0] if "." in dotted else ""


def _targets(node: ast.Import | ast.ImportFrom, dotted: str, is_package: bool) -> list[str]:
    """Candidate dotted module names this statement could pull in."""
    if isinstance(node, ast.Import):
        # Only the module the statement actually names.
        #
        # `import a.b.c` does also execute a/__init__.py and a/b/__init__.py, so
        # synthesizing ancestor edges would be closer to runtime semantics. It
        # was tried and is a bad trade: every module ends up depending on the
        # root package, the root package imports most of the tree, and the
        # facade-inclusive component becomes ~the entire codebase. Measured on
        # SQLAlchemy, precision against pylint fell to 0.43 with 159 modules
        # claimed against pylint's 77 - technically defensible, useless to act
        # on, and not comparable to any other tool.
        #
        # So: model what the developer wrote. The omission is real and worth
        # knowing - a cycle that exists ONLY through package-ancestor execution
        # will not be reported here.
        return [alias.name for alias in node.names]

    if node.level:
        base = _package_of(dotted, is_package)
        if node.level > 1:
            bits = base.split(".")
            drop = node.level - 1
            base = ".".join(bits[:-drop]) if drop < len(bits) else ""
        head = f"{base}.{node.module}" if node.module and base else (node.module or base)
    else:
        head = node.module or ""

    if not head:
        return []
    # `from pkg import x` may bind a submodule or an attribute. Emit both; the
    # caller keeps only names that resolve to a real file, so an attribute
    # simply drops out.
    return [head] + [f"{head}.{a.name}" for a in node.names if a.name != "*"]


def classified_imports(repo: str, repo_root: Path, ecosystem: str) -> Iterator[dict]:
    """File -> File import rows within one repo, each carrying its context."""
    if ecosystem != "python":
        return

    index = module_index(repo_root)
    packages = {m for m, p in index.items() if p.endswith("__init__.py")}

    for path in python_files(repo_root):
        rel = str(path.relative_to(repo_root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue

        dotted = next((m for m, p in index.items() if p == rel), None)
        if dotted is None:
            continue
        is_package = dotted in packages

        ctx_of = _contexts(tree)
        # Strongest context wins per edge: a module imported both at top level
        # and inside a function is really a top-level dependency.
        best: dict[tuple[str, str], tuple[str, int]] = {}
        rank = {TOPLEVEL: 3, TYPING: 2, DEFERRED: 1}

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            ctx = ctx_of.get(id(node), TOPLEVEL)
            for target in _targets(node, dotted, is_package):
                dst = index.get(target)
                if dst is None or dst == rel:
                    continue
                key = (rel, dst)
                if key not in best or rank[ctx] > rank[best[key][0]]:
                    best[key] = (ctx, node.lineno)

        for (src, dst), (ctx, line) in best.items():
            yield {
                "src": ids.file_id(repo, src),
                "dst": ids.file_id(repo, dst),
                "context": ctx,
                "line": line,
                "extractor": "source",
            }
