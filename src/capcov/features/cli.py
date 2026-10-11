"""capcov features: FODA feature model, configuration validity, and coverage.

    capcov features example                     emit the canonical model as JSON
    capcov features validate  model.json        structural validity + the tree
    capcov features check     model.json --select a,b,c
                                                is a configuration valid?
    capcov features rollup    model.json --select a,b,c --covered a,b
                                                binary: does coverage roll up whole?
    capcov features coverage  model.json obligations.json [--selected a,b,c]
                                                numeric: the completeness vector
    capcov features map       model.json mapping.json capabilities.json
                              [--coverage coverage.json] --out obligations.json
                              [--report report.json]
                                                surfaces -> feature obligations
    capcov features report    model.json obligations.json [--selected a,b,c]
                              [--format md|csv] [--out FILE]
                                                completeness vector as Harvey glyphs

``check`` and ``rollup`` take comma-separated feature ids. ``rollup`` is the yes/no
gate view (``model.coverage_rollup``); ``coverage`` is the numeric completeness
vector (``coverage.rollup``) that keeps the mandatory skeleton, the selected
optionals, and the unassessed choices in separate denominators.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import coverage as coverage_mod
from . import mapping as mapping_mod
from . import model as model_mod
from . import evidence as evidence_mod
from . import report as report_mod


def _ids(value: str) -> set[str]:
    return {token for token in value.split(",") if token}


def _children(model: dict) -> dict[str, list[str]]:
    root = model["root"]
    kids: dict[str, list[str]] = {}
    for f in model["features"]:
        if f["id"] != root:
            kids.setdefault(f["parent"], []).append(f["id"])
    for group in kids.values():
        group.sort()
    return kids


def _annotation(idx: dict[str, dict], feature: dict, root: str) -> str:
    """One-line facet label for a feature: how it hangs off its parent."""
    if feature["id"] == root:
        return "root"
    parts: list[str] = []
    if model_mod.is_member(idx, feature, root):
        parts.append("group member")
    dec = feature.get("decomposition")
    if dec:
        parts.append(dec)
    grp = feature.get("group")
    if grp:
        parts.append(f"{grp} group")
    return ", ".join(parts)


def _print_tree(model: dict) -> None:
    """Render the feature tree, each node annotated with its facet."""
    idx = {f["id"]: f for f in model["features"]}
    root = model["root"]
    kids = _children(model)

    def walk(fid: str, depth: int) -> None:
        feature = idx[fid]
        annotation = _annotation(idx, feature, root)
        suffix = f" ({annotation})" if annotation else ""
        print(f"{'  ' * depth}{feature.get('name', fid)} [{fid}]{suffix}")
        for child in kids.get(fid, []):
            walk(child, depth + 1)

    walk(root, 0)


def _print_vector(result: dict) -> None:
    """Print the completeness vector: the four denominators, then the tree rows."""
    print(
        "capcov features coverage: "
        f"mandatory {result['mandatory_covered']}/{result['mandatory_total']}, "
        f"optional {result['optional_covered']}/{result['optional_total']} "
        f"({result['optional_assessed']} assessed, "
        f"{result['optional_unassessed']} unassessed)"
    )
    rows = result["tree_rows"]
    wfeat = max((len(r["feature"]) for r in rows), default=len("feature"))
    wstat = max((len(r["status"]) for r in rows), default=len("status"))
    wkind = max((len(str(r["kind"])) for r in rows), default=len("kind"))
    header = (
        f"  {'feature'.ljust(wfeat)}  {'status'.ljust(wstat)}  "
        f"{'kind'.ljust(wkind)}  self   rolled"
    )
    print(header)
    for r in rows:
        own = f"{r['self_covered']}/{r['self_total']}"
        rolled = f"{r['covered']}/{r['total']}"
        print(
            f"  {r['feature'].ljust(wfeat)}  {r['status'].ljust(wstat)}  "
            f"{str(r['kind']).ljust(wkind)}  {own.ljust(5)}  {rolled}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov features")
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("example", help="emit the canonical Authentication model as JSON")
    ex.add_argument("--out", help="write here instead of stdout")

    va = sub.add_parser("validate", help="structural validity of a feature model, then its tree")
    va.add_argument("model")

    ch = sub.add_parser("check", help="is a configuration valid against the model?")
    ch.add_argument("model")
    ch.add_argument("--select", required=True, help="comma-separated selected feature ids")

    ro = sub.add_parser("rollup", help="binary: does coverage roll up to complete?")
    ro.add_argument("model")
    ro.add_argument("--select", required=True, help="comma-separated selected feature ids")
    ro.add_argument("--covered", required=True, help="comma-separated proven feature ids")

    co = sub.add_parser("coverage", help="numeric completeness vector rolled up the tree")
    co.add_argument("model")
    co.add_argument("obligations", help="JSON map: feature id -> {covered, total}")
    co.add_argument(
        "--selected",
        default=None,
        help="comma-separated configuration; omit to assess the mandatory skeleton alone",
    )

    mp = sub.add_parser("map", help="project discovered surfaces onto features")
    mp.add_argument("model")
    mp.add_argument("mapping", help="JSON: {version: 1, features: {id: {surfaces: [glob], tags: [tag]}}}")
    mp.add_argument("capabilities", help="capcov discover artifact (capabilities.json)")
    mp.add_argument("--coverage", default=None, help="capcov reconcile artifact; without it nothing is covered")
    mp.add_argument("--out", required=True, help="write the {id: {covered, total}} obligations map here")
    mp.add_argument("--report", default=None, help="write the full projection (unassigned, contested, assurance) here")

    rp = sub.add_parser("report", help="the completeness vector as a Markdown or CSV table with Harvey glyphs")
    rp.add_argument("model")
    rp.add_argument("obligations")
    rp.add_argument(
        "--selected",
        default=None,
        help="comma-separated configuration; omit to assess the mandatory skeleton alone",
    )
    rp.add_argument("--format", default="md", choices=["md", "csv"])
    rp.add_argument("--out", default=None)
    rec = sub.add_parser("reconcile", help="derive feature coverage from native outcome evidence")
    rec.add_argument("model")
    rec.add_argument("--outcomes-map", required=True)
    rec.add_argument("--inventory", required=True)
    rec.add_argument("--run")
    rec.add_argument("--target", default=".")
    rec.add_argument("--selected", required=True)
    rec.add_argument("--out", required=True)
    rec.add_argument("--report-features", help="comma-separated non-overlapping selected frontier")
    rec.add_argument("--flow-model")
    rec.add_argument("--flow-plan")
    rec.add_argument("--flow-inventory")
    rec.add_argument("--flow-run")

    args = parser.parse_args(argv)
    try:
        if args.command == "example":
            text = json.dumps(model_mod.example(), indent=2, sort_keys=True) + "\n"
            if args.out:
                Path(args.out).write_text(text)
                print(f"capcov features: wrote {args.out}")
            else:
                sys.stdout.write(text)
            return 0

        model = json.loads(Path(args.model).read_text())

        if args.command == "reconcile":
            flow_paths = [args.flow_model, args.flow_plan, args.flow_inventory, args.flow_run]
            if any(flow_paths) and not all(flow_paths):
                raise ValueError("flow evidence requires model, plan, inventory, and run together")
            if not args.run and not all(flow_paths):
                raise ValueError("provide a native pytest run, a raw flow chain, or both")
            load = lambda path: json.loads(Path(path).read_text())
            result = evidence_mod.reconcile(
                model, load(args.outcomes_map), load(args.inventory), load(args.run) if args.run else None,
                Path(args.target).resolve(), _ids(args.selected),
                flow_inputs=tuple(load(p) for p in flow_paths) if all(flow_paths) else None,
                report_features=_ids(args.report_features) if args.report_features else None,
            )
            Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            print(f"capcov features: complete={result['complete']}; wrote {args.out}")
            return 0 if result["complete"] else 1

        if args.command == "validate":
            model_mod.validate(model)
            print(f"capcov features: {args.model} is a valid feature model")
            _print_tree(model)
            return 0

        if args.command == "check":
            ok, reasons = model_mod.is_valid_configuration(model, _ids(args.select))
            if ok:
                print("capcov features: configuration is valid")
                return 0
            print(f"capcov features: configuration is INVALID ({len(reasons)})")
            for reason in reasons:
                print(f"  {reason}")
            return 1

        if args.command == "coverage":
            obligations = json.loads(Path(args.obligations).read_text())
            selected = None if args.selected is None else _ids(args.selected)
            result = coverage_mod.rollup(model, obligations, selected)
            _print_vector(result)
            return 0

        if args.command == "report":
            obligations = json.loads(Path(args.obligations).read_text())
            selected = None if args.selected is None else _ids(args.selected)
            text = report_mod.render(coverage_mod.rollup(model, obligations, selected), fmt=args.format)
            if args.out:
                Path(args.out).write_text(text)
                print(f"capcov features: wrote {args.out}")
            else:
                sys.stdout.write(text)
            return 0

        if args.command == "map":
            mapping = json.loads(Path(args.mapping).read_text())
            capabilities = json.loads(Path(args.capabilities).read_text())
            coverage = None if args.coverage is None else json.loads(Path(args.coverage).read_text())
            result = mapping_mod.project(model, mapping, capabilities, coverage)
            Path(args.out).write_text(json.dumps(result["obligations"], indent=2, sort_keys=True) + "\n")
            if args.report:
                Path(args.report).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            print(
                "capcov features map: "
                f"{result['surfaces_total']} surfaces, {result['assigned']} assigned to "
                f"{len(result['obligations'])} features, {len(result['unassigned'])} unassigned, "
                f"{len(result['contested'])} contested; assurance {result['assurance']}, "
                f"{result['exercised']} exercised; "
                f"{len(result['unmapped_features'])} unmapped features, "
                f"{len(result['empty_rules'])} empty rules; "
                f"discovery excluded {result['excluded_surfaces']}, unresolved {result['unresolved']}"
            )
            return 0

        result = model_mod.coverage_rollup(model, _ids(args.select), _ids(args.covered))
        print(
            f"capcov features: complete={result['complete']}; "
            f"uncovered={result['uncovered']}"
        )
        return 0 if result["complete"] else 1
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"capcov features: {exc}")
        return 1
