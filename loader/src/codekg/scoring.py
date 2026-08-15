"""Score the pipeline's cross-repo edges against the independent oracle.

Arms compared:

  raw            No graph at all. Structurally cannot answer a cross-repo
                 question; included so the table has an honest floor.
  single-tool    Each extractor artifact on its own. The count here is
                 *verified* from the artifacts rather than assumed - see
                 single_tool_cross_repo_edges().
  manifest-only  Repo-level DEPENDS_ON_REPO edges only, no symbol lifting.
                 Perfect at repo granularity, useless at symbol granularity.
                 This is what an SCA tool gives you, and it is the baseline
                 the symbol-level work has to beat to be worth anything.
  unified        The joined graph after the cross-repo pass.

Precision and recall are reported separately and never averaged into a single
headline number. They fail differently: low recall means the pipeline missed
real dependencies, low precision means it invented some. A prospect cares which.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Score:
    arm: str
    predicted: set = field(default_factory=set)
    truth: set = field(default_factory=set)

    @property
    def true_positives(self) -> set:
        return self.predicted & self.truth

    @property
    def false_positives(self) -> set:
        return self.predicted - self.truth

    @property
    def false_negatives(self) -> set:
        return self.truth - self.predicted

    @property
    def precision(self) -> float | None:
        if not self.predicted:
            return None  # undefined, not zero - the arm made no claims
        return len(self.true_positives) / len(self.predicted)

    @property
    def recall(self) -> float | None:
        if not self.truth:
            return None
        return len(self.true_positives) / len(self.truth)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def row(self) -> dict:
        return {
            "arm": self.arm,
            "predicted": len(self.predicted),
            "truth": len(self.truth),
            "tp": len(self.true_positives),
            "fp": len(self.false_positives),
            "fn": len(self.false_negatives),
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


# Both corpora share one database, so every prediction query is scoped to the
# repos of the corpus being scored. Without this, corpus A's edges count as
# false positives when scoring corpus B.
PREDICTED_CROSS_REPO = """
MATCH (caller:Symbol)-[x:CALLS_CROSS_REPO]->(callee:Symbol)
WHERE caller.repo IN $repos AND callee.repo IN $repos
RETURN DISTINCT caller.repo AS src_repo,
                caller.path AS src_file,
                callee.repo AS dst_repo,
                callee.name AS symbol
"""

PREDICTED_REPO_LEVEL = """
MATCH (a:Repo)-[:DEPENDS_ON_REPO]->(b:Repo)
WHERE a.id IN $repos AND b.id IN $repos
RETURN DISTINCT a.id AS src_repo, b.id AS dst_repo
"""


# File-level cross-repo imports, created from source rather than from the
# extractor's artifact. Scored as a SEPARATE arm - see enrich.py for why mixing
# it into the headline number would be dishonest.
PREDICTED_SOURCE_IMPORTS = """
MATCH (f:File)-[:IMPORTS_CROSS_REPO]->(callee:Symbol)
WHERE f.repo IN $repos AND callee.repo IN $repos
RETURN DISTINCT f.repo   AS src_repo,
                f.path   AS src_file,
                callee.repo AS dst_repo,
                callee.name AS symbol
"""


# Artifact-only edges attributable to ONE extractor. graphify anchors its refs
# on a Symbol (-> CALLS_CROSS_REPO), codegraph on a File (-> IMPORTS_CROSS_REPO),
# so an extractor's contribution spans both edge types and has to be filtered by
# the provenance recorded on the originating relationship.
PREDICTED_BY_EXTRACTOR = """
MATCH (caller:Symbol)-[u:USES]->(:ExternalRef)-[:RESOLVES_TO]->(callee:Symbol)
WHERE caller.repo IN $repos AND callee.repo IN $repos
  AND $extractor IN u.extractors AND caller.repo <> callee.repo
RETURN DISTINCT caller.repo AS src_repo, caller.path AS src_file,
                callee.repo AS dst_repo, callee.name AS symbol
