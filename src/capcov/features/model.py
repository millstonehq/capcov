"""FODA feature models: the capability-decomposition tree capcov never formalised.

A feature model (Kang et al., Feature-Oriented Domain Analysis, SEI
CMU/SEI-90-TR-21) is a tree of features -- user-recognisable capabilities --
refined by:

  mandatory / optional   a solitary child is always present with its parent, or may be;
  alternative / or group  a feature's children form a group: choose exactly one, or one-or-more;
  requires / excludes      cross-tree implication and mutual exclusion.

The model is a plain dict, so it round-trips through JSON with no schema library.
Each feature record is ``{id, name, parent, decomposition, group}``:

  parent         the parent feature id; null (or absent) on the root only.
  decomposition  "mandatory" | "optional" | null -- how a SOLITARY child depends
                 on its parent. A group member carries none; the root carries none.
  group          "alternative" | "or" | null -- set on the feature whose CHILDREN
                 form a group. Those children are the group's members; a member
                 never carries a decomposition. A feature may be both a solitary
                 child (decomposition) and a group's parent (group) at once.

A constraint is ``{type: "requires" | "excludes", a, b}`` over two distinct
declared features. A configuration is a subset of feature ids; validity is a
separate question from structural validity -- see ``is_valid_configuration``.

Coverage rolls up this same tree: ``coverage_rollup`` folds per-feature evidence
into a covered/not-covered verdict, and ``coverage.rollup`` (the sibling module)
folds numeric obligation counts into the completeness vector.
"""

from __future__ import annotations

DECOMPOSITIONS = ("mandatory", "optional")
GROUP_KINDS = ("alternative", "or")
CONSTRAINT_TYPES = ("requires", "excludes")


def _child_map(model: dict) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    root = model["root"]
    for f in model["features"]:
        if f["id"] != root:
            children.setdefault(f["parent"], []).append(f["id"])
    return children


def is_member(idx: dict[str, dict], feature: dict, root: str) -> bool:
    """A feature is a group member iff its parent declares a group kind."""
    if feature["id"] == root:
        return False
    parent = idx.get(feature.get("parent"))
    return bool(parent) and parent.get("group") is not None


def validate(model: dict) -> None:
    """Reject any structurally impossible model; return None when it is a tree.

    Structural validity is not configuration validity: a valid model still admits
    invalid configurations, which ``is_valid_configuration`` judges separately.
    """
    if model.get("version") != 1:
        raise ValueError("feature model version must be 1")
    root = model.get("root")
    features = model.get("features")
    if not root or not isinstance(features, list) or not features:
        raise ValueError("a root feature and nonempty features are required")

    ids: list[str] = []
    for f in features:
        fid = f.get("id")
        if not isinstance(fid, str) or not fid:
            raise ValueError("every feature needs a non-empty string id")
        if not f.get("name"):
            raise ValueError(f"feature {fid} requires a name")
        ids.append(fid)
    seen: set[str] = set()
    for fid in ids:
        if fid in seen:
            raise ValueError(f"duplicate feature id: {fid}")
        seen.add(fid)
    known = set(ids)
    if root not in known:
        raise ValueError(f"root {root} is not a declared feature")

    idx = {f["id"]: f for f in features}
    for f in features:
        fid = f["id"]
        parent = f.get("parent")
        dec = f.get("decomposition")
        grp = f.get("group")
        if fid == root:
            if parent is not None:
                raise ValueError(f"root {root} must not declare a parent")
            if dec is not None:
                raise ValueError(f"root {root} must not declare a decomposition")
            if grp is not None:
                raise ValueError(f"root {root} must not declare a group")
            continue
        if parent is None:
            raise ValueError(f"feature {fid} has no parent but is not the root")
        if parent == fid:
            raise ValueError(f"feature {fid} is its own parent")
        if parent not in known:
            raise ValueError(f"feature {fid} references unknown parent {parent}")
        if dec is not None and dec not in DECOMPOSITIONS:
            raise ValueError(f"feature {fid} has an invalid decomposition: {dec}")
        if grp is not None and grp not in GROUP_KINDS:
            raise ValueError(f"feature {fid} has an invalid group: {grp}")

    children = _child_map(model)
    for f in features:
        fid = f["id"]
        if fid == root:
            continue
        member = is_member(idx, f, root)
        if member and f.get("decomposition") is not None:
            raise ValueError(f"group member {fid} must not declare a decomposition")
        if not member and f.get("decomposition") is None:
            raise ValueError(f"solitary feature {fid} requires a decomposition")
        grp = f.get("group")
        if grp is not None and not children.get(fid):
            raise ValueError(f"{grp} group with no members: {fid}")

    # Single-rooted and acyclic: every feature reaches the root by parent links.
    for f in features:
        if f["id"] == root:
            continue
        cur = f["id"]
        walked: set[str] = set()
        while cur != root:
            if cur in walked:
                raise ValueError(f"cycle in feature tree at {f['id']}")
            walked.add(cur)
            cur = idx[cur]["parent"]

    req_pairs: set[frozenset[str]] = set()
    exc_pairs: set[frozenset[str]] = set()
    for c in model.get("constraints", []):
        ctype = c.get("type")
        a, b = c.get("a"), c.get("b")
        if ctype not in CONSTRAINT_TYPES:
            raise ValueError(f"constraint has an invalid type: {ctype}")
        if a not in known:
            raise ValueError(f"constraint references unknown feature '{a}'")
        if b not in known:
            raise ValueError(f"constraint references unknown feature '{b}'")
        if a == b:
            raise ValueError(f"constraint relates feature {a} to itself")
        (req_pairs if ctype == "requires" else exc_pairs).add(frozenset((a, b)))
    for pair in req_pairs & exc_pairs:
        x, y = sorted(pair)
        raise ValueError(f"features {x} and {y} are declared both requires and excludes")


