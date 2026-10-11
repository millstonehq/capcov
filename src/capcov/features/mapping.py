"""Project discovered surfaces onto a FODA feature tree.

`capcov features map` is the bridge the feature layer lacked: the tree records
what a system CAN do as user-recognisable nouns, discovery records what the code
DECLARES as routes, and nothing joined them. This module joins them by rule -- a
feature claims surfaces by id glob (fnmatch over ``"http:METHOD path"``), by
OpenAPI tag, or by declaring-file glob -- and emits the ``{id: {covered, total}}``
map ``features coverage`` consumes.

Refusals that keep the projection honest, in the package's usual style:

* A surface no feature claims is NOT silently outside the product. It is listed
  under ``unassigned`` (the global-denominator property: every surface discovery
  can see is assigned or named).
* A surface several features claim counts toward each and is listed under
  ``contested`` so a human resolves the overlap rather than the tool guessing.
* Without a reconciliation, nothing is covered. ``assurance`` says
  ``"static-only"`` and every ``covered`` is zero. Declared is not exercised.
* A coverage artifact with no ``rows`` list is not read as zero rows -- most
  often it is a capabilities.json (a discovery artifact) passed where a
  reconciliation belongs, and a plausible-looking wrong answer is worse than a
  refusal.
* A feature the mapping never rules on (``unmapped_features``) and a rule that
  matched no surface at all (``empty_rules``) are both reported, not silently
  folded into zero. Duplicate surface ids in the inventory are refused outright:
  they would silently inflate a feature's total.
"""

from __future__ import annotations

import fnmatch
from collections import Counter

from . import model as model_mod

_RULE_KEYS = {"surfaces", "tags", "files"}


def validate_mapping(mapping: dict, model: dict) -> None:
    """Structural validity of a mapping against its feature model.

    A rule is ``{"surfaces": [glob, ...], "tags": [tag, ...], "files": [glob, ...]}``;
    any of the three may be omitted, but at least one must be present and
    nonempty, and no other key is recognised. Feature ids must exist in the model.
    """
    if not isinstance(mapping, dict) or mapping.get("version") != 1:
        raise ValueError("mapping version must be 1")
    features = mapping.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError("mapping needs a nonempty 'features' map")
    known = {f["id"] for f in model["features"]}
    unknown = sorted(set(features) - known)
    if unknown:
        raise ValueError(f"mapping names unknown features: {unknown}")
    for fid, rule in features.items():
        if not isinstance(rule, dict):
            raise ValueError(f"{fid}: rule must be an object")
        extra = set(rule) - _RULE_KEYS
        if extra:
            raise ValueError(f"{fid}: unknown rule keys {sorted(extra)}")
        patterns = rule.get("surfaces", [])
        tags = rule.get("tags", [])
        files = rule.get("files", [])
        if not isinstance(patterns, list) or not isinstance(tags, list) or not isinstance(files, list):
            raise ValueError(f"{fid}: 'surfaces', 'tags' and 'files' must be lists")
        if not all(isinstance(p, str) and p for p in patterns + tags + files):
            raise ValueError(f"{fid}: patterns, tags and files must be nonempty strings")
        if not patterns and not tags and not files:
            raise ValueError(f"{fid}: rule claims nothing (no surfaces, no tags, no files)")


def _matches(surface: dict, rule: dict) -> bool:
    sid = surface["id"]
    if any(fnmatch.fnmatchcase(sid, pattern) for pattern in rule.get("surfaces", [])):
        return True
    wanted = set(rule.get("tags", []))
    if wanted and wanted & set(surface.get("tags") or []):
        return True
    file_patterns = rule.get("files", [])
    if file_patterns:
        declared_file = surface.get("file") or ""
        if any(fnmatch.fnmatchcase(declared_file, pattern) for pattern in file_patterns):
            return True
    return False


def exercised_surfaces(coverage: dict) -> set[str]:
    """Every surface a reconciliation saw reached at runtime, across all rows.

    Refuses a coverage artifact with no ``rows`` list -- see the module
    docstring: reading it as zero rows would silently produce a plausible
    completeness number for the wrong file.
    """
    rows = coverage.get("rows")
    if not isinstance(rows, list):
        raise ValueError("coverage artifact has no 'rows'; expected a capcov reconcile output")
    seen: set[str] = set()
    for row in rows:
        seen.update(row.get("runtime_surfaces", []))
    return seen


def project(
    model: dict,
    mapping: dict,
    capabilities: dict,
    coverage: dict | None = None,
) -> dict:
    """Surfaces -> per-feature obligations plus the named remainder.

    Returns ``{version, assurance, obligations, surfaces_total, assigned,
    exercised, unassigned, contested, unmapped_features, empty_rules,
    excluded_surfaces, unresolved}``. ``obligations`` is exactly the map
    ``features.coverage.rollup`` takes.
    """
    model_mod.validate(model)
    validate_mapping(mapping, model)
    surfaces = capabilities.get("surfaces", [])
    if not surfaces:
        raise ValueError(
            "discovery inventory has no surfaces; refusing to map an empty denominator"
        )
    id_counts = Counter(surface["id"] for surface in surfaces)
    dupes = sorted(sid for sid, n in id_counts.items() if n > 1)
    if dupes:
        raise ValueError(f"duplicate surface ids in capabilities: {dupes}")
    excluded_raw = capabilities.get("excluded_surfaces")
    if excluded_raw is not None and not isinstance(excluded_raw, dict):
        raise ValueError("capabilities excluded_surfaces must be an object")
    exercised = exercised_surfaces(coverage) if coverage is not None else set()
    rules = mapping["features"]
    obligations = {fid: {"covered": 0, "total": 0} for fid in rules}
    claims: dict[str, list[str]] = {}
    for surface in surfaces:
        sid = surface["id"]
        owners = sorted(fid for fid, rule in rules.items() if _matches(surface, rule))
        claims[sid] = owners
        for fid in owners:
            obligations[fid]["total"] += 1
            if sid in exercised:
                obligations[fid]["covered"] += 1
    unassigned = sorted(sid for sid, owners in claims.items() if not owners)
    contested = {sid: owners for sid, owners in sorted(claims.items()) if len(owners) > 1}
    unmapped_features = sorted({f["id"] for f in model["features"]} - set(rules))
    empty_rules = sorted(fid for fid, ob in obligations.items() if ob["total"] == 0)
    excluded = excluded_raw or {}
    return {
        "version": 1,
        "assurance": "static+runtime" if coverage is not None else "static-only",
        "obligations": obligations,
        "surfaces_total": len(surfaces),
        "assigned": len(surfaces) - len(unassigned),
        "exercised": len(set(claims) & exercised),
        "unassigned": unassigned,
        "contested": contested,
        "unmapped_features": unmapped_features,
        "empty_rules": empty_rules,
        "excluded_surfaces": int(excluded.get("count", 0)),
        "unresolved": len(capabilities.get("unresolved") or []),
    }
