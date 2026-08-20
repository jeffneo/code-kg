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


def repo_id(url: str, dist: str | None = None) -> str:
    """https://github.com/neo4j/langchain-neo4j -> repo:github.com/neo4j/langchain-neo4j

    `dist` names a separately published distribution living inside the repo, and
    is appended as a path segment:

        repo:github.com/apache/airflow/airflow-core
        repo:github.com/apache/airflow/task-sdk

    Needed because two distributions in one repository are two dependency
    endpoints - they version, publish and release independently - but they share
    a clone URL, so URL-derived ids collide and the two collapse into one node.

    A path segment rather than a new separator, deliberately: `split(repo,'/')[-1]`
    is already how queries derive a display label, and it keeps working.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/").removesuffix(".git")
    base = f"repo:{parsed.netloc}/{path}"
    return f"{base}/{dist}" if dist else base


def repo_id_for(spec: dict) -> str:
    """Canonical repo id for a corpus.yaml entry. Use this, not repo_id(url)."""
    return repo_id(spec["url"], spec.get("dist"))


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


def language_of(path: str) -> str:
    """File extension, lowercased. OUR rule, deliberately not the extractor's.

    The three extractors disagree: graphify and gitnexus derive it from the
    extension (`py`), CodeGraph reports its own detected language (`python`).
    Because MERGE_FILE assigns rather than coalesces, whichever extractor loaded
    last silently won - so `language` meant different things depending on load
    order, and every query filtering on it was unreliable.

    That bit: the cycle projections filter `language IN ['py','go']` to keep
    Markdown out, and after CodeGraph relabelled every Python file as `python`
    the projections matched nothing at all and the SCC step failed outright.
    A loud failure, but it could just as easily have silently dropped a repo.

    Same reasoning as `exported` - normalise centrally so a property means one
    thing regardless of which tool produced the row.
    """
    _, _, ext = _norm_path(path).rpartition(".")
    return ext.lower() if ext else "unknown"


def _norm_path(path: str) -> str:
    """Repo-relative, forward-slashed, no leading ./ - extractors vary."""
    path = path.replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path