UNION
MATCH (f:File)-[i:IMPORTS_EXT]->(:ExternalRef)-[:RESOLVES_TO]->(callee:Symbol)
WHERE f.repo IN $repos AND callee.repo IN $repos
  AND $extractor IN i.extractors AND f.repo <> callee.repo
RETURN DISTINCT f.repo AS src_repo, f.path AS src_file,
                callee.repo AS dst_repo, callee.name AS symbol
"""


def predicted_edges_for(store, repos: list[str], extractor: str) -> set:
    """Cross-repo edges one extractor's artifact supports, on its own."""
    return {
        (r["src_repo"], r["src_file"], r["dst_repo"], r["symbol"])
        for r in store.query(PREDICTED_BY_EXTRACTOR, repos=repos, extractor=extractor)
    }


def predicted_edges(store, repos: list[str]) -> set:
    """Edges the extractor's artifact supports. The independent measurement."""
    return {
        (r["src_repo"], r["src_file"], r["dst_repo"], r["symbol"])
        for r in store.query(PREDICTED_CROSS_REPO, repos=repos)
    }


def predicted_edges_with_source(store, repos: list[str]) -> set:
    """Artifact-derived plus source-derived. A ceiling, not an accuracy claim."""
    extra = {
        (r["src_repo"], r["src_file"], r["dst_repo"], r["symbol"])
        for r in store.query(PREDICTED_SOURCE_IMPORTS, repos=repos)
    }
    return predicted_edges(store, repos) | extra


def predicted_repo_edges(store, repos: list[str]) -> set:
    return {(r["src_repo"], r["dst_repo"]) for r in store.query(PREDICTED_REPO_LEVEL, repos=repos)}


def merged_graph_edges(
    merged_path: Path, artifacts_root: Path, extractor: str, corpus: str
) -> tuple[set, dict]:
    """Cross-repo edges from `graphify merge-graphs`, as a scored baseline.

    This is the honest head-to-head. Graphify's merge unions node sets, tags
    each node with a repo, and lets its resolver connect across the result. The
    question is not whether it can draw an edge across a repo boundary - it can -
    but whether those edges are grounded in a declared dependency or are two
    symbols that happen to share a name.

    FAIRNESS: the merge assigns opaque tags (`repo`, `repo-2`, ...) because our
    checkout directory names collided. Rather than penalise it for that, we
    recover the real mapping by matching each tag's file set against the
    per-repo artifacts. The baseline is therefore given *perfect* repo
    attribution, and is judged only on the edges themselves.
    """
    doc = json.loads(merged_path.read_text())
    nodes = {n["id"]: n for n in doc.get("nodes", [])}

    # tag -> set of source files, and the same per real repo from the artifacts.
    tag_files: dict[str, set[str]] = {}
    for n in doc.get("nodes", []):
        f = n.get("source_file")
        if f:
            tag_files.setdefault(n.get("repo", "?"), set()).add(f)

    repo_files: dict[str, set[str]] = {}
    base = artifacts_root / extractor / corpus
    for repo_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        graph = repo_dir / "graph.json"
        if not graph.is_file():
            continue
        sub = json.loads(graph.read_text())
        repo_files[repo_dir.name] = {
            n["source_file"] for n in sub.get("nodes", []) if n.get("source_file")
        }

    # Best-Jaccard match, so tag->repo does not depend on merge argument order.
    tag_to_repo: dict[str, str] = {}
    for tag, files in tag_files.items():
        best, best_score = None, 0.0
        for name, rfiles in repo_files.items():
            if not rfiles:
                continue
            score = len(files & rfiles) / len(files | rfiles)
            if score > best_score:
                best, best_score = name, score
        tag_to_repo[tag] = best

    edges: set[tuple[str, str, str, str]] = set()
    cross_by_relation: dict[str, int] = {}
    for e in doc.get("links", []):
        src, dst = nodes.get(e.get("source")), nodes.get(e.get("target"))
        if not src or not dst:
            continue
        src_tag, dst_tag = src.get("repo"), dst.get("repo")
        if src_tag == dst_tag or not src_tag or not dst_tag:
            continue
        src_file = src.get("source_file") or e.get("source_file")
        symbol = (dst.get("label") or "").removesuffix("()")
        if not src_file or not symbol:
            continue
        rel = e.get("relation", "?")
        cross_by_relation[rel] = cross_by_relation.get(rel, 0) + 1
        edges.add((tag_to_repo.get(src_tag), src_file, tag_to_repo.get(dst_tag), symbol))

    return edges, {
        "tag_to_repo": tag_to_repo,
        "cross_repo_edges_raw": sum(cross_by_relation.values()),
        "by_relation": dict(sorted(cross_by_relation.items(), key=lambda x: -x[1])),
    }


