"""Known vulnerabilities, from OSV, joined onto the packages already in the graph.

WHY THIS IS THE EASY LAYER
--------------------------
Everything else in this harness had to resolve names: an unresolved import in one
repo bound to a symbol in another, by module prefix or module path. This layer
joins on an identity that is already exact - a package name and an ecosystem -
so it is strictly less work than anything already proven here. That is the point
worth making: the hard part was the code graph, and it is done.

WHAT THIS ADDS THAT AN SCA TOOL DOES NOT
----------------------------------------
An SCA tool reports "you depend on X, and X has CVE-Y". It cannot tell you
whether your code ever touches X, because it never parsed your source. This
layer sits on a graph that already knows every import site, so the same finding
becomes "you depend on X, X has CVE-Y, and here are the 4 files that import it".

Three cells of that matrix matter, and only a joined graph has all three:

  declared + imported     a real exposure, with file:line
  declared + NOT imported unused dependency - attack surface you can just delete
  imported + NOT declared PHANTOM. The package is not in your SBOM, so an SCA
                          scan of this repo will never attribute the CVE to it.
                          This is the cell that is invisible to both tool
                          categories on their own.

HONESTY ABOUT VERSIONS - READ BEFORE QUOTING A COUNT
----------------------------------------------------
We know what a manifest DECLARES, not what a build RESOLVED. Those differ:

  exact pin (`==1.4.7`, or a go.mod version)  -> a definite verdict
  floating range (`>=5.25.0,<7.0.0`)          -> INDETERMINATE, and it stays
                                                 indeterminate until someone
                                                 resolves it

That is not a gap in this code, it is a true statement about the input, and it
is a security finding in itself: an unpinned dependency has no determinable
vulnerability status. Roughly a fifth of the declared dependencies in this
corpus are floating. `status` records which verdict you are looking at, and
nothing collapses the three into one number.

Go pseudo-versions (`v0.0.0-20260806130614-601b98d401d3`) are also treated as
indeterminate: they cannot be ordered against semver bounds reliably, and
guessing would be worse than saying so.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Iterator

from . import ids

OSV_QUERY = "https://api.osv.dev/v1/query"

# Our ecosystem names -> OSV's.
OSV_ECOSYSTEM = {"python": "PyPI", "go": "Go"}

AFFECTED = "affected"
NOT_AFFECTED = "not_affected"
INDETERMINATE = "indeterminate"


# --- version handling --------------------------------------------------------

def _parse(version: str):
    """Best-effort comparable version. None when it cannot be ordered."""
    from packaging.version import InvalidVersion, Version

    v = (version or "").strip().lstrip("vV")
    if not v:
        return None
    # Go pseudo-version: 0.0.0-20260806130614-601b98d401d3. Not orderable
    # against semver bounds; say so rather than guess.
    if "-" in v and any(part.isdigit() and len(part) >= 12 for part in v.split("-")):
        return None
    try:
        return Version(v)
    except InvalidVersion:
        return None


def resolved_version(spec: str | None, ecosystem: str) -> str | None:
    """The single version a declaration pins to, or None if it does not pin one.

    Python: only `==X` (and `===X`) pins. Anything with a comma, or a `>`/`<`/`~`
    /`^`/`*`, leaves a range open.
    Go: go.mod records an exact version, so the spec IS the version.
    """
    if not spec:
        return None
    spec = spec.strip()
    if ecosystem == "go":
        return spec.lstrip("vV") or None
    if "," in spec:
        return None
    if spec.startswith("==="):
        return spec[3:].strip() or None
    if spec.startswith("=="):
        rest = spec[2:].strip()
        return None if "*" in rest else (rest or None)
    return None


def _in_range(version, introduced: str | None, fixed: str | None,
              last_affected: str | None = None) -> bool | None:
    """Is `version` inside this affected interval? None when undecidable.

    OSV has THREE bound event types and they are not interchangeable:

      fixed          exclusive upper bound - affected is [introduced, fixed)
      last_affected  INCLUSIVE upper bound - affected is [introduced, last]
      limit          exclusive, same shape as fixed

    Reading only `introduced`/`fixed` treats a `last_affected` range as
    unbounded, which reports every version as affected. That is exactly what
    happened: torch 2.10.0 was flagged by 9 advisories whose ranges are
    `{introduced: 0, last_affected: 2.6.0-cu124}`, while OSV's own resolver
    correctly says 2. An upper bound that is silently dropped fails in the
    direction that manufactures findings, which is the worst direction for
    anything security-adjacent.
    """
    lo = _parse(introduced) if introduced and introduced != "0" else None
    if introduced and introduced != "0" and lo is None:
        return None
    if lo is not None and version < lo:
        return False

    if fixed:
        hi = _parse(fixed)
        if hi is None:
            return None
        return version < hi
    if last_affected:
        hi = _parse(last_affected)
        if hi is None:
            return None
        return version <= hi
    # No upper bound at all: genuinely open-ended, e.g. an unfixed advisory.
    return True


def _specifier(spec: str, ecosystem: str):
    """The declared constraint as a testable SpecifierSet, or None."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    if ecosystem != "python":
        return None
    try:
        return SpecifierSet(spec)
    except InvalidSpecifier:
        return None


