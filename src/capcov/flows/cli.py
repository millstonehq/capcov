"""capcov flows: inventory -> model -> plan -> browser evidence -> coverage.

``discover``, ``catalog``, and ``plan`` run on the shared discovery/planning
engine the promoted core adapters and the browser probe wrap; they are NOT
deprecated. ``run``, ``coverage``, and ``gate`` are DEPRECATION SHIMS: they still
resolve and return (so consumers pinned to the flows pipeline keep working until
they bump) but the unified engine supersedes them --
``capcov observe --probe browser`` -> ``capcov reconcile`` -> ``capcov gate``
drives the same planner+runner and reads the four cells.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from .catalog import build_catalog, render_catalog
from .discovery import discover
from .model import digest, plan, reconcile

# The three retired verbs and the one-line notice each prints. The notice points
# at the unified observe(--probe browser) -> reconcile -> gate path; the flows verb
# still does its work so the suite (and pinned consumers) stay green.
_SHIM_NOTICE = {
    "run": "`flows run` is superseded by `capcov observe --probe browser`, which drives "
    "the same planner+runner and emits the four-cell observed artifact.",
    "coverage": "`flows coverage` is superseded by `capcov reconcile` on the four-cell "
    "engine, fed by `capcov observe --probe browser`.",
    "gate": "the flows freeform baseline gate is superseded by `capcov gate` with a "
    "core exemptions.toml (cell-typed, dated); see the migration note in this module.",
}


def _deprecated(verb: str) -> None:
    print(f"capcov flows: DEPRECATED -- {_SHIM_NOTICE[verb]}")


def read(path: str) -> dict:
    return json.loads(Path(path).read_text())


def emit(path: str, value: dict, check: bool = False) -> int:
    if check:
        if not Path(path).exists() or read(path) != value:
            print(f"capcov flows: stale artifact {path}; regenerate and review")
            return 1
        return 0
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov flows")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover")
    d.add_argument("config")
    d.add_argument("--out", required=True)
    d.add_argument("--check", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("model")
    p.add_argument("--target", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--check", action="store_true")
    p.add_argument("--max-states", type=int, default=10000,
                   help="positive reachable-state budget; exhaustion fails without a complete plan")
    catalog = sub.add_parser(
        "catalog", help="derive flow families and source dependency candidates"
    )
    catalog.add_argument("inventory")
    catalog.add_argument("--out", required=True)
    catalog.add_argument("--check", action="store_true")
    catalog.add_argument("--report")
    catalog.add_argument("--focus", help="limit the human report, never the inventory denominator")
    catalog.add_argument(
        "--require-declarations",
        action="store_true",
        help="fail on unclassified declarations or unconsumed source files",
    )
    catalog.add_argument("--quiet", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("plan")
    r.add_argument("--inventory", required=True)
    r.add_argument("--config", help="legacy flow discovery config; omit for a unified capability inventory")
    r.add_argument("--out", required=True)
    r.add_argument("--timeout", type=int, default=180)
    r.add_argument("--outcomes-map", help="bind this run to outcome evidence inputs")
    r.add_argument("--capability-inventory", help="inventory used by --outcomes-map")
    r.add_argument("--evidence-target", default=".", help="root for outcome input provenance")
    r.add_argument(
        "--only",
        metavar="TRANSITION_OR_SCENARIO_ID",
        help="inner loop: run just this transition/scenario, passed to the runner "
        "as CAPCOV_FLOW_ONLY; omit to execute the whole plan",
    )
    # Parse runner argv after -- separately, so options may follow the plan.
    c = sub.add_parser("coverage")
    c.add_argument("inventory")
    c.add_argument("model")
    c.add_argument("plan")
    c.add_argument("--run")
    c.add_argument("--out", required=True)
    c.add_argument(
        "--only",
        metavar="TRANSITION_OR_SCENARIO_ID",
        help="inner loop: scope coverage to one transition/scenario id; "
        "omit to fold the whole plan",
    )
    g = sub.add_parser("gate")
    g.add_argument("coverage")
    g.add_argument("--baseline", help="exact named failures, each with a reason; never covered")
    report = sub.add_parser("report")
    report.add_argument("coverage")
    report.add_argument("plan")
    report.add_argument("--out", required=True)
    runner = []
    if "--" in argv:
        separator = argv.index("--")
        argv, runner = argv[:separator], argv[separator + 1 :]
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            result = discover(Path(args.config).resolve())
        elif args.command == "catalog":
            result = build_catalog(read(args.inventory))
            if not args.quiet:
                print(result["summary"])
            if args.report:
                out = Path(args.report)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_catalog(result, args.focus))
        elif args.command == "plan":
            result = plan(read(args.model), args.target, args.max_states)
        elif args.command == "run":
            _deprecated("run")
            if not runner:
                raise ValueError("run requires a runner command after --")
            output_path = Path(args.out).resolve()
            if output_path in {Path(p).resolve() for p in (args.plan, args.inventory, args.config) if p}:
                raise ValueError("run output must not overwrite its inputs")
            output_path.unlink(missing_ok=True)
            execution_plan, raw_inventory = read(args.plan), read(args.inventory)
            from ..features.evidence import flow_inventory
            inventory = flow_inventory(raw_inventory)
            if bool(args.outcomes_map) != bool(args.capability_inventory):
                raise ValueError("outcome map and capability inventory must be supplied together")
            input_provenance = None
            if args.outcomes_map:
                from .. import outcomes
                evidence_root = Path(args.evidence_target).resolve()
                outcome_map = read(args.outcomes_map)
                capability_inventory = read(args.capability_inventory)
                input_provenance = outcomes.provenance(
                    evidence_root, outcome_map, capability_inventory
                )
            def verify_inventory() -> None:
                if args.config:
                    discovered = flow_inventory(discover(Path(args.config).resolve()))
                    if discovered != inventory:
                        raise ValueError("source inventory is stale; re-discover and review")
                    return
                if raw_inventory.get("kind") != "capabilities":
                    raise ValueError("a legacy flow inventory requires --config")
                from ..artifacts import source_patterns_of, tree_sha256
                derived = raw_inventory.get("derived_from", {})
                artifact = Path(args.evidence_target).resolve() / derived.get("artifact", "")
                if (not artifact.is_dir()
                        or tree_sha256(artifact, source_patterns_of(derived))[0]
                        != derived.get("artifact_sha256")):
                    raise ValueError("unified capability inventory is stale; re-discover and review")
            verify_inventory()
            # Private fresh path + nonce: a successful command cannot reuse yesterday's run.
            with tempfile.TemporaryDirectory(prefix="capcov-flow-") as directory:
                output = Path(directory) / "run.json"
                canonical_plan = Path(directory) / "plan.json"
                canonical_plan.write_text(
                    json.dumps(execution_plan, sort_keys=True, ensure_ascii=False)
                )
                nonce = uuid.uuid4().hex
                env = {
                    **os.environ,
                    "CAPCOV_FLOW_PLAN": str(canonical_plan),
                    "CAPCOV_FLOW_OUT": str(output),
                    "CAPCOV_FLOW_NONCE": nonce,
                }
                if args.only is not None:
                    env["CAPCOV_FLOW_ONLY"] = args.only
                proc = subprocess.run(runner, env=env, timeout=args.timeout, check=False)
                if not output.exists():
                    raise ValueError("runner produced no fresh outcome evidence")
                result = read(str(output))
                if result.get("nonce") != nonce:
                    raise ValueError("runner nonce does not match this execution")
                if result.get("plan_sha256") != digest(execution_plan):
                    raise ValueError("runner did not attest the exact plan")
                verify_inventory()
                if input_provenance is not None and outcomes.provenance(
                    evidence_root, outcome_map, capability_inventory
                ) != input_provenance:
                    raise ValueError("outcome evidence inputs changed during flow execution")
                result["inventory_sha256"] = digest(inventory)
                if input_provenance is not None:
                    result["outcome_input_provenance"] = input_provenance
                if proc.returncode != 0 or result.get("status") != "passed":
                    result["status"] = "failed"
                    emit(args.out, result)
                    return 1
        elif args.command == "coverage":
            _deprecated("coverage")
            # Pass ``only`` only when asked, so the unscoped call is the bare
            # four-argument invocation that folds the whole plan via reconcile's
            # own default; a bare --only-less run threads no scope keyword at all.
            scope = {"only": args.only} if args.only is not None else {}
            result = reconcile(
                read(args.inventory),
                read(args.model),
                read(args.plan),
                read(args.run) if args.run else None,
                **scope,
            )
            print(f"capcov flows: {result['summary']}; assurance={result['assurance']}")
        elif args.command == "report":
            coverage, execution_plan = read(args.coverage), read(args.plan)
            lines = [
                f"# {coverage['scope']}",
                "",
                f"Target: {coverage['target']}. Evidence: {coverage['assurance']}.",
                "",
                f"Complete: **{coverage['complete']}**. "
                + ", ".join(f"{key}: {value}" for key, value in coverage["summary"].items()),
                "",
                "## Executable paths",
                "",
            ]
            for scenario in execution_plan["scenarios"]:
                steps = " → ".join(s["transition"] for s in scenario["steps"])
                lines.append(f"- **{scenario['id']}**: {steps}")
            lines.extend(["", "## Blocked flows", ""])
            for blocked in coverage["blocked"]:
                lines.append(f"- {blocked['transition']}: {blocked['reason']}")
            lines.extend(
                [
                    "",
                    "## Capability obligations",
                    "",
                    "| Obligation | Status | Flows | Evidence location |",
                    "|---|---|---|---|",
                ]
            )
            for row in coverage["rows"]:
                source = row.get("source", {})
                cells = [
                    row["id"],
                    row["status"],
                    ", ".join(row["transitions"]),
                    f"{source.get('file', '')}:{source.get('line', '')}",
                ]
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(lines) + "\n")
            return 0
        else:
            # MIGRATION NOTE (flows baseline -> core exemptions.toml). The flows
            # baseline is a flat `{"<failure>": "<reason>"}` map, e.g.
            #   {"unmapped: http:GET /jobs/{id}": "Existing screen; awaiting a flow"}
            # The unified gate (capcov gate) reads a cell-typed, dated exemptions.toml:
            #   [[exempt]]
            #   entity = "http:GET /jobs/{id}"   # the id after the "<status>: " prefix
            #   cell   = "static_only"           # unmapped/unproven -> static_only;
            #                                    # runtime-only -> runtime_only;
            #                                    # boundary/unresolved -> unresolved
            #   reason = "Existing screen; awaiting a flow"   # carried verbatim
            #   date   = "2026-09-13"            # REQUIRED by core; a bare baseline had none
            # The weaker freeform scheme does not survive "one gate": every waiver now
            # carries a reason AND a date AND the cell it exempts, so a stale waiver
            # fails instead of hiding a regression.
            _deprecated("gate")
            coverage = read(args.coverage)
            failures = set(coverage["failures"])
            baseline = read(args.baseline) if args.baseline else {}
            if any(
                not isinstance(reason, str) or not reason.strip() for reason in baseline.values()
            ):
                raise ValueError("every baseline failure requires a reason")
            new, obsolete = failures - set(baseline), set(baseline) - failures
            for failure in sorted(new):
                print(f"FAIL {failure}")
            for failure in sorted(obsolete):
                print(f"OBSOLETE baseline: {failure}")
            print(
                f"{len(failures)} total gaps; {len(failures & set(baseline))} baselined; "
                f"complete={coverage['complete']}"
            )
            return int(bool(new or obsolete))
        status = emit(args.out, result, getattr(args, "check", False))
        if (
            args.command == "catalog"
            and args.require_declarations
            and not result["declarations_accounted"]
        ):
            print(
                "capcov flows: FAIL -- declaration census or source-file accounting is incomplete"
            )
            return 1
        return status
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"capcov flows: {exc}")
        return 1