def single_tool_cross_repo_edges(artifacts_root: Path, extractor: str, corpus: str) -> int:
    """Count cross-repo edges present in the raw per-repo artifacts.

    Verified, not assumed. An artifact can only contain a cross-repo edge if it
    contains a node belonging to another repo, so this counts nodes whose
    source_file resolves outside the repo the artifact was built from. The
    expected answer is zero, and demonstrating that beats asserting it.
    """
    total = 0
    base = artifacts_root / extractor / corpus
    if not base.is_dir():
        return 0
    for repo_dir in sorted(base.iterdir()):
        graph = repo_dir / "graph.json"
        if not graph.is_file():
            continue
        doc = json.loads(graph.read_text())
        nodes = {n["id"]: n for n in doc.get("nodes", [])}
        for edge in doc.get("links", []):
            src = nodes.get(edge.get("source"), {})
            dst = nodes.get(edge.get("target"), {})
            # Both endpoints anchored in files, in different repos, would be a
            # cross-repo edge. Within one artifact every anchored node is from
            # the same repo by construction, so this should never fire.
            if src.get("source_file") and dst.get("source_file"):
                if _repo_of(src) != _repo_of(dst):
                    total += 1
    return total


def _repo_of(node: dict) -> str:
    return node.get("_repo", "")


def repo_level_from_symbol_edges(edges: set) -> set:
    """Collapse symbol-level edges to repo pairs, for the granularity comparison."""
    return {(src_repo, dst_repo) for src_repo, _, dst_repo, _ in edges}


def format_table(scores: list[Score]) -> str:
    head = f"{'arm':<16}{'pred':>7}{'truth':>7}{'TP':>6}{'FP':>6}{'FN':>6}{'precision':>11}{'recall':>9}{'F1':>8}"
    lines = [head, "-" * len(head)]
    for s in scores:
        r = s.row()
        fmt = lambda v: "     n/a" if v is None else f"{v:8.3f}"
        lines.append(
            f"{r['arm']:<16}{r['predicted']:>7}{r['truth']:>7}{r['tp']:>6}"
            f"{r['fp']:>6}{r['fn']:>6}{fmt(r['precision']):>11}"
            f"{fmt(r['recall']):>9}{fmt(r['f1']):>8}"
        )
    return "\n".join(lines)


def format_examples(score: Score, limit: int = 8) -> str:
    """Show the actual misses. A score without examples is not actionable."""
    out: list[str] = []
    if score.false_negatives:
        out.append(f"\nMISSED ({len(score.false_negatives)}) - real imports the pipeline did not surface:")
        for src_repo, src_file, dst_repo, symbol in sorted(score.false_negatives)[:limit]:
            out.append(f"  {_short(src_repo)}/{src_file}  ->  {symbol}  in {_short(dst_repo)}")
        if len(score.false_negatives) > limit:
            out.append(f"  ... and {len(score.false_negatives) - limit} more")
    if score.false_positives:
        out.append(f"\nINVENTED ({len(score.false_positives)}) - edges with no import backing them:")
        for src_repo, src_file, dst_repo, symbol in sorted(score.false_positives)[:limit]:
            out.append(f"  {_short(src_repo)}/{src_file}  ->  {symbol}  in {_short(dst_repo)}")
        if len(score.false_positives) > limit:
            out.append(f"  ... and {len(score.false_positives) - limit} more")
    return "\n".join(out)


def _short(repo_id: str) -> str:
    return repo_id.rsplit("/", 1)[-1]
