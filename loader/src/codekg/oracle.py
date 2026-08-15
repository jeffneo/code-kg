"""Independent ground truth for cross-repo edges.

THE FAIRNESS CONSTRAINT
-----------------------
This module must never read extractor output. It walks the checked-out source
with Python's own `ast` and answers, from first principles:

  "Does file F in repo A import symbol S from a package published by repo B,
   and is S actually defined in repo B?"

If truth were instead derived from the :ExternalRef nodes graphify produced,
recall would be 1.0 by construction and the score would measure nothing. The
whole point is that the oracle can see imports the extractor missed.

WHAT IT SHARES WITH THE LINKER, AND WHY THAT IS ACCEPTABLE
----------------------------------------------------------
The oracle determines independently (a) which imports exist, by parsing source,
and (b) where symbols are defined, by parsing source. It shares with the linker
only the *notion* of matching an imported name to a same-named definition in the
publishing repo.

So this scores the pipeline's ability to surface and bind a cross-repo import.
It does not adjudicate whether name-matching is the right rule - if both the
oracle and the linker are wrong about `Session` meaning the same thing in two
repos, both are wrong together. That limit is real and is reported alongside
the score rather than buried.

GRANULARITY
-----------
Truth is keyed at (importing_file, target_repo, symbol), not
(calling_function, target_symbol). Determining which *function* inside a file
uses an imported name requires redoing call resolution, which is the extractor's
job and not something an oracle can do independently. Predictions are collapsed
to the same granularity before comparison, so both sides are measured on equal
terms.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ids

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "__pycache__", ".tox", ".mypy_cache", "testdata",
}


@dataclass(frozen=True)
class TruthEdge:
    """One expected cross-repo dependency, at file->symbol granularity."""
    src_repo: str
    src_file: str
    dst_repo: str
    symbol: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.src_repo, self.src_file, self.dst_repo, self.symbol)


def python_files(root: Path):
    for path in root.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        yield path


def module_level_names(root: Path) -> dict[str, str]:
    """Every importable top-level name in a repo -> the file defining it.

    Module-level defs, classes, and assignments. Anything a sibling repo could
    legally `from pkg import X`. Underscore-prefixed names are excluded on the
    same convention the linker uses.
    """
    found: dict[str, str] = {}
    for path in python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        rel = str(path.relative_to(root))
        for node in tree.body:  # top level only - not ast.walk
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            elif isinstance(node, ast.ImportFrom):
                # Re-exports through __init__.py are how most packages expose
                # their surface. `from .graph import Node` in neo4j/__init__.py
                # is what makes `from neo4j import Node` work at all.
                names = [a.asname or a.name for a in node.names if a.name != "*"]
            for name in names:
                if name and not name.startswith("_"):
                    found.setdefault(name, rel)
    return found


def imports_of(path: Path, root: Path) -> list[tuple[str, str]]:
    """[(root_module, bound_symbol)] for absolute imports in one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return []

    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:      # relative import - resolves inside this repo
                continue
            if not node.module:
                continue
            root_mod = node.module.split(".")[0]
            for alias in node.names:
                if alias.name != "*":
                    out.append((root_mod, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import neo4j` binds the module, not a symbol. No symbol-level
                # truth edge is claimed - attribute access would have to be
                # traced to know what is actually used, which is resolution.
                continue
    return out


def load_goscan(artifacts_root: Path, corpus: str, repo_dir: str) -> dict:
    """Read one repo's goscan output. See extractors/goscan/main.go."""
    path = artifacts_root / "goscan" / corpus / f"{repo_dir}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no goscan output at {path} - run `make goscan CORPUS={corpus}` first"
        )
    return json.loads(path.read_text())


def build_go(repos: list[dict], artifacts_root: Path, corpus: str) -> tuple[set, dict]:
    """Go ground truth, from goscan's AST output.

    Go's advantage over Python is that import paths are fully qualified and name
    the publishing repo, so matching a reference to its publisher is a
    longest-prefix match on the module path rather than a guess. The answer key
    is exact by construction.
    """
    publisher: dict[str, str] = {}
    for spec in repos:
        rid = ids.repo_id(spec["url"])
        for module in (spec.get("publishes") or []):
            publisher[module] = rid

    # Repos in a higher tier (corpus B's `full`) may not be fetched. Scoring
    # against repos that were never extracted would report their entire import
    # surface as missed, which is a configuration artefact rather than a result.
    scans: dict[str, dict] = {}
    skipped: list[str] = []
    for spec in repos:
        try:
            scans[ids.repo_id(spec["url"])] = load_goscan(artifacts_root, corpus, spec["id"])
        except FileNotFoundError:
            skipped.append(spec["id"])

    # Defs are indexed by (package directory, name), not name alone. A Go import
    # path names the exact package, and treating `ring.Config` as satisfied by
    # any Config anywhere in the repo would make the answer key agree with an
    # over-matching linker instead of catching it.
    surface: dict[str, set[tuple[str, str]]] = {}
    for rid, scan in scans.items():
        surface[rid] = {
            ("" if "/" not in d["file"] else str(PurePosixPath(d["file"]).parent), d["name"])
            for d in scan["defs"]
        }
    publisher = {m: r for m, r in publisher.items() if r in scans}

    def owner(module: str) -> str | None:
        best, best_len = None, -1
        for prefix, rid in publisher.items():
            if module == prefix or module.startswith(prefix + "/"):
                if len(prefix) > best_len:
                    best, best_len = rid, len(prefix)
        return best

    truth: set[tuple[str, str, str, str]] = set()
    unmatched: list[tuple[str, str]] = []

    def pkgdir_of(module: str, dst: str) -> str:
        """Package directory inside the publishing repo, from the import path."""
        prefix = max((p for p, r in publisher.items() if r == dst
                      and (module == p or module.startswith(p + "/"))),
                     key=len, default=None)
        if prefix is None:
            return ""
        return module[len(prefix) + 1:] if module != prefix else ""

    for rid, scan in scans.items():
        for ref in scan["refs"]:
            dst = owner(ref["module"])
            if not dst or dst == rid:
                continue
            if (pkgdir_of(ref["module"], dst), ref["symbol"]) in surface.get(dst, set()):
                truth.add(TruthEdge(rid, ref["file"], dst, ref["symbol"]).key())
            else:
                # Referenced from a corpus module but not found among its
                # exported top-level declarations. Usually a method on a type
                # rather than a package-level symbol.
                unmatched.append((ref["module"], ref["symbol"]))

    return truth, {
        "repos": len(scans),
        "skipped_repos": skipped,
        "publishers": publisher,
        "surface_sizes": {r: len(s) for r, s in surface.items()},
        "unmatched_imports": len(unmatched),
        "unmatched_examples": sorted(set(unmatched))[:15],
    }


def build(
    repos: list[dict],
    corpus_root: Path,
    ecosystem: str,
    artifacts_root: Path | None = None,
    corpus: str | None = None,
) -> tuple[set, dict]:
    """Compute the expected cross-repo edge set for a corpus.

    Returns (truth_edges, diagnostics).
    """
    if ecosystem == "go":
        if artifacts_root is None or corpus is None:
            raise ValueError("go oracle needs artifacts_root and corpus")
        return build_go(repos, artifacts_root, corpus)
    if ecosystem != "python":
        raise NotImplementedError(f"oracle supports python and go; got {ecosystem!r}")

    # package name -> publishing repo id
    publisher: dict[str, str] = {}
    roots: dict[str, Path] = {}
    for spec in repos:
        rid = ids.repo_id(spec["url"])
        root = corpus_root / spec["id"]
        roots[rid] = root / spec["subpath"] if spec.get("subpath") else root
        for pkg in (spec.get("publishes") or []):
            publisher[ids.normalize_package_name("python", pkg)] = rid

    # publishing repo id -> {importable name: defining file}
    surface: dict[str, dict[str, str]] = {
        rid: module_level_names(root) for rid, root in roots.items()
    }

    truth: set[tuple[str, str, str, str]] = set()
    unmatched: list[tuple[str, str, str]] = []   # import found, symbol not defined there

    for spec in repos:
        src_repo = ids.repo_id(spec["url"])
        root = roots[src_repo]
        for path in python_files(root):
            rel = str(path.relative_to(root))
            for root_mod, symbol in imports_of(path, root):
                dst_repo = publisher.get(ids.normalize_package_name("python", root_mod))
                if not dst_repo or dst_repo == src_repo:
                    continue  # third-party, stdlib, or self-import
                if symbol in surface.get(dst_repo, {}):
                    truth.add(TruthEdge(src_repo, rel, dst_repo, symbol).key())
                else:
                    # The import names a corpus package but the symbol is not
                    # discoverable in it. Usually a submodule import
                    # (`from neo4j.exceptions import X` where the name lives
                    # deeper) - reported, not silently dropped.
                    unmatched.append((src_repo, root_mod, symbol))

    diagnostics = {
        "repos": len(repos),
        "publishers": publisher,
        "surface_sizes": {r: len(s) for r, s in surface.items()},
        "unmatched_imports": len(unmatched),
        "unmatched_examples": sorted({(m, s) for _, m, s in unmatched})[:15],
    }
    return truth, diagnostics
