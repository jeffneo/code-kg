"""Score the graph's cycle finding against pylint, an independent oracle.

WHY PYLINT
----------
Same principle as using dependency manifests for cross-repo truth: the answer
key has to come from something that is not us. pylint's `cyclic-import` (R0401)
is the canonical Python cycle checker, it implements a different algorithm over
its own import graph, and anyone in the room can run it and check. Our
extractors never see it.

WHAT THE COMPARISON IS, AND WHY IT IS SET-BASED
----------------------------------------------
pylint reports cycles as overlapping *chains*, not as components. Measured on
neo4j-python-driver: 8+ R0401 findings, several sharing the same edges, for what
is a single strongly connected component of 99 modules. There is no way to
recover "how many distinct problems do I have" from that output.

So the two tools are not comparable cycle-for-cycle. What IS comparable is the
set of modules that participate in at least one cycle, which both can express.
That is what this scores.

THE GRANULARITY TRAP
--------------------
pylint makes no distinction between a package `__init__.py` and a real module -
its chains run straight through `neo4j.time` and `neo4j._work`, both of which are
facades. So the honest comparison is against our facade-INCLUSIVE component set.
The facade-free number is reported alongside as the refinement it is, never as
the thing being scored. Comparing our facade-free set against pylint's inclusive
set would manufacture false negatives and flatter us.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .internal_imports import module_index

# pylint: path:line:col: R0401: Cyclic import (a -> b -> c) (cyclic-import)
R0401 = re.compile(r"R0401:\s*Cyclic import\s*\((?P<chain>[^)]*)\)")


@dataclass
class CycleScore:
    arm: str
    predicted: set[str]
    truth: set[str]

    @property
    def tp(self) -> int:
        return len(self.predicted & self.truth)

    @property
    def precision(self) -> float:
        return self.tp / len(self.predicted) if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.tp / len(self.truth) if self.truth else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def row(self) -> dict:
        return {
            "arm": self.arm,
            "predicted": len(self.predicted),
            "truth": len(self.truth),
            "tp": self.tp,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
        }


def top_level_packages(index: dict[str, str]) -> list[str]:
    """Distinct top-level importable package names in a checkout."""
    return sorted({m.split(".")[0] for m in index})


def pylint_cycles(root: Path, packages: list[str], timeout: int = 1800) -> tuple[set[str], dict]:
    """Modules pylint says are in at least one cycle, plus diagnostics.

    Runs with cwd=root so the packages are importable as pylint expects, and
    with every check but cyclic-import disabled - pylint's full run on a large
    package is minutes of CPU for output we would throw away.
    """
    cmd = [
        "pylint", "--disable=all", "--enable=cyclic-import",
        "--score=n", "--persistent=n", *packages,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return set(), {"error": "pylint not installed in this image"}
    except subprocess.TimeoutExpired:
        return set(), {"error": f"pylint exceeded {timeout}s"}

    members: set[str] = set()
    chains = 0
    for match in R0401.finditer(proc.stdout):
        chain = [part.strip() for part in match.group("chain").split("->")]
        members.update(p for p in chain if p)
        chains += 1

    diag = {"chains_reported": chains, "modules_in_cycles": len(members)}
    if not chains and proc.returncode not in (0, 4, 8, 16):
        # pylint uses a bitmask exit status; a fatal (1) means it never ran.
        diag["error"] = (proc.stderr or proc.stdout)[-400:]
    return members, diag


GRAPH_CYCLE_QUERY = """
MATCH (f:File {repo: $repo})
WHERE f[$prop] IS NOT NULL
WITH f[$prop] AS component, collect(f.path) AS paths
WHERE size(paths) > 1
UNWIND paths AS path
RETURN collect(DISTINCT path) AS paths
"""


def graph_cycle_members(store, repo: str, prop: str) -> set[str]:
    """Repo-relative paths of files in a strongly connected component of size>1."""
    rows = store.query(GRAPH_CYCLE_QUERY, repo=repo, prop=prop)
    return set(rows[0]["paths"]) if rows and rows[0]["paths"] else set()


def to_modules(paths: set[str], index: dict[str, str], prefix: str = "") -> set[str]:
    """Map file paths onto dotted module names.

    Graph paths are already relative to the corpus `subpath`, because extraction
    runs inside it - SQLAlchemy's session module is `sqlalchemy/orm/session.py`,
    not `lib/sqlalchemy/orm/session.py`. So is the module index, which is built
    from the same directory, and the two line up directly.

    `prefix` is therefore a no-op for every corpus here. It is kept, and applied
    only when a path actually starts with it, so that an extractor which does
    report repo-root-relative paths cannot silently produce zero matches - which
    would read as "no cycles found" rather than as a mapping failure.
    """
    reverse = {p: m for m, p in index.items()}
    out = set()
    for path in paths:
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        mod = reverse.get(rel)
        if mod:
            out.add(mod)
    return out


def format_table(scores: list[CycleScore]) -> str:
    head = f"{'arm':<34}{'predicted':>10}{'truth':>7}{'tp':>6}{'prec':>7}{'recall':>8}{'f1':>7}"
    lines = [head, "-" * len(head)]
    for s in scores:
        lines.append(
            f"{s.arm:<34}{len(s.predicted):>10}{len(s.truth):>7}{s.tp:>6}"
            f"{s.precision:>7.3f}{s.recall:>8.3f}{s.f1:>7.3f}"
        )
    return "\n".join(lines)