def is_valid_configuration(model: dict, selected: set[str]) -> tuple[bool, list[str]]:
    """Is ``selected`` a valid configuration of ``model``?

    Returns (ok, reasons). Valid iff: the root is selected; no feature is selected
    without its parent; every mandatory solitary feature under a selected parent is
    selected; each group under a selected parent meets its cardinality (alternative
    = exactly one, or = one-or-more); and every requires/excludes constraint holds.
    Optional features and which group member is chosen are the free choices.
    """
    validate(model)
    idx = {f["id"]: f for f in model["features"]}
    known = set(idx)
    root = model["root"]
    sel = set(selected)
    reasons: list[str] = []
    for unknown in sorted(sel - known):
        reasons.append(f"selected unknown feature: {unknown}")
    chosen_sel = sel & known
    children = _child_map(model)

    if root not in chosen_sel:
        reasons.append(f"root feature {root} is not selected")

    for fid in sorted(chosen_sel):
        if fid == root:
            continue
        parent = idx[fid]["parent"]
        if parent not in chosen_sel:
            reasons.append(f"feature {fid} selected without its parent {parent}")

    for f in model["features"]:
        fid = f["id"]
        if fid == root or is_member(idx, f, root):
            continue
        if f.get("decomposition") == "mandatory":
            parent = f["parent"]
            if parent in chosen_sel and fid not in chosen_sel:
                reasons.append(
                    f"mandatory feature {fid} is required when {parent} is selected"
                )

    for f in model["features"]:
        grp = f.get("group")
        gid = f["id"]
        if grp is None or gid not in chosen_sel:
            continue
        chosen = [m for m in children.get(gid, []) if m in chosen_sel]
        if grp == "alternative" and len(chosen) != 1:
            reasons.append(
                f"alternative group {gid} requires exactly one selected child, "
                f"got {len(chosen)}"
            )
        if grp == "or" and not chosen:
            reasons.append(f"or group {gid} requires at least one selected child, got 0")

    for c in model.get("constraints", []):
        a, b = c["a"], c["b"]
        if c["type"] == "requires" and a in chosen_sel and b not in chosen_sel:
            reasons.append(f"feature {a} requires {b}")
        if c["type"] == "excludes" and a in chosen_sel and b in chosen_sel:
            reasons.append(f"feature {a} excludes {b}")

    return (not reasons), reasons


def coverage_rollup(model: dict, selected: set[str], covered: set[str]) -> dict:
    """Binary coverage roll-up: is every selected feature proven, all the way up?

    ``selected`` is the configuration; ``covered`` is the set of feature ids with
    proven evidence. A feature is
    ``rolled_covered`` iff it is itself covered and every selected child is
    rolled_covered, so a single uncovered leaf propagates a gap to the root. The
    numeric completeness vector is the sibling ``coverage.rollup``; this is the
    yes/no view the CLI and gate consume. Refuses an invalid configuration --
    coverage of a product that cannot be built is meaningless.
    """
    ok, reasons = is_valid_configuration(model, selected)
    if not ok:
        raise ValueError("invalid configuration: " + "; ".join(reasons))
    idx = {f["id"]: f for f in model["features"]}
    root = model["root"]
    sel = set(selected)
    cov = set(covered)
    children = _child_map(model)

    rolled: dict[str, bool] = {}

    def compute(fid: str) -> bool:
        if fid in rolled:
            return rolled[fid]
        result = fid in cov
        for child in children.get(fid, []):
            if child in sel and not compute(child):
                result = False
        rolled[fid] = result
        return result

    for fid in sel:
        compute(fid)

    uncovered = sorted(fid for fid in sel if fid not in cov)
    rows = [
        {
            "id": fid,
            "name": idx[fid].get("name"),
            "covered": fid in cov,
            "rolled_covered": rolled[fid],
        }
        for fid in sorted(sel)
    ]
    return {
        "complete": not uncovered,
        "uncovered": uncovered,
        "required": sorted(sel),
        "rows": rows,
    }


def example() -> dict:
    """Kang et al.'s canonical worked example, as capcov consumes it.

    Authentication(root) -> Password(mandatory), MFA(optional). Password carries an
    optional SecondFactor, whose children form an alternative group
    { SMS | AuthenticatorApp | Passkey }; Passkey excludes SMS.
    """
    return {
        "version": 1,
        "root": "auth",
        "features": [
            {"id": "auth", "name": "Authentication", "parent": None,
             "decomposition": None, "group": None},
            {"id": "password", "name": "Password", "parent": "auth",
             "decomposition": "mandatory", "group": None},
            {"id": "mfa", "name": "Multi-Factor Authentication", "parent": "auth",
             "decomposition": "optional", "group": None},
            {"id": "secondFactor", "name": "Second Factor", "parent": "password",
             "decomposition": "optional", "group": "alternative"},
            {"id": "sms", "name": "SMS Code", "parent": "secondFactor",
             "decomposition": None, "group": None},
            {"id": "authenticatorApp", "name": "Authenticator App",
             "parent": "secondFactor", "decomposition": None, "group": None},
            {"id": "passkey", "name": "Passkey", "parent": "secondFactor",
             "decomposition": None, "group": None},
        ],
        "constraints": [
            {"type": "excludes", "a": "passkey", "b": "sms"},
        ],
    }
