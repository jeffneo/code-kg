"""Map Graphify's graph.json onto the canonical schema.

Verified against graphify 0.9.43 output. The real shape:

  {"nodes": [...], "links": [...], "directed": false, "failed_sources": [...]}

  node: {id, label, file_type, source_file, source_location, _origin, _callable?}
  link: {source, target, relation, confidence, source_file, source_location,
         weight, _origin}

Three things about that shape drive everything below.

1. Nodes carry no `kind`. There is no function/class/method field - only
   `_callable: true` on callables and `label` conventions ("foo()" for
   callables, "bar.py" for files). Kind is therefore inferred, and inferred
   kind is recorded as such rather than presented as fact.

2. Identity is (source_file, label). Graphify's own `id` is a slug derived
   from the importing file's path and is not stable across repos, so it is
   used only to resolve edge endpoints within one artifact.

3. External imports appear as nodes with `source_file: ""`, labelled with the
   symbol but NOT the module. The module is recovered from source by
   importmap.py - see that module's header for why the extractor drops it.

Dropped on purpose:
  - file_type `rationale` (976 nodes here): LLM/comment-derived, nondeterministic,
    not comparable against other extractors.
  - file_type `concept`: same reasoning.
  - relation `rationale_for`, `semantically_similar_to`: not structural.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from .. import ids, importmap

EXTRACTOR = "graphify"

# Artifact this mapper reads, and where its external refs attach. graphify
# resolves imports from a symbol node; codegraph attaches them to the file.
ARTIFACT = "graph.json"
EXTERNAL_REF_ANCHOR = "symbol"

# Graphify's own confidence vocabulary, mapped onto one numeric scale so
# extractors that use different words for the same idea stay comparable.
CONFIDENCE = {"EXTRACTED": 1.0, "INFERRED": 0.7, "AMBIGUOUS": 0.4}

CALL_RELATIONS = {"calls", "method", "indirect_call"}
IMPORT_RELATIONS = {"imports", "imports_from", "uses", "references", "re_exports"}
INHERIT_RELATIONS = {"inherits", "extends"}

# `indirect_call` is graphify's dynamic-dispatch guess. Kept, but capped -
# it should never outrank a resolved static call in a blast-radius path.
RELATION_CEILING = {"indirect_call": 0.5}

_LOC = re.compile(r"L(\d+)")


def load(artifact_dir: Path) -> dict[str, Any]:
    return json.loads((artifact_dir / "graph.json").read_text())


def provenance(artifact_dir: Path) -> dict[str, Any]:
    path = artifact_dir / "provenance.json"
    return json.loads(path.read_text()) if path.exists() else {}


def files(doc: dict, repo: str) -> Iterator[dict]:
    seen: set[str] = set()
    for node in doc.get("nodes", []):
        if node.get("file_type") != "code":
            continue
        path = node.get("source_file")
        if not path or path in seen:
            continue
        seen.add(path)
        yield {
            "id": ids.file_id(repo, path),
            "path": path,
            "repo": repo,
            "language": _language(path),
            "extractor": EXTRACTOR,
        }


def symbols(doc: dict, repo: str, ecosystem: str) -> Iterator[dict]:
    kinds = _infer_kinds(doc)
    owners = _method_owners(doc)
    for node in doc.get("nodes", []):
        if node.get("file_type") != "code":
            continue
        path = node.get("source_file")
        label = node.get("label")
        if not path or not label:
            continue  # external node - handled by external_refs()

        name = label[:-2] if label.endswith("()") else label
        if name == Path(path).name:
            continue  # the file's own node; File covers it

        # Graphify labels a method `.close()` - leading dot, no owning class.
        # The owner is only recoverable from its `method` edges. Both mappers
        # must render identity the same way or the agreement matrix measures
        # formatting rather than extraction. See the IDENTITY CONTRACT in
        # mappers/codegraph.py.
        identity = _identity_name(node["id"], name, owners)

        yield {
            "id": ids.symbol_id(repo, path, identity),
            "name": name.lstrip("."),
            "qname": identity,
            "kind": kinds.get(node["id"], "function" if node.get("_callable") else "symbol"),
            "kind_inferred": True,
            "path": path,
            "repo": repo,
            "file": ids.file_id(repo, path),
            "start_line": _line(node.get("source_location")),
            "end_line": None,  # graphify records a single anchor, not a span
            "exported": _is_exported(name, ecosystem),
            "docstring": None,
            "extractor": EXTRACTOR,
        }


def file_imports(doc: dict, repo: str) -> Iterator[dict]:
    """File -> File imports that resolve inside this repo.

    A graphify file node is the one whose label is the basename of its own
    source_file; an import edge between two of those is a module dependency.
    """
    nodes = {n["id"]: n for n in doc.get("nodes", [])}

    def is_file(n):
        path = n.get("source_file") or ""
        return bool(path) and n.get("label") == path.rsplit("/", 1)[-1]

    seen: set[tuple[str, str]] = set()
    for edge in doc.get("links", []):
        if edge.get("relation") not in ("imports", "imports_from"):
            continue
        src, dst = nodes.get(edge.get("source")), nodes.get(edge.get("target"))
        if not src or not dst or not is_file(src) or not is_file(dst):
            continue
        if src["source_file"] == dst["source_file"]:
            continue                     # self-import is not a dependency
        key = (src["source_file"], dst["source_file"])
        if key in seen:
            continue
        seen.add(key)
        yield {
            "src": ids.file_id(repo, src["source_file"]),
            "dst": ids.file_id(repo, dst["source_file"]),
            "extractor": EXTRACTOR,
        }


def calls(doc: dict, repo: str, symbol_ids: set[str]) -> Iterator[dict]:
    """Intra-repo call edges.

    NOTE ON DIRECTION: graph.json sets `directed: false`, but the relation
    vocabulary is directional (`calls`, `inherits`) and source/target ordering
    encodes it. We treat source->target as the direction. Worth re-checking if
    blast-radius results ever look symmetric - that would be the cause.
    """
    index = _index(doc, repo)
    for edge in doc.get("links", []):
        relation = edge.get("relation")
        if relation not in CALL_RELATIONS and relation not in INHERIT_RELATIONS:
            continue
        src, dst = index.get(edge["source"]), index.get(edge["target"])
        if not src or not dst or src not in symbol_ids or dst not in symbol_ids:
            continue
        confidence = CONFIDENCE.get(edge.get("confidence", "EXTRACTED"), 0.7)
        yield {
            "src": src,
            "dst": dst,
            "extractor": EXTRACTOR,
            "confidence": min(confidence, RELATION_CEILING.get(relation, 1.0)),
            "evidence": f"{edge.get('source_file','')}:{edge.get('source_location','')}",
        }


def external_refs(
    doc: dict,
    repo: str,
    ecosystem: str,
    symbol_ids: set[str],
    repo_root: Path | None = None,
) -> Iterator[dict]:
    """Imports whose target is outside this repo - the dangling ends.

    Graphify gives us the symbol but not the module, so the module is recovered
    by reading the importing file's import statements. Refs whose module cannot
    be recovered are still emitted with module=None: they are honest unknowns,
    they show up in Q5, and dropping them would quietly inflate recall.
    """
    index = _index(doc, repo)
    nodes = {n["id"]: n for n in doc.get("nodes", [])}
    emitted: set[tuple[str, str]] = set()

    for edge in doc.get("links", []):
        if edge.get("relation") not in IMPORT_RELATIONS:
            continue

        src = index.get(edge["source"])
        if not src or src not in symbol_ids:
            continue

        target = nodes.get(edge["target"])
        if not target or target.get("source_file"):
            continue  # resolved inside this repo already
        if target.get("file_type") != "code":
            continue

        symbol = target.get("label")
        if not symbol:
            continue
        symbol = symbol[:-2] if symbol.endswith("()") else symbol

        importing_file = edge.get("source_file")
        module = None
        if repo_root and importing_file:
            module = importmap.lookup(repo_root, importing_file, symbol)

        # Relative imports resolve inside this repo - not cross-repo candidates.
        if module and module.startswith("."):
            continue

        key = (src, f"{module}:{symbol}")
        if key in emitted:
            continue
        emitted.add(key)

        yield {
            "id": ids.external_ref_id(ecosystem, module or "<unknown>", symbol),
            "module": module,
            "root_module": ids.module_to_package(ecosystem, module) if module else None,
            "symbol": symbol,
            "ecosystem": ecosystem,
            "src": src,
            "extractor": EXTRACTOR,
        }


# --- helpers -----------------------------------------------------------------

def _index(doc: dict, repo: str) -> dict[str, str]:
    """Graphify's internal node id -> our canonical symbol id.

    Only internal code nodes get an entry; external nodes deliberately do not,
    which is what lets calls() cheaply tell "resolved here" from "dangling".
    """
    owners = _method_owners(doc)
    out: dict[str, str] = {}
    for node in doc.get("nodes", []):
        path, label = node.get("source_file"), node.get("label")
        if node.get("file_type") != "code" or not path or not label:
            continue
        name = label[:-2] if label.endswith("()") else label
        out[node["id"]] = ids.symbol_id(repo, path, _identity_name(node["id"], name, owners))
    return out


def _method_owners(doc: dict) -> dict[str, str]:
    """method-node id -> owning class label, from `method` edges."""
    nodes = {n["id"]: n for n in doc.get("nodes", [])}
    owners: dict[str, str] = {}
    for edge in doc.get("links", []):
        if edge.get("relation") != "method":
            continue
        owner = nodes.get(edge.get("source"), {}).get("label")
        if owner:
            owners[edge["target"]] = owner.removesuffix("()").lstrip(".")
    return owners


def _identity_name(node_id: str, name: str, owners: dict[str, str]) -> str:
    """Canonical identity: `Owner.member` for methods, bare name otherwise."""
    bare = name.lstrip(".")
    owner = owners.get(node_id)
    return f"{owner}.{bare}" if owner else bare


def _infer_kinds(doc: dict) -> dict[str, str]:
    """Derive a kind per node from edge context, since nodes carry none.

    Targets of `inherits`/`extends` are classes; sources of `method` edges are
    classes and targets are methods; `_callable` nodes are functions. Anything
    left over stays the generic `symbol` rather than being guessed at.
    """
    kinds: dict[str, str] = {}
    for edge in doc.get("links", []):
        relation = edge.get("relation")
        if relation in INHERIT_RELATIONS:
            kinds[edge["target"]] = "class"
            kinds.setdefault(edge["source"], "class")
        elif relation == "method":
            kinds[edge["source"]] = "class"
            kinds[edge["target"]] = "method"
    return kinds


def _module_of(path: str) -> str:
    return re.sub(r"\.(py|go|ts|js|java|rb|rs)$", "", path).replace("/", ".")


def _line(location: Any) -> int | None:
    m = _LOC.match(str(location or ""))
    return int(m.group(1)) if m else None


def _language(path: str) -> str:
    return Path(path).suffix.lstrip(".") or "unknown"


def _is_exported(name: str, ecosystem: str) -> bool:
    """Only exported symbols are cross-repo resolution candidates.

    Go: capitalised identifiers are exported - exact.
    Python: the leading-underscore convention. Not language-enforced, so this
    will wrongly exclude a private symbol another repo imports anyway, which is
    a real pattern. Known recall gap; Q5 is where it shows up.
    """
    if not name:
        return False
    if ecosystem == "go":
        return name[0].isupper()
    return not name.startswith("_")
