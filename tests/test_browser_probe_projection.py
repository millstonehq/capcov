"""The seam, pinned: planner + browser run evidence -> four-cell observed.

The browser probe is the one hard seam of the consolidation. These tests drive
the REAL flow planner (``flows.model.plan``, imported read-only) and the REAL
core reconcile + gate, so they prove the projection lands the surface-only
browser world in the entity-centric four cells exactly as design 1.4 requires:

  - a passing scenario for a discovered route -> ``both``;
  - a hit on an undiscovered route -> ``runtime_only`` + ``unknown_surfaces``;
  - a diagnostic-scope run credits nothing (zero coverage);
  - an assertion failure omits the binding (and names why, never dropped);
  - the runtime mount census maps to exercised / unexercised.

They exercise core, not tree-sitter, so they RUN (do not skip) under
``--extra treesitter`` -- the false-floor guard (design R3). ``observe`` is also
driven end-to-end through a fake external runner to pin the freshness guard and
the plan attestation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.core.gate import gate
from capcov.core.reconcile import reconcile
from capcov.flows.model import digest, plan
from capcov.probes.browser_probe import observe, project

LIST_ROUTE = "http:GET /jobs"
OPEN_ROUTE = "http:GET /jobs/{id}"

# Which route each transition drives at runtime. The plan carries steps and
# commands, not surfaces (surfaces live on the model's obligations), so the
# fixtures below map transition -> surface explicitly, the way a runner would
# observe the request the browser actually made.
SURFACE_BY_TRANSITION = {"list": LIST_ROUTE, "open": OPEN_ROUTE}


def flow_model() -> dict:
    """A two-transition browser flow: list jobs, open one. Both reachable from
    the initial state, so the plan is two single-step scenarios."""
    return {
        "version": 1,
        "scope": "jobs",
        "facts": ["listed"],
        "initial": [],
        "transitions": [
            {
                "id": "list",
                "requires": [],
                "adds": [],
                "actor": "operator",
                "outcome": "list",
                "evidence": [{"file": "jobs.py", "line": 1}],
                "obligations": [LIST_ROUTE],
                "bindings": {
                    "browser": {
                        "commands": [
                            {"op": "assert", "id": "rows", "selector": "body",
                             "text": "jobs"},
                        ]
                    }
                },
            },
            {
                "id": "open",
                "requires": [],
                "adds": [],
                "actor": "operator",
                "outcome": "open",
                "evidence": [{"file": "jobs.py", "line": 2}],
                "obligations": [OPEN_ROUTE],
                "bindings": {
                    "browser": {
                        "commands": [
                            {"op": "assert", "id": "detail", "selector": "body",
                             "text": "job"},
                        ]
                    }
                },
            },
        ],
    }


def run_evidence(
    execution_plan: dict,
    *,
    status: str = "passed",
    execution_scope: str = "full",
    mounted_surfaces: list[str] | None = None,
    hit: dict[str, str] | None = None,
    scenario_status: dict[str, str] | None = None,
    scenario_assertions: dict[str, list[str]] | None = None,
) -> dict:
    """Build browser run evidence for a plan, all scenarios passing by default.

    ``hit`` overrides the surface a scenario's request lands on (to model a hit
    on an UNdiscovered route); ``scenario_status`` / ``scenario_assertions``
    override a single scenario to model a failure. Assertions default to exactly
    the planned ids so a passing scenario is emitted.
    """
    hit = hit or {}
    scenario_status = scenario_status or {}
    scenario_assertions = scenario_assertions or {}
    scenarios = []
    for scenario in execution_plan["scenarios"]:
        sid = scenario["id"]
        planned = [
            f"{i}:{step['transition']}:{c['id']}"
            for i, step in enumerate(scenario["steps"])
            for c in step["commands"]
            if c["op"] == "assert"
        ]
        observed_requests = [
            {
                "step": f"{i}:{step['transition']}",
                "surface": hit.get(sid, SURFACE_BY_TRANSITION[step["transition"]]),
            }
            for i, step in enumerate(scenario["steps"])
        ]
        scenarios.append(
            {
                "id": sid,
                "status": scenario_status.get(sid, "passed"),
                "assertions": scenario_assertions.get(sid, planned),
                "observed_requests": observed_requests,
            }
        )
    return {
        "status": status,
        "assurance": "test-fixture",
        "plan_sha256": digest(execution_plan),
        "execution_scope": execution_scope,
        "mounted_surfaces": mounted_surfaces
        if mounted_surfaces is not None
        else [LIST_ROUTE, OPEN_ROUTE],
        "scenarios": scenarios,
    }


_ZERO_RESIDUE = {
    "resolved_by_import": 0, "resolved_by_name": 0, "ambiguous": 0,
    "external": 0, "chained": 0, "builtin_shadowed": 0,
}


def discovered_capabilities(route_ids: list[str]) -> dict:
    """The capabilities artifact a promoted route adapter emits for these routes:
    each route is its own entity and its own hop-0 surface (design 1.2)."""
    method_op = {"GET": "read", "POST": "create", "PUT": "update",
                 "PATCH": "update", "DELETE": "delete"}
    caps = []
    for route in route_ids:
        method = route[len("http:"):].split(" ", 1)[0]
        caps.append(
            {
                "entity": route,
                "surface": route,
                "operations": [method_op.get(method, "read")],
                "evidence": {"hops": 0, "chain": [route], "kind": "direct"},
            }
        )
    return {
        "entities": [{"name": r} for r in route_ids],
        "surfaces": [{"id": r, "handler": r, "mounted": True} for r in route_ids],
        "capabilities": caps,
        "blind_spots": [],
        "residue": [],
        "residue_summary": dict(_ZERO_RESIDUE),
        "excluded_surfaces": {"count": 0, "surfaces": []},
        "unresolved": [],
    }


def observed_artifact(body: dict) -> dict:
    """Wrap a projected observed BODY as the artifact reconcile consumes."""
    return {"schema_version": 1, "kind": "observed", "derived_from": {}, **body}


def gate_failures(coverage: dict) -> set[tuple[str, str]]:
    """(rule, subject) pairs the gate raised.

    Asserting the PAIR, not merely that the gate is non-empty: a gate reddened by
    an unrelated `undiscovered-surface` proves nothing about required-flow gating,
    and a test that cannot tell the two apart passes against a gate with the flow
    check deleted (tests/fixtures/backpressure/ignore-required-flows.patch).
    """
    return {(f.rule, f.subject) for f in gate(coverage, None)}


class ProjectionShape(unittest.TestCase):
    def test_carriers_are_always_present(self) -> None:
        body = project(plan(flow_model(), "browser"), run_evidence(plan(flow_model(), "browser")))
        # The honest-denominator carriers are ALWAYS on the observed body, even
        # when empty -- absent-defaults-to-dropped is the Property-3 regression.
        self.assertIn("excluded_surfaces", body)
        self.assertIn("unresolved", body)
        self.assertEqual(body["excluded_surfaces"], {"count": 0, "surfaces": []})
        self.assertEqual(body["unresolved"], [])

    def test_verb_projects_to_crud_and_entity_equals_surface(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan))
        bindings = {b["surface"]: b for b in body["bindings"]}
        self.assertEqual(set(bindings), {LIST_ROUTE, OPEN_ROUTE})
        for route, binding in bindings.items():
            self.assertEqual(binding["entity"], route)   # surface == entity
            self.assertEqual(binding["operations"], ["read"])   # GET -> read
        self.assertEqual(bindings[LIST_ROUTE]["tests"], ["list"])

    def test_flows_richness_rides_as_namespaced_extras(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan))
        # per-scenario detail + mount census carried, never collapsed to a %.
        self.assertEqual(len(body["flows_scenarios"]), 2)
        self.assertEqual(body["flows_execution_scope"], "full")
        self.assertIn("flows_mount_census", body)


class PassingScenarioForDiscoveredRouteIsBoth(unittest.TestCase):
    def test_both(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        observed = observed_artifact(project(execution_plan, run_evidence(execution_plan)))
        capabilities = discovered_capabilities([LIST_ROUTE, OPEN_ROUTE])
        coverage = reconcile(capabilities, observed)
        self.assertEqual(coverage["summary"]["both"], 2)
        self.assertEqual(coverage["summary"]["static_only"], 0)
        self.assertEqual(coverage["summary"]["runtime_only"], 0)
        self.assertEqual(coverage["unknown_surfaces"], [])
        # a discovered route a passing scenario exercised is a genuine capability.
        self.assertEqual(gate(coverage, None), [])


class HitOnUndiscoveredRouteIsRuntimeOnly(unittest.TestCase):
    def test_runtime_only_and_unknown_surface(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        observed = observed_artifact(project(execution_plan, run_evidence(execution_plan)))
        # discover found only /jobs; the browser hit /jobs/{id} too.
        capabilities = discovered_capabilities([LIST_ROUTE])
        coverage = reconcile(capabilities, observed)
        self.assertEqual(coverage["summary"]["both"], 1)          # /jobs
        self.assertEqual(coverage["summary"]["runtime_only"], 1)  # /jobs/{id}
        self.assertEqual(
            [u["surface"] for u in coverage["unknown_surfaces"]], [OPEN_ROUTE]
        )
        rules = sorted(f.rule for f in gate(coverage, None))
        self.assertIn("undiscovered-surface", rules)


class DiagnosticScopeAwardsZeroCoverage(unittest.TestCase):
    def test_zero_coverage(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(
            execution_plan, run_evidence(execution_plan, execution_scope="diagnostic")
        )
        # a diagnostic run emits NO bindings...
        self.assertEqual(body["bindings"], [])
        # ...and its surfaces stay visible as excluded (seen, out of scope).
        self.assertEqual(body["excluded_surfaces"]["count"], 2)
        capabilities = discovered_capabilities([LIST_ROUTE, OPEN_ROUTE])
        coverage = reconcile(capabilities, observed_artifact(body))
        self.assertEqual(coverage["summary"]["both"], 0)
        self.assertEqual(coverage["summary"]["static_only"], 2)


class AssertionFailureOmitsTheBinding(unittest.TestCase):
    def test_pass_on_same_route_cannot_hide_required_failure(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        run = run_evidence(
            execution_plan, hit={"list": OPEN_ROUTE},
            scenario_status={"open": "failed"},
        )
        coverage = reconcile(
            discovered_capabilities([OPEN_ROUTE]),
            observed_artifact(project(execution_plan, run)),
        )
        self.assertEqual(coverage["summary"]["both"], 1)
        self.assertTrue(any(f.subject == "open" for f in gate(coverage, None)))

    def test_missing_scenario_cannot_hide_behind_shared_route(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        run = run_evidence(execution_plan, hit={"list": OPEN_ROUTE})
        run["scenarios"] = [s for s in run["scenarios"] if s["id"] == "list"]
        coverage = reconcile(discovered_capabilities([OPEN_ROUTE]),
                             observed_artifact(project(execution_plan, run)))
        # `list` passed on the very route `open` was supposed to exercise. The
        # absent scenario must still be named, by id.
        self.assertIn(("required-flow-failed", "open"), gate_failures(coverage))

    def test_duplicate_scenario_cannot_overwrite_failure(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        run = run_evidence(execution_plan)
        run["scenarios"].insert(0, {**run["scenarios"][0], "status": "failed"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            project(execution_plan, run)

    def test_partial_and_assertionless_runs_cannot_qualify(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        for scope in ("diagnostic", "partial", "unknown"):
            with self.subTest(scope=scope):
                body = project(execution_plan, run_evidence(execution_plan, execution_scope=scope))
                # Every route IS discovered here, so a scope failure is the only
                # thing that can redden the gate on the non-diagnostic scopes --
                # against an empty inventory `undiscovered-surface` reddens it for
                # free and the scope rule goes untested.
                coverage = reconcile(
                    discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]),
                    observed_artifact(body),
                )
                self.assertIn(("required-flow-failed", "scope"), gate_failures(coverage))
        for scenario in execution_plan["scenarios"]:
            for step in scenario["steps"]:
                step["commands"] = []
        body = project(execution_plan, run_evidence(execution_plan))
        coverage = reconcile(
            discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), observed_artifact(body)
        )
        # A scenario that asserts nothing cannot pass by asserting nothing.
        self.assertLessEqual(
            {("required-flow-failed", "list"), ("required-flow-failed", "open")},
            gate_failures(coverage),
        )

    def test_only_cannot_qualify_full_plan_even_on_shared_route(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        run = run_evidence(execution_plan, hit={"list": OPEN_ROUTE})
        body = project(execution_plan, run, only="list")
        coverage = reconcile(discovered_capabilities([OPEN_ROUTE]), observed_artifact(body))
        self.assertIn(("required-flow-failed", "scope"), gate_failures(coverage))

    def test_old_browser_receipt_requires_fresh_observation(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan))
        del body["flows_failures"]
        coverage = reconcile(discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), observed_artifact(body))
        self.assertTrue(any("re-run observe" in f.detail for f in gate(coverage, None)))

    def test_structural_exemption_cannot_waive_runtime_failure(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan, scenario_status={"list": "failed"}))
        coverage = reconcile(discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), observed_artifact(body))
        with tempfile.TemporaryDirectory() as directory:
            exemptions = Path(directory) / "exemptions.toml"
            exemptions.write_text(f'[[exempt]]\nentity = "{LIST_ROUTE}"\ncell = "static_only"\n'
                                  'reason = "measurement gap"\ndate = "2026-09-14"\n')
            failures = gate(coverage, exemptions)
            self.assertEqual([f.rule for f in failures], ["required-flow-failed"])

    def test_failed_scenario_status_omits_binding(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(
            execution_plan,
            run_evidence(execution_plan, scenario_status={"list": "failed"}),
        )
        surfaces = {b["surface"] for b in body["bindings"]}
        self.assertNotIn(LIST_ROUTE, surfaces)   # failing scenario -> omitted
        self.assertIn(OPEN_ROUTE, surfaces)      # the passing one stays
        # omitted, but not dropped: named, reported-only so the gate is not
        # double-charged (the route already reads as static_only).
        unproven = [u for u in body["unresolved"] if u.get("scenario") == "list"]
        self.assertEqual(len(unproven), 1)
        self.assertIs(unproven[0]["gating"], False)

    def test_wrong_assertions_omit_binding(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(
            execution_plan,
            run_evidence(execution_plan, scenario_assertions={"list": ["0:list:wrong"]}),
        )
        self.assertNotIn(LIST_ROUTE, {b["surface"] for b in body["bindings"]})

    def test_run_level_failure_omits_every_binding(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan, status="failed"))
        self.assertEqual(body["bindings"], [])

    def test_runtime_unresolved_does_not_over_fail_the_gate(self) -> None:
        # A failing scenario's `unresolved` entry (gating=False) must not add an
        # unresolved-obligation failure; only the static_only coverage gap fails.
        execution_plan = plan(flow_model(), "browser")
        observed = observed_artifact(
            project(execution_plan,
                    run_evidence(execution_plan, scenario_status={"list": "failed"}))
        )
        coverage = reconcile(discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), observed)
        rules = sorted(f.rule for f in gate(coverage, None))
        self.assertNotIn("unresolved-obligation", rules)
        self.assertIn("unexplained-static_only", rules)   # /jobs is the gap


class MountCensusMapsToExercisedAndUnexercised(unittest.TestCase):
    def test_projection_splits_census(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        # mounted A, B, C; only `list` (A) is exercised by a passing scenario.
        extra = "http:GET /health"
        body = project(
            execution_plan,
            run_evidence(
                execution_plan,
                mounted_surfaces=[LIST_ROUTE, OPEN_ROUTE, extra],
                scenario_status={"open": "failed"},
            ),
        )
        census = body["flows_mount_census"]
        self.assertEqual(census["exercised"], [LIST_ROUTE])
        self.assertEqual(census["unexercised"], sorted([OPEN_ROUTE, extra]))

    def test_reconcile_sees_exercised_and_unexercised(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        observed = observed_artifact(
            project(
                execution_plan,
                run_evidence(
                    execution_plan,
                    mounted_surfaces=[LIST_ROUTE, OPEN_ROUTE],
                    scenario_status={"open": "failed"},
                ),
            )
        )
        coverage = reconcile(discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), observed)
        # the exercised route is not in the unexercised list; the mounted-but-
        # never-driven one is.
        self.assertNotIn(LIST_ROUTE, coverage["unexercised_surfaces"])
        self.assertIn(OPEN_ROUTE, coverage["unexercised_surfaces"])


class OnlyScopesTheFold(unittest.TestCase):
    def test_only_emits_one_scenario(self) -> None:
        execution_plan = plan(flow_model(), "browser")
        body = project(execution_plan, run_evidence(execution_plan), only="list")
        self.assertEqual([b["surface"] for b in body["bindings"]], [LIST_ROUTE])


# The fake external runner: reads the flow env contract, attests the exact plan
# and nonce, and writes browser run evidence exactly as the real DOM runner would.
_FAKE_RUNNER = """
import json, os
from capcov.flows.model import digest
plan = json.load(open(os.environ["CAPCOV_FLOW_PLAN"]))
surface = {"list": "http:GET /jobs", "open": "http:GET /jobs/{id}"}
scenarios = []
for s in plan["scenarios"]:
    asserts = [f"{i}:{step['transition']}:{c['id']}"
               for i, step in enumerate(s["steps"])
               for c in step["commands"] if c["op"] == "assert"]
    obs = [{"step": f"{i}:{step['transition']}", "surface": surface[step["transition"]]}
           for i, step in enumerate(s["steps"])]
    scenarios.append({"id": s["id"], "status": "passed",
                      "assertions": asserts, "observed_requests": obs})
