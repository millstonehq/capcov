"""capcov command line: discover, observe, reconcile, gate, report.

`discover` and `observe` both always run. There is no static-only mode and no
threshold that switches between them -- that was a rejected design, and what it
amounted to was a tool that sometimes does not work with the answer bolted on
beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import artifacts
from .adapters import load as load_adapter
from .core import fixpoint
from .core import gate as gate_mod
from .core import reconcile as reconcile_mod


def _resolve(target: Path, source: str | None, adapter: str | None) -> tuple[Path, str]:
    config = {}
    config_path = target / "capcov.toml"
    if config_path.exists():
        import tomllib

        config = tomllib.loads(config_path.read_text()).get("capcov", {})
    source_dir = target / (source or config.get("source", "src"))
    adapter_name = adapter or config.get("adapter")
    if not adapter_name:
        raise SystemExit(
            f"no adapter: pass --adapter or set [capcov] adapter in {config_path}"
        )
    if not source_dir.is_dir():
        raise SystemExit(f"source root {source_dir} does not exist")
    return source_dir, adapter_name


def _emit(path: Path, doc: dict, check: bool) -> int:
    """Write, or in --check mode re-render and diff against what is on disk."""
    if not check:
        artifacts.write(path, doc.pop("kind"), doc.pop("derived_from"), doc)
        return 0
    if not path.exists():
        print(f"capcov: {path} does not exist; run without --check to create it")
        return 1
    on_disk = json.loads(path.read_text())
    fresh = {"schema_version": artifacts.SCHEMA_VERSION, **doc}
    if artifacts.normalise(on_disk) == artifacts.normalise(fresh):
        return 0
    print(
        f"capcov: {path} is stale. What this system can do has changed and the "
        "committed capability set has not. Re-run without --check and read the "
        "diff -- it is the list of capabilities this change adds or removes."
    )
    _diff(artifacts.normalise(on_disk), artifacts.normalise(fresh), str(path))
    return 1


def _diff(old: str, new: str, label: str) -> None:
    import difflib

    for line in list(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"{label} (on disk)",
            f"{label} (re-derived)",
        )
    )[:80]:
        sys.stdout.write(line)


# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    source_dir, adapter_name = _resolve(target, args.source, args.adapter)
    adapter = load_adapter(adapter_name)

    raw = adapter.discover(source_dir, target, name_match=not args.no_name_match)
    # Optional hybrid: rent SCIP as the resolver for the call graph, keep the AST
    # pass for entities, surfaces and the enumerated blind spots.
    # The AST adapter always runs first -- it is the fallback and the enumerator;
    # SCIP only re-sources `_calls` and adds its resolved-edges + residue.
    if getattr(args, "resolver", "ast") == "scip":
        from .scip import resolve as scip_resolve

        language = getattr(adapter, "LANGUAGE", "python")
        try:
            raw = scip_resolve.resolve(source_dir, raw, language=language)
        except scip_resolve.ScipToolsUnavailable as exc:
            raise SystemExit(f"capcov discover --resolver scip: {exc}")
    direct, calls, ops = raw["_direct"], raw["_calls"], raw["_ops"]

    roots = [s["handler"] for s in raw["surfaces"]]
    known = set(direct) | set(calls)
    missing_roots = sorted(r for r in roots if r not in known)
    per_root, history = fixpoint.bind(roots, calls, direct)

    capabilities = []
    for surface in raw["surfaces"]:
        bound = per_root.get(surface["handler"], {})
        reach = fixpoint.distances(surface["handler"], calls)
        for entity, hops in sorted(bound.items()):
            observed_ops = set()
            for node in reach:
                observed_ops |= ops.get(node, {}).get(entity, set())
            path = fixpoint.chain(surface["handler"], entity, calls, direct)
            capabilities.append(
                {
                    "entity": entity,
                    "surface": surface["id"],
                    "operations": sorted(observed_ops),
                    "evidence": {
                        "hops": hops,
                        "chain": path,
                        "kind": "direct" if hops == 0 else f"call-chain:{hops}",
                    },
                }
            )
    capabilities.sort(key=lambda c: (c["entity"], c["surface"]))

    tree_hash, files = artifacts.tree_sha256(source_dir)
    blind = [b for b in raw["blind_spots"] if b["blind"]]
    doc = {
        "kind": "capabilities",
        "derived_from": artifacts.provenance(
            str(source_dir.relative_to(target)), tree_hash,
            f"capcov {adapter.NAME}", files,
        ),
        "entities": raw["entities"],
        "surfaces": raw["surfaces"],
        "capabilities": capabilities,
        "binding_history": history,
        "blind_spots": blind,
        "resolved_dynamic_access": [
            b for b in raw["blind_spots"] if not b["blind"]
        ],
        "residue": raw["residue"],
        "residue_summary": raw["residue_summary"],
        "unbound_entry_points": missing_roots,
    }
    # When SCIP resolved the call graph, carry both halves of the hybrid into the
    # artifact: the edges SCIP resolved AND the residue it stayed silent about,
    # so coverage never reports a bare number -- resolved-by-SCIP plus
    # unresolved-enumerated, each named with a file and a line.
    if raw.get("resolver") == "scip":
        doc["resolver"] = "scip"
        doc["scip_resolved_edges"] = raw["scip_resolved_edges"]
        doc["scip_entities"] = raw["scip_entities"]
        doc["scip_residue"] = raw["scip_residue"]
        doc["scip_residue_summary"] = raw["scip_residue_summary"]
    rc = _emit(Path(args.out), dict(doc), args.check)
    if not args.quiet:
        bound_entities = {c["entity"] for c in capabilities}
        print(
            f"capcov discover: {len(raw['entities'])} entities, "
            f"{len(raw['surfaces'])} surfaces, {len(bound_entities)} entities bound, "
            f"history {' -> '.join(map(str, history))}, "
            f"{len(blind)} blind spots"
        )
        res = raw["residue_summary"]
        print(
            f"capcov discover: call edges {res['resolved_by_import']} by import, "
            f"{res['resolved_by_name']} by name; residue {res['ambiguous']} ambiguous "
            f"({res['external']} external, {res['chained']} chained, "
            f"{res['builtin_shadowed']} builtin-shadowed; none of these are residue)"
        )
        if missing_roots:
            print(
                "capcov discover: entry points with no analysed body: "
                + ", ".join(missing_roots)
            )
        if raw.get("resolver") == "scip":
            rs = raw["scip_residue_summary"]
            print(
                f"capcov discover: resolver scip -- {rs['scip_resolved_edges']} "
                f"edges resolved ({rs['scip_rooted_edges']} rooted in-project); "
                f"{rs['unresolved_enumerated']} of {rs['ast_call_sites']} call "
                "sites enumerated as unresolved (named, not dropped)"
            )
    return rc


def cmd_observe(args: argparse.Namespace) -> int:
    """Run the target's own exercises with the probe installed.

    The probe writes observed.json itself, in-process. capcov does not talk to
    the system under test; it sets three environment variables and runs the
    command the target already runs.
    """
    target = Path(args.target).resolve()
    source_dir, _ = _resolve(target, args.source, args.adapter)
    env = dict(os.environ)
    env["CAPCOV_OBSERVE"] = "1"
    env["CAPCOV_OUT"] = str(Path(args.out).resolve())
    env["CAPCOV_SOURCE_ROOT"] = str(source_dir)
    env["CAPCOV_TARGET"] = str(target)
    if not args.command:
        raise SystemExit("capcov observe: give the command after --, e.g. -- pytest -q")
    print(f"capcov observe: {' '.join(args.command)}")
    proc = subprocess.run(args.command, cwd=target, env=env)
    if proc.returncode != 0:
        print(
            f"capcov observe: the exercise failed (exit {proc.returncode}). "
            "Observation from a failing run is not evidence of anything; fix the "
            "run first."
        )
        return proc.returncode
    if not Path(args.out).exists():
        print(
            f"capcov observe: the command succeeded and wrote no {args.out}. "
            "The probe did not load -- check that capcov is installed in the "
            "same environment as the exercise."
        )
        return 1
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    capabilities = artifacts.read(Path(args.capabilities), "capabilities")
    observed = artifacts.read(Path(args.observed), "observed")
    same, why = artifacts.same_artifact(capabilities, observed)
    if not same and not args.allow_drift:
        raise SystemExit(f"capcov reconcile: {why}")

    result = reconcile_mod.reconcile(capabilities, observed)
    doc = {
        "kind": "coverage",
        "derived_from": {
            **capabilities["derived_from"],
            "extractor": "capcov reconcile",
            "static_from": capabilities["derived_from"]["extractor"],
            "runtime_from": observed["derived_from"]["extractor"],
        },
        **result,
        "blind_spots": capabilities.get("blind_spots", []),
    }
    # Carry the hybrid's SCIP evidence through the reconcile so coverage reports
    # resolved-by-SCIP AND unresolved-enumerated -- both survive to the gate and
    # the report, and neither is collapsed into a single coverage figure. Absent
    # for an AST-resolved run, so this is backward compatible.
    for key in ("resolver", "scip_resolved_edges", "scip_residue", "scip_residue_summary"):
        if key in capabilities:
            doc[key] = capabilities[key]
    rc = _emit(Path(args.out), dict(doc), args.check)
    if not args.quiet:
        s = result["summary"]
        print(
            "capcov reconcile: "
            + ", ".join(f"{k} {s[k]}" for k in reconcile_mod.CELLS)
        )
        rs = capabilities.get("scip_residue_summary")
        if rs is not None:
            print(
                f"capcov reconcile: scip resolver -- {rs['scip_resolved_edges']} "
                f"edges resolved, {rs['unresolved_enumerated']} call sites "
                "unresolved-enumerated (each named in scip_residue)"
            )
    return rc


def cmd_gate(args: argparse.Namespace) -> int:
    coverage = artifacts.read(Path(args.coverage), "coverage")
    exemptions = Path(args.exemptions) if args.exemptions else None
    failures = gate_mod.gate(coverage, exemptions)
    if not failures:
        s = coverage["summary"]
        print(
            f"capcov gate: PASS -- {s['both']} capabilities covered, "
            f"{sum(s[c] for c in reconcile_mod.FAILING_CELLS)} exempted with reasons"
        )
        return 0
    print(f"capcov gate: FAIL -- {len(failures)} unexplained\n")
    for failure in failures:
        print(f"  {failure}\n")
    where = args.exemptions or "an exemptions file (--exemptions)"
    print(
        "Each of these is a question with an answer: write the test, fix the "
        f"adapter, delete the dead entity, or record why in {where}."
    )
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    coverage = artifacts.read(Path(args.coverage), "coverage")
    width = max(len(r["entity"]) for r in coverage["rows"])
    print(f"{'entity'.ljust(width)}  cell          tests  surfaces  operations")
    for row in coverage["rows"]:
        print(
            f"{row['entity'].ljust(width)}  {row['cell'].ljust(12)}  "
            f"{len(row['tests']):>5}  "
            f"{len(row['static_surfaces'] or row['runtime_surfaces']):>8}  "
            f"{','.join(row['runtime_operations'] or row['static_operations'])}"
        )
    s = coverage["summary"]
    print("\n" + ", ".join(f"{k}: {s[k]}" for k in reconcile_mod.CELLS))
    unexercised = coverage.get("unexercised_surfaces", [])
    if unexercised:
        print(f"\nsurfaces no exercise reached ({len(unexercised)}):")
        for surface in unexercised:
            print(f"  {surface}")
    if coverage["blind_spots"]:
        print(f"\nstatic blind spots ({len(coverage['blind_spots'])}):")
        for spot in coverage["blind_spots"]:
            print(f"  {spot['file']}:{spot['line']}  {spot['expr']}  ({spot['kind']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    actual = sys.argv[1:] if argv is None else argv
    if actual and actual[0] == "outcomes":
        from .outcomes import main as outcomes_main

        return outcomes_main(actual[1:])
    if actual and actual[0] == "flows":
        from .flows.cli import main as flows_main

        return flows_main(actual[1:])
    if actual and actual[0] == "features":
        from .features.cli import main as features_main

        return features_main(actual[1:])
    parser = argparse.ArgumentParser(prog="capcov", description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("flows", help="source obligations, flow planning, and browser outcome evidence")
    sub.add_parser("outcomes", help="scoped behavior obligations and fresh pytest evidence")
    sub.add_parser("features", help="FODA feature-model validation, configuration check, coverage rollup")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--target", default=".", help="the project root")
        p.add_argument("--source", default=None, help="source root under --target")
        p.add_argument("--adapter", default=None)
        p.add_argument("--quiet", action="store_true")

    d = sub.add_parser("discover", help="static: entities, surfaces, bindings")
    common(d)
    d.add_argument("--out", default="capabilities.json")
    d.add_argument("--check", action="store_true", help="re-derive and diff")
    d.add_argument(
        "--no-name-match",
        action="store_true",
        help="resolve calls through imports only; report every name-match as residue",
    )
    d.add_argument(
        "--resolver",
        choices=("ast", "scip"),
        default="ast",
        help="call-graph resolver: 'ast' (default, hand-rolled, stdlib-only) or "
        "'scip' (type-aware cross-file; needs a SCIP indexer + the scip CLI). "
        "The AST pass runs either way and stays the blind-spot enumerator.",
    )
    d.set_defaults(func=cmd_discover)

    o = sub.add_parser("observe", help="runtime: run the exercises with the probe")
    common(o)
    o.add_argument("--out", default="observed.json")
    o.add_argument("command", nargs=argparse.REMAINDER)
    o.set_defaults(func=cmd_observe)

    r = sub.add_parser("reconcile", help="the four-way diff")
    r.add_argument("capabilities")
    r.add_argument("observed")
    r.add_argument("--out", default="coverage.json")
    r.add_argument("--check", action="store_true")
    r.add_argument("--quiet", action="store_true")
    r.add_argument(
        "--allow-drift",
        action="store_true",
        help="compare artifacts derived from different trees (it is not a finding)",
    )
    r.set_defaults(func=cmd_reconcile)

    g = sub.add_parser("gate", help="fail on anything unexplained")
    g.add_argument("coverage")
    g.add_argument("--exemptions", default=None)
    g.add_argument("--quiet", action="store_true")
    g.set_defaults(func=cmd_gate)

    p = sub.add_parser("report", help="human-readable coverage table")
    p.add_argument("coverage")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if args.command if hasattr(args, "command") else False:
        args.command = [a for a in args.command if a != "--"]
    return args.func(args)