def assess(spec: str | None, ecosystem: str, affects: list[dict]) -> tuple[str, str]:
    """(status, reason) for one repo's declaration against one advisory.

    A floating range is NOT automatically indeterminate, and getting this wrong
    made the first version of this layer useless: it reported CVE-2012-0805
    against a modern SQLAlchemy as "indeterminate" simply because the pin was a
    range. But `sqlalchemy>=1.4.49` permits *no* version that advisory affects,
    so the correct answer is NOT_AFFECTED, and it is decidable.

    So the test is set intersection, not a pin lookup: does the declared
    constraint permit any version this advisory affects?

      permits none                 -> not_affected   (definite)
      declaration pins exactly one -> affected       (definite)
      permits some but not all     -> indeterminate  (genuinely unknown until
                                                      something resolves it)
    """
    if not spec:
        return INDETERMINATE, "no version constraint declared at all"

    listed = sorted({v for a in affects for v in (a.get("versions") or [])})
    pinned = resolved_version(spec, ecosystem)

    # Preferred path: an explicit affected-version list tested against the
    # declared constraint. Decidable even when the declaration is a range.
    specifier = _specifier(spec, ecosystem)
    if listed and specifier is not None:
        permitted = [v for v in listed if _permits(specifier, v)]
        if not permitted:
            return NOT_AFFECTED, (
                f"'{spec}' permits none of the {len(listed)} versions this "
                "advisory affects"
            )
        if pinned is not None:
            return AFFECTED, f"{pinned} is in the advisory's affected-version list"
        return INDETERMINATE, (
            f"'{spec}' permits {len(permitted)} of the {len(listed)} affected "
            f"versions (e.g. {permitted[0]}) - resolve it to decide"
        )

    if listed and pinned is not None:
        if pinned in listed or pinned.lstrip("vV") in listed:
            return AFFECTED, f"{pinned} is in the advisory's affected-version list"

    # Fall back to bound comparison. This is the Go path: go.mod records an exact
    # version and Go advisories are usually ranges without a version list.
    if pinned is None:
        return INDETERMINATE, (
            f"declared as '{spec}', which does not pin a single version, and the "
            "advisory gives no version list to intersect against"
        )
    parsed = _parse(pinned)
    if parsed is None:
        return INDETERMINATE, (
            f"'{pinned}' cannot be ordered against the advisory bounds "
            "(Go pseudo-versions are not semver-comparable)"
        )

    undecidable = False
    for a in affects:
        verdict = _in_range(parsed, a.get("introduced"), a.get("fixed"),
                            a.get("last_affected"))
        if verdict is None:
            undecidable = True
        elif verdict:
            lo = a.get("introduced") or "0"
            hi = a.get("fixed") or a.get("last_affected") or "unfixed"
            close = ")" if a.get("fixed") or not a.get("last_affected") else "]"
            return AFFECTED, f"{pinned} is within [{lo}, {hi}{close}"
    if undecidable:
        return INDETERMINATE, f"advisory bounds could not be compared to {pinned}"
    if listed:
        return NOT_AFFECTED, f"{pinned} is not in the advisory's affected-version list"
    return NOT_AFFECTED, f"{pinned} is outside every affected range"


def _permits(specifier, version: str) -> bool:
    """Does the declared constraint allow this version? Prereleases included.

    `prereleases=True` matters: without it a specifier silently excludes every
    prerelease, so an advisory whose affected list is all prereleases would read
    as not_affected.
    """
    try:
        return specifier.contains(version, prereleases=True)
    except Exception:
        return False


# --- fetching ----------------------------------------------------------------

