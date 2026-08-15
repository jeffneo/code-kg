"""Stable node identity.

This is the load-bearing piece of the whole harness. Every node needs an ID
that is deterministic across extractors and across runs, or you cannot MERGE
idempotently and the agreement matrix becomes noise.

IDs are readable rather than hashed on purpose - when a cross-repo edge looks
wrong you want to see why in the query result, not decode a sha1.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


def repo_id(url: str) -> str:
    """https://github.com/neo4j/langchain-neo4j -> repo:github.com/neo4j/langchain-neo4j"""
    parsed = urlparse(url)
    path = parsed.path.strip("/").removesuffix(".git")
    return f"repo:{parsed.netloc}/{path}"


def file_id(repo: str, path: str) -> str:
    return f"file:{repo}:{_norm_path(path)}"


def symbol_id(repo: str, path: str, qname: str) -> str:
    """Symbol identity is (repo, file, qualified name).

    Signature is deliberately excluded. Extractors disagree on how they render
    signatures - defaults, type annotations, decorators - and including it
    would make the same function look like different nodes to different tools,
    which is exactly what triangulation needs to avoid. The cost is that true
    overloads collapse into one node; acceptable for Python and Go, revisit if
    Java or C++ enters the corpus.
    """
    return f"sym:{repo}:{_norm_path(path)}#{qname}"


def package_id(ecosystem: str, name: str) -> str:
    return f"pkg:{ecosystem}:{normalize_package_name(ecosystem, name)}"


def external_ref_id(ecosystem: str, module: str, symbol: str | None) -> str:
    """An import whose target is not in the repo being extracted.

    These are the dangling ends. Cross-repo resolution is the act of binding
    them to real symbols in a sibling repo, and until that pass runs they are
    the honest representation of what a single-repo extractor can know.
    """
    suffix = f"#{symbol}" if symbol else ""
    return f"ext:{ecosystem}:{module}{suffix}"


def normalize_package_name(ecosystem: str, name: str) -> str:
    """Canonical package name for join purposes.

    Python treats `-` and `_` as equivalent and is case-insensitive (PEP 503),
    so `Langchain_Neo4j` and `langchain-neo4j` must land on the same node.
    Go module paths are case-sensitive and must not be touched.
    """
    name = name.strip()
    if ecosystem == "python":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def module_to_package(ecosystem: str, module: str) -> str | None:
    """Map an imported module path to the package that would provide it.

    Python: `neo4j.graph` -> `neo4j` (top-level module; the distribution name
    usually but not always matches, which is why corpus.yaml declares
    `publishes` explicitly rather than inferring it).

    Go: import paths are fully qualified and already name the publishing repo,
    so the mapping is identity and the match is done by longest-prefix against
    known module paths in link_cross_repo.
    """
    module = module.strip()
    if not module:
        return None
    if ecosystem == "python":
        return normalize_package_name("python", module.split(".")[0])
    return module


def _norm_path(path: str) -> str:
    """Repo-relative, forward-slashed, no leading ./ - extractors vary."""
    path = path.replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path
