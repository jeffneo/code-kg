"""Map GitNexus's exported graph onto the canonical schema.

Verified against gitnexus 1.6.9. The export is produced by
extractors/gitnexus/extract.sh, which runs GitNexus's own `cypher` command
against its embedded LadybugDB store - there is no portable artifact to read.

  nodes  {id, name, path, sl, el, exp?, label}
  files  {id, name, path}
  edges  {src, dst, type, conf}

Node ids are `Label:path:name`, and for methods the name part already carries
the owner (`Method:foo.py:GraphCypherQAChain.__init__`). GitNexus is the only
one of the three that gets the IDENTITY CONTRACT right without help -
codegraph needed `::` rewriting and graphify needed its `method` edges walked.

WHAT IT CONTRIBUTES, AND WHAT IT CANNOT
---------------------------------------
Contributes: a third independent vote on symbols and call edges, with a real
per-edge `confidence` value rather than a categorical tag.

Cannot contribute: cross-repo edges. Its IMPORTS relationships connect File to
File and only for imports that resolve *inside* the repo; an import of
`neo4j.Driver` produces no node and no edge at all. So like graphify, and unlike
codegraph, its artifact carries nothing for the cross-repo arm. external_refs()
returns nothing, deliberately, rather than inventing something.

Community / Process / Folder / Section nodes are excluded upstream in the
exporter: they are GitNexus-specific aggregates with no counterpart in the other
extractors, and importing them would inflate "gitnexus only" in the agreement
matrix for purely structural reasons.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .. import ids

EXTRACTOR = "gitnexus"

ARTIFACT = "graph.json"
EXTERNAL_REF_ANCHOR = "symbol"

# Structural relations only. DEFINES / CONTAINS / MEMBER_OF / HAS_METHOD /
# HAS_PROPERTY describe containment, and STEP_IN_PROCESS links into GitNexus's
# Process aggregates, which we do not import.
CALL_TYPES = {"CALLS", "EXTENDS", "METHOD_OVERRIDES", "IMPLEMENTS"}

# GitNexus's dynamic-dispatch guesses arrive with a lower confidence already, so
# its own value is used directly rather than being re-bucketed.
DEFAULT_CONFIDENCE = 0.8


def load(artifact_dir: Path) -> dict[str, Any]:
    return json.loads((artifact_dir / ARTIFACT).read_text())


def provenance(artifact_dir: Path) -> dict[str, Any]:
    path = artifact_dir / "provenance.json"
    return json.loads(path.read_text()) if path.exists() else {}


def files(doc: dict, repo: str) -> Iterator[dict]:
    seen: set[str] = set()
    for row in doc.get("files", []):
        path = row.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        yield {
            "id": ids.file_id(repo, path),
            "path": path,
            "repo": repo,
            "language": Path(path).suffix.lstrip(".") or "unknown",
            "extractor": EXTRACTOR,
        }


def symbols(doc: dict, repo: str, ecosystem: str) -> Iterator[dict]:
    for row in doc.get("nodes", []):
        path, identity = row.get("path"), _identity_name(row)
        if not path or not identity:
            continue
        yield {
            "id": ids.symbol_id(repo, path, identity),
            "name": row.get("name") or identity,
            "qname": identity,
            "kind": (row.get("label") or "symbol").lower(),
            "kind_inferred": False,     # gitnexus states the kind via node table
            "path": path,
            "repo": repo,
            "file": ids.file_id(repo, path),
            "start_line": _int(row.get("sl")),
            "end_line": _int(row.get("el")),
            # Its isExported is real where present, but absent on several node
            # tables. The shared convention keeps the agreement matrix about
            # extraction rather than about differing export rules.
            "exported": _is_exported(row.get("name") or identity, ecosystem),
            "docstring": None,
            "extractor": EXTRACTOR,
        }


def calls(doc: dict, repo: str, symbol_ids: set[str]) -> Iterator[dict]:
    index = _index(doc, repo)
    for edge in doc.get("edges", []):
        if edge.get("type") not in CALL_TYPES:
            continue
        src, dst = index.get(edge.get("src")), index.get(edge.get("dst"))
        if not src or not dst or src not in symbol_ids or dst not in symbol_ids:
            continue
        yield {
            "src": src,
            "dst": dst,
            "extractor": EXTRACTOR,
            "confidence": _float(edge.get("conf")) or DEFAULT_CONFIDENCE,
            "evidence": None,
        }


def external_refs(
    doc: dict,
    repo: str,
    ecosystem: str,
    symbol_ids: set[str],
    repo_root: Path | None = None,
) -> Iterator[dict]:
    """Nothing. See the module docstring.

    GitNexus's IMPORTS edges are File->File and internal-only; an external
    import leaves no trace in the index. Emitting nothing is the honest result,
    and it is what makes its artifact-only cross-repo arm score zero.
    """
    return iter(())


# --- helpers -----------------------------------------------------------------

def _identity_name(row: dict) -> str | None:
    """Recover the identity name from the node id.

    Ids are `Label:path:name`, and paths can themselves contain colons, so the
    name is taken as whatever follows the known `Label:path:` prefix rather than
    by splitting on ':'.

    The trailing `#N` disambiguator is stripped. GitNexus appends it to
    same-named symbols within a file (`GraphSchema._skip_required_migration#1`);
    neither other extractor does. Measured on corpus A: 5,547 symbols carried
    the suffix, every one of them matched nothing, and 5,391 matched an existing
    symbol once stripped - so left in, it manufactures ~5,400 phantom
    "gitnexus only" rows in the agreement matrix.

    Stripping merges genuine overloads into one node, which is the same
    trade-off ids.symbol_id already makes by excluding the signature.
    """
    node_id, label, path = row.get("id"), row.get("label"), row.get("path")
    name = row.get("name")
    if node_id and label and path:
        prefix = f"{label}:{path}:"
        if node_id.startswith(prefix):
            name = node_id[len(prefix):]
    return name.split("#")[0] if name else None


def _index(doc: dict, repo: str) -> dict[str, str]:
    """GitNexus node id -> canonical symbol id."""
    out: dict[str, str] = {}
    for row in doc.get("nodes", []):
        path, identity = row.get("path"), _identity_name(row)
        if path and identity:
            out[row["id"]] = ids.symbol_id(repo, path, identity)
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_exported(name: str, ecosystem: str) -> bool:
    if not name:
        return False
    if ecosystem == "go":
        return name[0].isupper()
    return not name.startswith("_")