def _post(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def _severity(vuln: dict) -> tuple[str | None, str | None]:
    """(label, cvss vector). OSV puts the label in database_specific."""
    label = (vuln.get("database_specific") or {}).get("severity")
    vector = None
    for entry in vuln.get("severity") or []:
        vector = entry.get("score") or vector
    return label, vector


def _affects_for(vuln: dict, name: str, osv_eco: str) -> list[dict]:
    """The advisory's affected entries that match this exact package."""
    out: list[dict] = []
    for affected in vuln.get("affected") or []:
        pkg = affected.get("package") or {}
        if pkg.get("ecosystem") != osv_eco:
            continue
        if (pkg.get("name") or "").lower() != name.lower():
            continue
        versions = affected.get("versions") or []
        ranges = affected.get("ranges") or []
        if not ranges:
            out.append({"introduced": None, "fixed": None,
                        "last_affected": None, "versions": versions})
            continue
        for rng in ranges:
            # One events list can describe SEVERAL disjoint intervals:
            #   [{introduced: 0}, {fixed: 1.2}, {introduced: 2.0}, {fixed: 2.1}]
            # means [0,1.2) and [2.0,2.1). Collapsing to the last introduced and
            # the last fixed - which this did - silently merges them into one
            # wrong interval, so each `introduced` opens a new one here.
            current: dict | None = None
            for event in rng.get("events") or []:
                if "introduced" in event:
                    if current is not None:
                        out.append({**current, "versions": versions})
                    current = {"introduced": event["introduced"],
                               "fixed": None, "last_affected": None}
                    continue
                if current is None:
                    current = {"introduced": "0", "fixed": None, "last_affected": None}
                if "fixed" in event:
                    current["fixed"] = event["fixed"]
                elif "last_affected" in event:
                    current["last_affected"] = event["last_affected"]
                elif "limit" in event:
                    current["fixed"] = event["limit"]   # exclusive, like `fixed`
            if current is not None:
                out.append({**current, "versions": versions})
    return out


def fetch(packages: Iterable[tuple[str, str]], timeout: int = 60,
          workers: int = 8) -> dict[tuple[str, str], list[dict]]:
    """{(ecosystem, name): [advisory, ...]} from OSV.

    One request per package rather than the batch endpoint: querybatch returns
    ids only, so the details would need a second round trip per advisory
    anyway - which is more requests, not fewer.
    """
    targets = [(eco, name) for eco, name in packages if eco in OSV_ECOSYSTEM]

    def one(target: tuple[str, str]) -> tuple[tuple[str, str], list[dict]]:
        eco, name = target
        payload = {"package": {"name": name, "ecosystem": OSV_ECOSYSTEM[eco]}}
        try:
            doc = _post(OSV_QUERY, payload, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError):
            return target, []
        return target, doc.get("vulns") or []

    out: dict[tuple[str, str], list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for target, vulns in pool.map(one, targets):
            if vulns:
                out[target] = vulns
    return out


# --- row shaping -------------------------------------------------------------

def vulnerability_rows(advisories: dict[tuple[str, str], list[dict]]) -> Iterator[dict]:
    """One row per distinct advisory. Deduped: one CVE can hit many packages."""
    seen: set[str] = set()
    for vulns in advisories.values():
        for vuln in vulns:
            vid = vuln.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            label, vector = _severity(vuln)
            aliases = vuln.get("aliases") or []
            yield {
                "id": vid,
                # The CVE id is what people search for; OSV's own id is a GHSA.
                "cve": next((a for a in aliases if a.startswith("CVE-")), None),
                "aliases": aliases,
                "summary": (vuln.get("summary") or "")[:400],
                "severity": label,
                "cvss": vector,
                "cwes": (vuln.get("database_specific") or {}).get("cwe_ids") or [],
                "published": vuln.get("published"),
                "modified": vuln.get("modified"),
                "withdrawn": vuln.get("withdrawn"),
            }


def affects_rows(advisories: dict[tuple[str, str], list[dict]]) -> Iterator[dict]:
    """Vulnerability -> Package, one row per affected range."""
    for (eco, name), vulns in advisories.items():
        pkg = ids.package_id(eco, name)
        for vuln in vulns:
            for entry in _affects_for(vuln, name, OSV_ECOSYSTEM[eco]):
                yield {
                    "vuln": vuln["id"],
                    "package": pkg,
                    "introduced": entry["introduced"],
                    "fixed": entry["fixed"],
                    "version_count": len(entry["versions"]),
                }


def assessment_rows(
    declarations: list[dict],
    advisories: dict[tuple[str, str], list[dict]],
) -> Iterator[dict]:
    """Repo -> Vulnerability, with the verdict for that repo's declared version.

    `declarations` comes from the graph: one row per (repo, package, spec).
    NOT_AFFECTED is emitted too - "we checked and you are fine" is a result, and
    without it the absence of an edge is ambiguous between "safe" and "not yet
    scanned".
    """
    for decl in declarations:
        key = (decl["ecosystem"], decl["package_name"])
        for vuln in advisories.get(key, []):
            entries = _affects_for(vuln, decl["package_name"], OSV_ECOSYSTEM[key[0]])
            if not entries:
                continue
            status, reason = assess(decl["version_spec"], decl["ecosystem"], entries)
            yield {
                "repo": decl["repo"],
                "vuln": vuln["id"],
                "package": decl["package"],
                "status": status,
                "reason": reason,
                "declared": decl["version_spec"],
                "resolved": resolved_version(decl["version_spec"], decl["ecosystem"]),
            }
