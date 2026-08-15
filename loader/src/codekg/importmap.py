"""Recover the module behind each imported symbol, per file.

WHY THIS EXISTS
---------------
Graphify records an external import as a node labelled with the *symbol*
(`Driver`) and an empty `source_file`. The module it came from (`neo4j`) is not
kept anywhere in graph.json.

That is not a bug. Inside a single repo the module name is redundant - the
import either resolved to a local file or it didn't, and either way you have
your answer. The field only acquires value when a sibling repo is in scope and
you need to know *which* repo the symbol belongs to.

It is also a clean illustration of the structural claim this harness is
testing: the single-repo extractor discards precisely the field cross-repo work
needs, because in single-repo scope that field is worthless.

Recovering it is a ~40-line supplemental parse over source we already have on
disk, not a reimplementation of the extractor. We use the stdlib `ast` module,
so the result is exact rather than heuristic for Python.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path


def build(repo_root: Path, rel_path: str) -> dict[str, str]:
    """symbol -> module, for one file.

    `from neo4j import Driver, GraphDatabase`  ->  {Driver: neo4j, GraphDatabase: neo4j}
    `from .types import Chunk`                 ->  {Chunk: .types}   (relative, stays local)
    `import neo4j.graph as g`                  ->  {g: neo4j.graph}
    """
    path = repo_root / rel_path
    if path.suffix != ".py" or not path.is_file():
        return _build_go(path) if path.suffix == ".go" else {}

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return {}

    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # node.level > 0 means a relative import - resolves inside this repo,
            # so it is not a cross-repo candidate and we record it as-is.
            module = ("." * node.level) + (node.module or "")
            for alias in node.names:
                mapping[alias.asname or alias.name] = module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` binds the name `a` unless aliased.
                bound = alias.asname or alias.name.split(".")[0]
                mapping[bound] = alias.name
    return mapping


_GO_IMPORT = re.compile(r'^\s*(?:(?P<alias>[\w.]+)\s+)?"(?P<path>[^"]+)"')


def _build_go(path: Path) -> dict[str, str]:
    """Go import block -> {package-identifier: full module path}.

    The bound identifier is the alias when present, otherwise the last path
    component. That is right the overwhelming majority of the time; it is wrong
    when a package's declared name differs from its directory, which needs the
    package clause of the imported file to detect and is not worth it here.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    mapping: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            break
        if not in_block and not stripped.startswith("import "):
            if stripped.startswith(("func ", "type ", "var ", "const ")):
                break  # past the import section
            continue
        m = _GO_IMPORT.search(stripped)
        if m:
            module = m.group("path")
            mapping[m.group("alias") or module.split("/")[-1]] = module
    return mapping


@lru_cache(maxsize=8192)
def _cached(repo_root: str, rel_path: str) -> tuple[tuple[str, str], ...]:
    return tuple(build(Path(repo_root), rel_path).items())


def lookup(repo_root: Path, rel_path: str, symbol: str) -> str | None:
    """The module that `symbol` was imported from in `rel_path`, if any."""
    return dict(_cached(str(repo_root), rel_path)).get(symbol)
