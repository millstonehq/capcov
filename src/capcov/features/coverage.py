"""Roll obligation coverage up a FODA feature tree without collapsing it to a number.

capcov's rule is that completeness is a vector: a single percentage hides which
half is missing. Feature optionality sharpens the point. A mandatory feature is
part of the product whenever its parent is, so its obligation gaps are real gaps
and belong in the denominator. An optional feature nobody selected is not a gap --
it is a choice not taken, and folding its (absent) coverage into the score would
either flatter or punish a decision that was never made. An alternative or or
group contributes only the member(s) actually chosen; the branches not taken are
neither covered nor owed.

Where ``model.coverage_rollup`` gives the binary "is every selected feature
proven" verdict, this gives the numeric vector and keeps four things apart:

  mandatory_covered / mandatory_total   the required skeleton -- root, mandatory
                                        features under a present parent, and the
                                        chosen member of each present group;
  optional_covered / optional_total     obligations of optional features actually
                                        selected (optional_assessed counts them);
  optional_unassessed                   optionals and group choices left open;
  tree_rows                             every feature, its status, its rolled pair.

``selected`` is the configuration under assessment. ``selected=None`` means "the
mandatory skeleton, every optional and every group choice still open": mandatory
features are assessed, optionals and group members are reported as unassessed --
never silently covered, never silently failed. A group member is required once
chosen (the group forces the choice), so a chosen branch lands in the mandatory
denominator while the branches beside it are excluded entirely.
"""

from __future__ import annotations

from . import model as model_mod

_PRESENT = ("required", "selected")


def _obligation(feature_obligations: dict, fid: str) -> tuple[int, int]:
    entry = feature_obligations.get(fid, {"covered": 0, "total": 0})
    covered, total = entry.get("covered", 0), entry.get("total", 0)
    if (
        type(covered) is not int
        or type(total) is not int
        or covered < 0
        or total < 0
        or covered > total
    ):
        raise ValueError(
            f"{fid}: obligation coverage must be integers with 0 <= covered <= total"
        )
    return covered, total


def _resolve_status(model: dict, selected: set[str] | None) -> dict[str, str]:
    """Assign each feature exactly one status, top-down from the root.

    required    present, not by choice: the root; a mandatory solitary feature
                under a present parent; a chosen member of a present group's parent.
    selected    an optional solitary feature the configuration explicitly included.
    unassessed  ``selected`` is None and the decision was deferred (every optional,
                every group choice), or an ancestor is itself unassessed.
    deselected  a decision was made to leave it out (a provided selection), or an
                ancestor is out.
    """
    idx = {f["id"]: f for f in model["features"]}
    root = model["root"]
    children: dict[str, list[dict]] = {}
    for f in model["features"]:
        if f["id"] != root:
            children.setdefault(f["parent"], []).append(f)

    open_mode = selected is None
    sel: set[str] = set() if open_mode else set(selected)
    status: dict[str, str] = {root: "required"}

    def walk(fid: str) -> None:
        parent_present = status[fid] in _PRESENT
        parent_is_group = idx[fid].get("group") is not None
        for f in sorted(children.get(fid, []), key=lambda x: x["id"]):
            child = f["id"]
            if not parent_present:
                status[child] = status[fid]
            elif parent_is_group:
                # A group member: presence is a choice the group forces.
                status[child] = (
                    "unassessed" if open_mode
                    else "required" if child in sel
                    else "deselected"
                )
            elif f.get("decomposition") == "mandatory":
                status[child] = "required"
            else:  # solitary optional
                status[child] = (
                    "unassessed" if open_mode
                    else "selected" if child in sel
                    else "deselected"
                )
            walk(child)

    walk(root)
    missing = set(idx) - set(status)
    if missing:
        raise ValueError(f"features unreachable from the root: {sorted(missing)}")
    return status


def rollup(
    model: dict,
    feature_obligations: dict,
    selected: set[str] | None = None,
) -> dict:
    """Per-feature and tree-level coverage that never collapses to one number.

    ``feature_obligations`` maps a feature id to its OWN obligation coverage as
    ``{"covered": int, "total": int}``; a feature absent from the map owns none.
    Returns the coverage vector documented in this module's docstring.
    """
    model_mod.validate(model)
    idx = {f["id"]: f for f in model["features"]}
    root = model["root"]

    unknown = set(feature_obligations) - set(idx)
    if unknown:
        raise ValueError(f"obligations for unknown features: {sorted(unknown)}")
    if selected is not None:
        stray = set(selected) - set(idx)
        if stray:
            raise ValueError(f"selection includes unknown features: {sorted(stray)}")

    status = _resolve_status(model, selected)

    self_cov: dict[str, int] = {}
    self_tot: dict[str, int] = {}
    for fid in idx:
        self_cov[fid], self_tot[fid] = _obligation(feature_obligations, fid)

    children: dict[str, list[str]] = {}
    for f in model["features"]:
        if f["id"] != root:
            children.setdefault(f["parent"], []).append(f["id"])
    for kids in children.values():
        kids.sort()

    roll_cov: dict[str, int] = {}
    roll_tot: dict[str, int] = {}

    def roll(fid: str) -> None:
        cov, tot = self_cov[fid], self_tot[fid]
        for child in children.get(fid, []):
            roll(child)
            if status[child] in _PRESENT:
                cov += roll_cov[child]
                tot += roll_tot[child]
        roll_cov[fid], roll_tot[fid] = cov, tot

    roll(root)

    order: list[str] = []

    def preorder(fid: str) -> None:
        order.append(fid)
        for child in children.get(fid, []):
            preorder(child)

    preorder(root)

    def kind_of(f: dict) -> str:
        if f["id"] == root:
            return "root"
        if model_mod.is_member(idx, f, root):
            return "group-member"
        return f.get("decomposition")

    mandatory_covered = mandatory_total = 0
    optional_covered = optional_total = 0
    optional_assessed = optional_unassessed = 0
    rows = []
    for fid in order:
        f = idx[fid]
        st = status[fid]
        if st == "required":
            mandatory_covered += self_cov[fid]
            mandatory_total += self_tot[fid]
        elif st == "selected":
            optional_covered += self_cov[fid]
            optional_total += self_tot[fid]
            optional_assessed += 1
        elif st == "unassessed":
            optional_unassessed += 1
        rows.append(
            {
                "feature": fid,
                "name": f.get("name"),
                "parent": f.get("parent"),
                "kind": kind_of(f),
                "status": st,
                "self_covered": self_cov[fid],
                "self_total": self_tot[fid],
                "covered": roll_cov[fid],
                "total": roll_tot[fid],
            }
        )

    return {
        "version": 1,
        "root": root,
        "selected": None if selected is None else sorted(set(selected)),
        "mandatory_covered": mandatory_covered,
        "mandatory_total": mandatory_total,
        "optional_covered": optional_covered,
        "optional_total": optional_total,
        "optional_assessed": optional_assessed,
        "optional_unassessed": optional_unassessed,
        "tree_rows": rows,
    }
