"""codekg - normalize extractor artifacts into Neo4j and run the cross-repo pass."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

from . import cyclescore, enrich, ids, internal_imports, manifests, oracle, scoring
from . import db
from .mappers import codegraph, gitnexus, graphify

MAPPERS = {"graphify": graphify, "codegraph": codegraph, "gitnexus": gitnexus}


def _config() -> dict:
    return yaml.safe_load(Path(os.environ.get("CORPUS_CONFIG", "/config/corpus.yaml")).read_text())


def _lock() -> dict:
    path = Path(os.environ.get("CORPUS_LOCK", "/config/corpus.lock.yaml"))
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _repos(corpus: str) -> list[dict]:
    cfg = _config()
    if corpus not in cfg["corpora"]:
        sys.exit(f"no such corpus: {corpus} (have: {', '.join(cfg['corpora'])})")
    return cfg["corpora"][corpus]["repos"]


def _ecosystem(corpus: str) -> str:
    return _config()["corpora"][corpus]["ecosystem"]


# --- commands ----------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    with db.store() as store:
        store.run_script("/cypher/00_constraints.cypher")
    print("constraints and indexes applied")


def cmd_inspect(args: argparse.Namespace) -> None:
    """Dump the real shape of an extractor artifact.

    Run this before trusting any mapper. The mappers were written against
    plausible field names, not verified ones, and this is how you find out
    where they are wrong.
    """
    artifact = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts")) / args.extractor / args.corpus / args.repo
    doc = json.loads((artifact / "graph.json").read_text())

    print(f"top-level keys: {sorted(doc)}\n")
    for key, value in doc.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{key}: {len(value)} items")
            print(f"  keys on first item: {sorted(value[0])}")
            kinds = Counter(
                (item.get("type") or item.get("kind") or item.get("label") or "?")
                for item in value[:5000]
            )
            print(f"  type/kind distribution: {dict(kinds.most_common(15))}")
            print(f"  sample: {json.dumps(value[0], indent=2)[:600]}\n")
        elif not isinstance(value, (list, dict)):
            print(f"{key}: {value!r}")


def cmd_load(args: argparse.Namespace) -> None:
    mapper = MAPPERS[args.extractor]
    ecosystem = _ecosystem(args.corpus)
    artifacts_root = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    corpus_root = Path(os.environ.get("CORPUS_DIR", "/corpus"))
    lock = _lock().get(args.corpus, {})

    with db.store() as store:
        for spec in _repos(args.corpus):
            rid = spec["id"]
            artifact = artifacts_root / args.extractor / args.corpus / rid
            if not (artifact / mapper.ARTIFACT).exists():
                print(f"  {rid}: SKIP (no artifact - extraction failed or not run)")
                continue

            repo = ids.repo_id(spec["url"])
            checkout = corpus_root / args.corpus / rid
            prov = mapper.provenance(artifact)

            if args.replace:
                store.query(db.DELETE_REPO_SUBGRAPH, repo=repo)

            store.write_batched(db.MERGE_REPO, [{
                "id": repo,
                "name": rid,
                "url": spec["url"],
                "commit": prov.get("commit") or lock.get(rid, {}).get("sha"),
                "ecosystem": ecosystem,
                "corpus": args.corpus,
                "extractor": args.extractor,
            }])

            # Manifests: what this repo publishes, and what it declares it needs.
            pkg_rows = [{
                "id": ids.package_id(ecosystem, name),
                "name": ids.normalize_package_name(ecosystem, name),
                "ecosystem": ecosystem,
                "repo": repo,
                "relation": "PUBLISHES",
                "version_spec": None,
                "source": None,
            } for name in (spec.get("publishes") or [])]

            if ecosystem == "go" and not pkg_rows:
                declared = manifests.module_path(checkout)
                if declared:
                    pkg_rows.append({
                        "id": ids.package_id(ecosystem, declared),
                        "name": declared, "ecosystem": ecosystem, "repo": repo,
                        "relation": "PUBLISHES", "version_spec": None, "source": "go.mod",
                    })

            sub = checkout / spec["subpath"] if spec.get("subpath") else checkout
            for dep in manifests.parse(ecosystem, sub):
                pkg_rows.append({
                    "id": ids.package_id(ecosystem, dep.name),
                    "name": ids.normalize_package_name(ecosystem, dep.name),
                    "ecosystem": ecosystem,
                    "repo": repo,
                    "relation": "DEPENDS_ON",
                    "version_spec": dep.version_spec,
                    "source": dep.source,
                })
            store.write_batched(db.MERGE_PACKAGE, pkg_rows)

            # Code graph.
            doc = mapper.load(artifact)
            # `language` is normalised here, not taken from the extractor: the
            # three disagree (`py` vs `python`) and MERGE_FILE assigns rather
            # than coalesces, so load order decided the value. See ids.language_of.
            n_files = store.write_batched(db.MERGE_FILE, (
                {**row, "language": ids.language_of(row["path"])}
                for row in mapper.files(doc, repo)
            ))

            symbol_rows = list(mapper.symbols(doc, repo, ecosystem))
            symbol_ids = {row["id"] for row in symbol_rows}
            n_symbols = store.write_batched(db.MERGE_SYMBOL, symbol_rows)

            n_calls = store.write_batched(db.MERGE_CALLS, mapper.calls(doc, repo, symbol_ids))

            import_rows = mapper.file_imports(doc, repo)
            if ecosystem == "go":
                # Go has no conditional and no function-scoped imports - the
                # language puts every import in one block at the top of the file
                # - so every Go edge is toplevel by definition. Python context
                # is exact and comes from the source pass in `enrich`.
                #
                # Restricted to actual .go files on both ends. A blanket label
                # over the whole corpus also tagged .md, .rb and .sh edges as
                # toplevel, because Graphify indexes Markdown and treats a link
                # as an import. The cycle projections filter on language so
                # nothing leaked, but that is one filter away from the
                # documentation-cross-links-as-cycles bug all over again.
                import_rows = (
                    {**row, "context": internal_imports.TOPLEVEL}
                    if row["src"].endswith(".go") and row["dst"].endswith(".go")
                    else row
                    for row in import_rows
                )
            n_imports = store.write_batched(db.MERGE_FILE_IMPORTS, import_rows)
            # repo_root lets the mapper recover import modules from source -
            # graphify records the imported symbol but drops the module it came
            # from. See mappers/importmap.py for why.
            # graphify anchors external refs on a Symbol; codegraph on a File.
            ref_merge = (db.MERGE_FILE_EXTERNAL_REF
                         if mapper.EXTERNAL_REF_ANCHOR == "file"
                         else db.MERGE_EXTERNAL_REF)
            n_refs = store.write_batched(
                ref_merge,
                mapper.external_refs(doc, repo, ecosystem, symbol_ids, repo_root=sub),
            )

            print(f"  {rid}: {n_files} files, {n_symbols} symbols, {n_calls} calls, "
                  f"{n_imports} module imports, {n_refs} external refs, "
                  f"{len(pkg_rows)} package edges")


def cmd_enrich(args: argparse.Namespace) -> None:
    """Add source-derived external imports the extractor's artifact omits.

    Run between `load` and `link`. See enrich.py for why this is necessary and
    what it does to the interpretation of the score.
    """
    corpus_root = Path(os.environ.get("CORPUS_DIR", "/corpus")) / args.corpus
    ecosystem = _ecosystem(args.corpus)

    artifacts_root = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))

    with db.store() as store:
        for spec in _repos(args.corpus):
            repo = ids.repo_id(spec["url"])
            root = corpus_root / spec["id"]
            sub = root / spec["subpath"] if spec.get("subpath") else root
            if not sub.is_dir():
                print(f"  {spec['id']}: SKIP (not checked out)")
                continue

            if ecosystem == "go":
                # Go symbols come from selector expressions, which needs go/ast.
                scan = oracle.load_goscan(artifacts_root, args.corpus, spec["id"])
                rows = enrich.external_refs_go(repo, scan)
            else:
                rows = enrich.external_imports(repo, sub, ecosystem)

            n = store.write_batched(db.MERGE_FILE_EXTERNAL_REF, rows)

            # Intra-repo imports, classified toplevel/typing/deferred. Required
            # for honest cycle detection: no extractor records where an import
            # sits, and a `if TYPE_CHECKING:` cycle never executes.
            n_int = store.write_batched(
                db.MERGE_FILE_IMPORTS,
                internal_imports.classified_imports(repo, sub, ecosystem),
            )
            suffix = f", {n_int} classified module imports" if n_int else ""
            print(f"  {spec['id']}: {n} source-derived import refs{suffix}")


def cmd_gds(args: argparse.Namespace) -> None:
    """Run org-level graph algorithms over the joined graph."""
    with db.store() as store:
        results = store.run_script("/cypher/20_gds.cypher")
    for rows in results:
        for row in rows:
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))


def cmd_link(args: argparse.Namespace) -> None:
    with db.store() as store:
        results = store.run_script("/cypher/10_link_cross_repo.cypher")
    report = results[-1] if results else []
    if not report:
        print("cross-repo pass created nothing.")
        print("run `codekg query q5` to see which imports are still dangling -")
        print("if they name a package a corpus repo publishes, the pass is at fault.")
        return
    print(f"{'relationship':<24} {'method':<26} {'edges':>8} {'avg conf':>9}")
    for row in report:
        print(f"{row['relationship']:<24} {str(row['method']):<26} "
              f"{row['edges']:>8} {row['avg_confidence']:>9}")


def cmd_query(args: argparse.Namespace) -> None:
    """Run one demo query by its Q-number from cypher/90_queries.cypher."""
    body = Path("/cypher/90_queries.cypher").read_text()
    blocks = [b for b in body.split("\n\n\n") if b.strip()]
    wanted = args.name.upper()
    for block in blocks:
        if f"// {wanted} " in block or f"// {wanted}\n" in block:
            stmt = "\n".join(l for l in block.splitlines() if not l.strip().startswith("//")).strip()
            stmt = stmt.rstrip(";")
            if not stmt:
                continue
            params = dict(p.split("=", 1) for p in args.param)
            with db.store() as store:
                rows = store.query(stmt, **params)
            print(json.dumps(rows, indent=2, default=str))
            return
    sys.exit(f"no query named {wanted} in 90_queries.cypher")


def cmd_score(args: argparse.Namespace) -> None:
    """Score cross-repo edges against source-derived ground truth."""
    corpus_root = Path(os.environ.get("CORPUS_DIR", "/corpus")) / args.corpus
    artifacts_root = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    repos = _repos(args.corpus)
    ecosystem = _ecosystem(args.corpus)

    present = [r for r in repos if (corpus_root / r["id"]).is_dir()]
    if len(present) < len(repos):
        missing = ", ".join(r["id"] for r in repos if r not in present)
        print(f"  not fetched, excluded from scoring: {missing}")
    print(f"building oracle from source ({ecosystem}, {len(present)} repos)...")
    truth, diag = oracle.build(present, corpus_root, ecosystem,
                               artifacts_root=artifacts_root, corpus=args.corpus)
    print(f"  importable surface: "
          + ", ".join(f"{k.rsplit('/',1)[-1]}={v}" for k, v in diag["surface_sizes"].items()))
    print(f"  ground-truth cross-repo edges: {len(truth)}")
    if diag["unmatched_imports"]:
        print(f"  note: {diag['unmatched_imports']} imports named a corpus package but the "
              f"symbol was not discoverable in it (submodule imports); excluded from truth")

    corpus_repos = [ids.repo_id(r["url"]) for r in present]
    dir_to_repo = {r["id"]: ids.repo_id(r["url"]) for r in present}
    with db.store() as store:
        predicted = scoring.predicted_edges(store, corpus_repos)
        predicted_plus = scoring.predicted_edges_with_source(store, corpus_repos)
        repo_predicted = scoring.predicted_repo_edges(store, corpus_repos)
        per_extractor = {
            name: scoring.predicted_edges_for(store, corpus_repos, name)
            for name in sorted(MAPPERS)
            if (artifacts_root / name / args.corpus).is_dir()
        }

    truth_repo_level = scoring.repo_level_from_symbol_edges(truth)
    single_tool = scoring.single_tool_cross_repo_edges(
        artifacts_root, args.extractor, args.corpus
    )

    scores = [
        scoring.Score("raw", set(), truth),
        scoring.Score("single-tool", set(), truth),
        # manifest-only knows repo pairs but claims nothing at symbol level,
        # so at this granularity it scores zero. That is the point of showing it.
        scoring.Score("manifest-only", set(), truth),
        scoring.Score("unified", predicted, truth),
        scoring.Score("unified+source", predicted_plus, truth),
    ]

    # One arm per extractor that actually produced artifacts, so the two can be
    # compared on the cross-repo task rather than only on agreement.
    for name, per in per_extractor.items():
        scores.insert(-1, scoring.Score(f"artifact:{name}", per, truth))

    merge_diag = None
    if args.merge:
        raw, merge_diag = scoring.merged_graph_edges(
            Path(args.merge), artifacts_root, args.extractor, args.corpus
        )
        # Map artifact directory names onto canonical repo ids.
        merged = {
            (dir_to_repo.get(a), f, dir_to_repo.get(b), sym)
            for a, f, b, sym in raw
            if dir_to_repo.get(a) and dir_to_repo.get(b)
        }
        scores.insert(3, scoring.Score("graphify-merge", merged, truth))

    print("\n=== SYMBOL-LEVEL: (importing file -> symbol in another repo) ===")
    print(scoring.format_table(scores))
    print(f"\nsingle-tool arm verified from artifacts: {single_tool} cross-repo edges present")
    if merge_diag:
        print(f"\n`graphify-merge` is `graphify merge-graphs` over the same artifacts, given")
        print(f"perfect repo attribution so it is judged on its edges alone.")
        print(f"  raw cross-repo edges it produced: {merge_diag['cross_repo_edges_raw']}")
        print(f"  by relation: {merge_diag['by_relation']}")
    print("\n`unified` is the independent measurement - only what the extractor's artifact")
    print("supports. `unified+source` adds imports read from source; it shares its method")
    print("with the oracle, so its recall is a CEILING DIAGNOSTIC, not an accuracy claim.")

    repo_scores = [
        scoring.Score("manifest-only", repo_predicted, truth_repo_level),
        scoring.Score("unified", scoring.repo_level_from_symbol_edges(predicted), truth_repo_level),
        scoring.Score("unified+source",
                      scoring.repo_level_from_symbol_edges(predicted_plus), truth_repo_level),
    ]
    print("\n=== REPO-LEVEL: (repo -> repo) ===")
    print(scoring.format_table(repo_scores))
    print("\nRepo level is where an SCA tool already does well. It is included so the")
    print("symbol-level table above is read as the increment, not the whole result.")

    unified = scores[-1]
    print(scoring.format_examples(unified, limit=args.examples))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "corpus": args.corpus,
            "symbol_level": [s.row() for s in scores],
            "repo_level": [s.row() for s in repo_scores],
            "single_tool_verified_cross_repo_edges": single_tool,
            "oracle": {k: v for k, v in diag.items() if k != "publishers"},
            "false_negatives": sorted(unified.false_negatives),
            "false_positives": sorted(unified.false_positives),
        }, indent=2, default=str))
        print(f"\nwrote {args.json}")


def cmd_score_cycles(args: argparse.Namespace) -> None:
    """Score the graph's cycle finding against pylint's cyclic-import check.

    See cyclescore.py for why the comparison is set-based and why the scored
    arm is the facade-inclusive one.
    """
    corpus_root = Path(os.environ.get("CORPUS_DIR", "/corpus")) / args.corpus
    ecosystem = _ecosystem(args.corpus)
    if ecosystem != "python":
        sys.exit(
            "cycle scoring is Python-only. Go forbids circular package imports at "
            "compile time, so zero is the only correct answer for a Go corpus and "
            "there is nothing to score against."
        )

    with db.store() as store:
        for spec in _repos(args.corpus):
            repo = ids.repo_id(spec["url"])
            root = corpus_root / spec["id"]
            sub = root / spec["subpath"] if spec.get("subpath") else root
            if not sub.is_dir():
                print(f"  {spec['id']}: SKIP (not checked out)")
                continue

            index = internal_imports.module_index(sub)
            packages = cyclescore.top_level_packages(index)
            prefix = (spec["subpath"].rstrip("/") + "/") if spec.get("subpath") else ""

            print(f"\n=== {spec['id']} ===")
            print(f"  running pylint --enable=cyclic-import over {', '.join(packages)} "
                  f"({len(index)} modules)...")
            truth, diag = cyclescore.pylint_cycles(sub, packages, timeout=args.timeout)
            if diag.get("error"):
                print(f"  ORACLE UNAVAILABLE: {diag['error']}")
                continue
            print(f"  pylint: {diag['chains_reported']} overlapping chains naming "
                  f"{diag['modules_in_cycles']} distinct modules")

            # A 2x2: facades on/off x TYPE_CHECKING on/off. One variable at a
            # time, so the deltas are readable. pylint sits in the top row - it
            # makes no facade distinction - which is why that is the scored arm.
            arms = [
                ("all contexts, with facades (scored)", "sccAllCtxId"),
                ("toplevel + typing, with facades", "sccAllDesignId"),
                ("toplevel only, with facades", "sccId"),
                ("toplevel only, facade-free", "sccCoreId"),
                ("toplevel + typing, facade-free", "sccDesignId"),
            ]
            scores = []
            for label, prop in arms:
                members = cyclescore.graph_cycle_members(store, repo, prop)
                scores.append(cyclescore.CycleScore(
                    label, cyclescore.to_modules(members, index, prefix), truth
                ))

            print(cyclescore.format_table(scores))
            scored = scores[0]
            missed = sorted(scored.truth - scored.predicted)[:6]
            extra = sorted(scored.predicted - scored.truth)[:6]
            if missed:
                print(f"  pylint-only (we miss): {', '.join(missed)}")
            if extra:
                print(f"  graph-only (we add):   {', '.join(extra)}")

    print("\nThe SCORED arm is `all contexts, with facades`, because that is what")
    print("pylint actually models: it makes no facade distinction and it counts")
    print("function-scoped imports, so it reports cycles the standard cycle-breaking")
    print("idiom has already broken. Read the arms downward - each row removes one")
    print("class of edge that does not constitute a runtime cycle. The last row is")
    print("the finding; the gap between first and last is the noise we remove.")


def cmd_stats(args: argparse.Namespace) -> None:
    with db.store() as store:
        for label in ("Repo", "File", "Symbol", "Package", "ExternalRef"):
            n = store.query(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            print(f"{label:<14} {n:>9,}")
        print()
        for rel in ("CONTAINS", "DECLARES", "CALLS", "USES", "RESOLVES_TO",
                    "CALLS_CROSS_REPO", "DEPENDS_ON", "PUBLISHES"):
            n = store.query(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n")[0]["n"]
            print(f"{rel:<20} {n:>9,}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="codekg")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="apply constraints and indexes").set_defaults(fn=cmd_init)

    p = sub.add_parser("inspect", help="dump an extractor artifact's real shape")
    p.add_argument("extractor", choices=list(MAPPERS))
    p.add_argument("corpus")
    p.add_argument("repo")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("load", help="normalize artifacts and load Neo4j")
    p.add_argument("corpus")
    p.add_argument("--extractor", choices=list(MAPPERS), default="graphify")
    p.add_argument("--replace", action="store_true",
                   help="drop each repo's existing subgraph first (use after a re-extract)")
    p.set_defaults(fn=cmd_load)

    p = sub.add_parser("enrich", help="add source-derived imports the artifact omits")
    p.add_argument("corpus")
    p.set_defaults(fn=cmd_enrich)

    sub.add_parser("link", help="run the cross-repo resolution pass").set_defaults(fn=cmd_link)

    sub.add_parser("gds", help="org-level GDS over the joined graph").set_defaults(fn=cmd_gds)

    p = sub.add_parser("query", help="run a demo query (q1..q7)")
    p.add_argument("name")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("score", help="score cross-repo edges against source-derived truth")
    p.add_argument("corpus")
    p.add_argument("--extractor", choices=list(MAPPERS), default="graphify")
    p.add_argument("--examples", type=int, default=8, help="how many FP/FN to print")
    p.add_argument("--json", metavar="PATH", help="also write full results as JSON")
    p.add_argument("--merge", metavar="PATH",
                   help="also score a `graphify merge-graphs` output as a baseline arm")
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("score-cycles", help="score cycle finding against pylint")
    p.add_argument("corpus")
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds to allow pylint per repo (default 1800)")
    p.set_defaults(fn=cmd_score_cycles)

    sub.add_parser("stats", help="node and relationship counts").set_defaults(fn=cmd_stats)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