run = {"nonce": os.environ["CAPCOV_FLOW_NONCE"], "plan_sha256": digest(plan),
       "status": "passed", "assurance": "fake", "execution_scope": "full",
       "mounted_surfaces": ["http:GET /jobs", "http:GET /jobs/{id}"],
       "scenarios": scenarios}
%(nonce_line)s
json.dump(run, open(os.environ["CAPCOV_FLOW_OUT"], "w"))
"""


class ObserveEndToEnd(unittest.TestCase):
    """observe() plans, drives the runner under the freshness guard, projects."""

    def _runner(self, *, bad_nonce: bool = False) -> list[str]:
        script = _FAKE_RUNNER % {
            "nonce_line": 'run["nonce"] = "stale-nonce"' if bad_nonce else "",
        }
        return [sys.executable, "-c", script]

    def test_full_seam_produces_a_reconcilable_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            out = root / "observed.json"
            result = observe(
                model=flow_model(),
                source_root=source,
                out=out,
                runner=self._runner(),
            )
            self.assertEqual(result["kind"], "observed")
            self.assertEqual(
                {b["surface"] for b in result["bindings"]},
                {LIST_ROUTE, OPEN_ROUTE},
            )
            self.assertEqual(result["derived_from"]["extractor"], "capcov browser-probe")
            coverage = reconcile(discovered_capabilities([LIST_ROUTE, OPEN_ROUTE]), result)
            self.assertEqual(coverage["summary"]["both"], 2)

    def test_stale_nonce_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            out = root / "observed.json"
            with self.assertRaisesRegex(ValueError, "nonce"):
                observe(
                    model=flow_model(),
                    source_root=source,
                    out=out,
                    runner=self._runner(bad_nonce=True),
                )
            # a refused run leaves no stale observed artifact behind.
            self.assertFalse(out.exists())

    def test_empty_runner_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "runner"):
                observe(
                    model=flow_model(),
                    source_root=Path(directory),
                    out=Path(directory) / "o.json",
                    runner=[],
                )

    def test_failed_process_cannot_attest_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "observed.json"
            out.write_text("stale")
            runner = self._runner()
            runner[-1] += "\nraise SystemExit(7)\n"
            with self.assertRaisesRegex(ValueError, "exit 7"):
                observe(model=flow_model(), source_root=root, out=out, runner=runner)
            self.assertFalse(out.exists())

    def test_full_execution_does_not_inherit_diagnostic_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = self._runner()
            runner[-1] = 'import os\nassert "CAPCOV_FLOW_ONLY" not in os.environ\n' + runner[-1]
            with patch.dict(os.environ, {"CAPCOV_FLOW_ONLY": "list"}):
                observe(model=flow_model(), source_root=root, out=root / "observed.json", runner=runner)


if __name__ == "__main__":
    unittest.main()
