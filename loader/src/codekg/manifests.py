"""Dependency manifest parsing.

Manifests do double duty:

1. Linking substrate. They tell us which repo publishes which package, which is
   how an unresolved import in repo A gets bound to a symbol in repo B.

2. Ground truth. The declared dependency set is the answer key for scoring. The
   extractors never see it as a graph - they parse source and hit an
   unresolvable external import - so scoring against it is a fair test rather
   than a leak.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    version_spec: str | None
    source: str  # manifest file it came from, relative to repo root


_PY_REQ = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?P<extras>\[[^\]]*\])?\s*"
    r"(?P<spec>[<>=!~^].*)?\s*$"
)


def parse_python(repo_root: Path) -> list[Dependency]:
    deps: list[Dependency] = []
    seen: set[str] = set()

    for req in sorted(repo_root.rglob("requirements*.txt")):
        if _is_excluded(req, repo_root):
            continue
        rel = str(req.relative_to(repo_root))
        for raw in req.read_text(errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            # Skip pip directives (-r, -e, --index-url) and bare URLs.
            if not line or line.startswith("-") or "://" in line:
                continue
            m = _PY_REQ.match(line)
            if not m:
                continue
            name = m.group("name")
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            deps.append(Dependency("python", name, (m.group("spec") or "").strip() or None, rel))

    for pyproject in sorted(repo_root.rglob("pyproject.toml")):
        if _is_excluded(pyproject, repo_root):
            continue
        rel = str(pyproject.relative_to(repo_root))
        try:
            doc = tomllib.loads(pyproject.read_text(errors="replace"))
        except tomllib.TOMLDecodeError:
            continue
        for entry in doc.get("project", {}).get("dependencies", []) or []:
            m = _PY_REQ.match(str(entry))
            if not m:
                continue
            name = m.group("name")
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            deps.append(Dependency("python", name, (m.group("spec") or "").strip() or None, rel))

    return deps


def _go_replacements(text: str) -> dict[str, tuple[str, str | None]]:
    """module -> (replacement module, replacement version) from `replace` lines.

    Handles both forms:
        replace old => new v1.2.3
        replace ( old => new v1.2.3 )

    This matters more than it looks. Mimir requires
    `github.com/prometheus/prometheus v1.99.0` - a placeholder version that does
    not exist upstream - and then replaces it with `grafana/mimir-prometheus`.
    Read the require line alone and you conclude Mimir ships upstream Prometheus
    1.99.0, which produced a confident, wrong CVE finding against it. The build
    never uses that module at all.
    """
    out: dict[str, tuple[str, str | None]] = {}
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("replace ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        body = line[len("replace "):] if line.startswith("replace ") else (line if in_block else None)
        if not body or "=>" not in body:
            continue
        left, right = body.split("=>", 1)
        old = left.split()[0] if left.split() else None
        parts = right.split()
        if not old or not parts:
            continue
        out[old] = (parts[0], parts[1] if len(parts) > 1 else None)
    return out


def parse_go(repo_root: Path) -> list[Dependency]:
    """Parse go.mod require blocks, applying `replace` directives.

    Only direct requires count as ground truth. Lines marked `// indirect` are
    transitive and the extractors have no reason to surface them, so counting
    them would tank recall for the wrong reason.
    """
    deps: list[Dependency] = []
    seen: set[str] = set()

    for gomod in sorted(repo_root.rglob("go.mod")):
        if _is_excluded(gomod, repo_root):
            continue
        rel = str(gomod.relative_to(repo_root))
        text = gomod.read_text(errors="replace")
        replacements = _go_replacements(text)
        in_block = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("require ("):
                in_block = True
                continue
            if in_block and line == ")":
                in_block = False
                continue
            if line.startswith("//") or not line:
                continue

            if in_block:
                spec_line = line
            elif line.startswith("require "):
                spec_line = line[len("require "):].strip()
            else:
                continue

            if "// indirect" in spec_line:
                continue
            parts = spec_line.split("//", 1)[0].split()
            if len(parts) < 2:
                continue
            module, version = parts[0], parts[1]
            # A replaced module is not what the build resolves. Follow it, so the
            # dependency recorded is the one actually compiled - and so a CVE
            # against the replaced-away module is not attributed here.
            if module in replacements:
                new_module, new_version = replacements[module]
                # A filesystem replacement (`=> ./pkg/push`) has no version and
                # is a local directory, not a published dependency.
                if new_module.startswith((".", "/")):
                    continue
                module, version = new_module, new_version or version
            if module in seen:
                continue
            seen.add(module)
            deps.append(Dependency("go", module, version, rel))

    return deps


def module_path(repo_root: Path) -> str | None:
    """The Go module path this repo publishes, from its root go.mod."""
    gomod = repo_root / "go.mod"
    if not gomod.exists():
        return None
    for raw in gomod.read_text(errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("module "):
            return line[len("module "):].strip()
    return None


def parse(ecosystem: str, repo_root: Path) -> list[Dependency]:
    if ecosystem == "python":
        return parse_python(repo_root)
    if ecosystem == "go":
        return parse_go(repo_root)
    raise ValueError(f"unsupported ecosystem: {ecosystem}")


_EXCLUDED_PARTS = {"vendor", "node_modules", ".venv", "venv", "testdata", "dist", "build", ".git"}


def _is_excluded(path: Path, root: Path) -> bool:
    return bool(_EXCLUDED_PARTS.intersection(path.relative_to(root).parts))
