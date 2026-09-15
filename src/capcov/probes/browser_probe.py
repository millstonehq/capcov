"""The browser probe: the integration seam. Planner in, four-cell observed out.

This is the single hard seam of the consolidation (design 1.4). Every other
probe observes what a test suite or a pipeline touched; this one first PLANS the
exercises (it internalizes the flow planner), then drives the browser runner,
then PROJECTS the surface-only browser world onto the entity-centric four-cell
``observed`` contract that the one reconcile reads.

Three moving parts:

1. **The planner, internalized.** ``flows.model.plan`` (EFSM/STRIPS BFS, one
   shortest-prerequisite scenario per reachable transition, within the state
   budget; not Chow's conformance method) is called IN-PROCESS, read-only. It stops being a public pipeline
   stage; ``blocked`` / ``states_explored`` become internal reachability
   accounting carried as provenance.

2. **The runner, driven.** The existing external-runner contract is reused
   verbatim: ``CAPCOV_FLOW_{PLAN,OUT,NONCE,ONLY}`` after the run, the nonce
   echoed and checked, the source tree re-hashed before and after (the strong
   freshness guard, design R4). The runner produces flows run evidence --
   ``scenarios[].{status,assertions,observed_requests}`` and ``mounted_surfaces``.

3. **The projection (this module's real work).** Each ``observed_requests[]``
   surface (``"http:METHOD path"``) becomes a runtime-reached binding whose
   ENTITY is the same route-obligation id (design 1.2: surface == entity for
   route targets, the faithful mapping of the surface-only browser world onto the
   entity-centric reconcile). ``operations`` = HTTP verb -> CRUD (weak but
   declared, R2). ``tests`` = the scenario id. A scenario's PASSING assertions
   gate whether its bindings are emitted at all -- assertion evidence flattened
   into emit/omit + ``tests``. A diagnostic-scope run credits nothing (its
   surfaces go to ``excluded_surfaces``, visible, never silent).

Everything the four cells cannot hold -- per-assertion detail, mutation results,
the blocked list, the mount census -- rides as ``flows_*`` namespaced extras on
the observed artifact, present only when a browser run produced them (the
``scip_*`` conditional-copy pattern). The runtime side's own honest-denominator
carriers (``excluded_surfaces`` / ``unresolved``) are folded in so a browser
run's saw-but-out-of-scope and could-not-evaluate material reaches coverage ->
gate instead of silently shrinking N. Its runtime-probe ``unresolved`` material
marks itself ``gating: False`` (reported-only): it is legible in coverage but is
not a static surface/spec obligation, so it never over-fails the gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import adapters, artifacts
from ..flows.model import digest, plan
from .probe_registry import (
    ENV_NONCE,
    ENV_ONLY,
    ENV_OUT,
    ENV_SOURCE_ROOT,
    ENV_TARGET,
    FreshnessGuard,
    observed_carriers,
    source_provenance_from_env,
)

# HTTP verb -> CRUD, the SAME weak-but-declared map the promoted route adapter
# uses statically (adapters.build_core_dict). Keeping them identical is what makes
# a route's static operation and its runtime operation comparable in reconcile's
# untested_operations. Verbs outside the set (HEAD, OPTIONS, a verb-less route)
# claim no operation rather than guess one.
VERB_TO_CRUD = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

# The plan target key a browser flow model binds its commands under. The flow
# model's transitions carry `bindings[<target>]`; the runner drives that target.
DEFAULT_TARGET = "browser"


def _crud_for_surface(surface: str) -> list[str]:
    """The CRUD operation an ``"http:METHOD path"`` surface declares, if any.

    A runtime request carries only its HTTP verb, so this is the honest limit of
    what the browser can say about the operation (R2): coarser than the SQL-verb
    the python probe reads, and a route whose verb is outside the recognised set
    claims nothing rather than fabricate an operation.
    """
    if not surface.startswith("http:"):
        return []
    method = surface[len("http:"):].split(" ", 1)[0]
    crud = VERB_TO_CRUD.get(method.upper())
    return [crud] if crud else []


def _required_assertions(plan_scenario: dict) -> list[str]:
    """The assertion ids a passing run of this scenario must attest.

    Exactly the ``"<i>:<transition>:<assert-id>"`` shape the flows runner and
    ``flows.model.reconcile`` already speak, computed here from the plan so the
    projection can flatten "did every planned assertion pass?" into emit/omit.
    """
    return [
        f"{index}:{step['transition']}:{command['id']}"
        for index, step in enumerate(plan_scenario["steps"])
        for command in step["commands"]
        if command["op"] == "assert"
    ]


def _scenario_index(scenarios: list[dict]) -> dict[str, dict]:
    index = {}
    if not isinstance(scenarios, list):
        raise ValueError("browser scenarios must be a list")
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise ValueError("browser scenario requires a string id")
        identity = scenario["id"]
        if not identity or identity in index:
            raise ValueError(f"empty or duplicate browser scenario id: {identity!r}")
        index[identity] = scenario
    return index


def project(execution_plan: dict, run: dict, *, only: str | None = None) -> dict:
    """Project a plan + its browser run evidence onto the ``observed`` body.

    The seam's real work. Returns the observed artifact BODY -- ``bindings`` +
    the honest-denominator carriers + the ``flows_*`` namespaced richness -- ready
    for ``artifacts.write(kind="observed")`` and, unchanged, for the one reconcile.

    A binding is emitted for a request only when its scenario passed in full
    scope: ``run.status == "passed"``, the scenario's own status is ``"passed"``,
    and its attested assertions are exactly the planned ones. Anything else omits
    the binding (the route then reads as ``static_only`` -- a coverage gap -- in
    the four cells) and names WHY, so nothing is dropped silently:

    * a diagnostic-scope run credits nothing; every surface it touched goes to
      ``excluded_surfaces`` (seen, out of scope, visible);
    * a scenario that did not pass its planned assertions is named in
      ``unresolved`` for diagnostics and in ``flows_failures`` for gating. A
      different passing scenario on the same route cannot discharge this failure.

    ``only`` scopes the fold to a single scenario id (the inner loop), matching
    ``CAPCOV_FLOW_ONLY`` / ``flows.model.reconcile``'s ``only``: other scenarios
    contribute no bindings and are left silent (they are in scope, just not run).
    """
    plan_scenarios = _scenario_index(execution_plan.get("scenarios", []))
    run_scenarios = _scenario_index(run.get("scenarios", []))
    for result in run_scenarios.values():
        assertions = result.get("assertions", [])
        requests = result.get("observed_requests", [])
        if not isinstance(assertions, list) or any(not isinstance(a, str) for a in assertions):
            raise ValueError("browser assertions must be a list of assertion ids")
        if not isinstance(requests, list) or any(not isinstance(r, dict) for r in requests):
            raise ValueError("browser observed_requests must be a list of requests")
    if only is not None and only not in plan_scenarios:
        raise ValueError(f"unknown browser scenario selection: {only}")
    unexpected = set(run_scenarios) - set(plan_scenarios)
    if unexpected:
        raise ValueError(f"unplanned browser scenarios: {sorted(unexpected)}")
    run_passed = run.get("status") == "passed"
    execution_scope = run.get("execution_scope", "full")
    diagnostic = execution_scope == "diagnostic"

    # Behavioral failures must survive entity aggregation: another scenario may
    # pass on the SAME route. These cannot be waived by structural exemptions.
    failures = []
    if not plan_scenarios:
        failures.append({"id": "plan", "reason": "no planned scenarios"})
    if not run_passed:
        failures.append({"id": "run", "reason": "browser run did not pass"})
    if execution_scope != "full" or only is not None:
        failures.append({"id": "scope", "reason": "partial/diagnostic execution cannot qualify the full plan"})
    for blocked in execution_plan.get("blocked", []):
        failures.append({"id": blocked["transition"], "reason": blocked["reason"]})

    binding_index: dict[tuple[str, str], dict[str, set[str]]] = {}
    excluded_surfaces: list[dict] = []
    unresolved: list[dict] = []
    scenario_detail: list[dict] = []

    for scenario_id in sorted(plan_scenarios):
        if only is not None and scenario_id != only:
            continue
        plan_scenario = plan_scenarios[scenario_id]
        result = run_scenarios.get(scenario_id)
        required = _required_assertions(plan_scenario)
        observed_requests = (result or {}).get("observed_requests", [])
        scenario_passed = (
            run_passed
            and result is not None
            and result.get("status") == "passed"
            and bool(required)
            and result.get("assertions") == required
        )
        emitted = bool(scenario_passed and not diagnostic)

        detail = {
            "id": scenario_id,
            "status": (result or {}).get("status"),
            "assertions": list((result or {}).get("assertions", [])),
            "required_assertions": required,
            "observed_requests": list(observed_requests),
            "emitted": emitted,
        }
        mutations = [
            step["mutations"]
            for step in plan_scenario["steps"]
            if step.get("mutations")
        ]
        if mutations:
            detail["mutations"] = mutations
        scenario_detail.append(detail)

        if not scenario_passed:
            failures.append({
                "id": scenario_id,
                "reason": f"required assertions {required!r}; observed status "
                          f"{(result or {}).get('status')!r}, assertions "
                          f"{(result or {}).get('assertions')!r}",
            })

        if diagnostic:
            # Seen-but-out-of-scope at runtime: the scenarios ran, but a
            # diagnostic run cannot qualify coverage (mirrors flows'
            # "diagnostic execution does not qualify coverage"). Its surfaces
            # stay VISIBLE as excluded, not folded into a binding.
            for request in observed_requests:
                excluded_surfaces.append(
                    {
                        "surface": request.get("surface"),
                        "scenario": scenario_id,
                        "reason": "diagnostic execution does not qualify coverage",
                    }
                )
            continue

        if not scenario_passed:
            # Keep the diagnostic carrier; flows_failures independently gates
            # this scenario even if another exercise covers the same route.
            unresolved.append(
                {
                    "adapter": "browser",
                    "kind": "scenario-unproven",
                    "reason": (
                        "scenario did not pass its planned assertions"
                        if result is not None
                        else "planned scenario produced no run evidence"
                    ),
                    "scenario": scenario_id,
                    "gating": False,
                }
            )
            continue

        for request in observed_requests:
            surface = request.get("surface")
            if not surface:
                unresolved.append(
                    {
                        "adapter": "browser",
                        "kind": "request-unattributed",
                        "reason": "an observed request carried no surface id",
                        "scenario": scenario_id,
                        "gating": False,
                    }
                )
                continue
            # entity == surface: a route obligation is its own entity (design 1.2),
            # the faithful mapping of the surface-only browser world onto the
            # entity-centric reconcile. Same id the promoted adapter emits, so a
            # discovered route lands in `both`, an undiscovered one in
            # runtime_only / unknown_surfaces.
            row = binding_index.setdefault(
                (surface, surface), {"operations": set(), "tests": set()}
            )
            row["operations"].update(_crud_for_surface(surface))
            row["tests"].add(scenario_id)

    bindings = [
        {
            "surface": surface,
            "entity": entity,
            "operations": sorted(row["operations"]),
            "tests": sorted(row["tests"]),
        }
        for (surface, entity), row in sorted(binding_index.items())
    ]

    # The runtime mount census -> exercised / unexercised, projected against the
    # bindings actually emitted. reconcile derives `exercised` from binding
    # surfaces and `unexercised_surfaces` from the static side; carrying the
    # census here makes the same distinction legible at the observed layer and
    # preserves the flows mount-census richness the four cells cannot hold.
    mounted = sorted({s for s in run.get("mounted_surfaces", []) if s})
    exercised = {binding["surface"] for binding in bindings}
    mount_census = {
        "mounted": mounted,
        "exercised": sorted(m for m in mounted if m in exercised),
        "unexercised": sorted(m for m in mounted if m not in exercised),
    }

    body: dict = {
        "bindings": bindings,
        "flows_failures": failures,
        "exercises": len(
            [d for d in scenario_detail if d["emitted"]]
        ),
        **observed_carriers(
            excluded_surfaces=excluded_surfaces, unresolved=unresolved
        ),
    }

    # flows-specific richness, carried ONLY when a browser run produced it (the
    # scip_* conditional-copy pattern: present when meaningful, absent -- and so
    # backward compatible -- otherwise). Never collapsed to a percentage.
    if scenario_detail:
        body["flows_scenarios"] = scenario_detail
    if mounted:
        body["flows_mount_census"] = mount_census
    blocked = execution_plan.get("blocked") or []
    if blocked:
        body["flows_blocked"] = blocked
    unmutated = execution_plan.get("unmutated_state_transitions") or []
    if unmutated:
        body["flows_unmutated_state_transitions"] = unmutated
    scope = execution_plan.get("scope")
    if scope is not None:
        body["flows_scope"] = scope
    if run.get("assurance") is not None:
        body["flows_assurance"] = run["assurance"]
    body["flows_execution_scope"] = execution_scope
    return body


def observe(
    *,
    model: dict,
    source_root: Path | str,
    out: Path | str,
    runner: list[str],
    target: str = DEFAULT_TARGET,
    nonce: str | None = None,
    only: str | None = None,
    timeout: int = 180,
    max_states: int = 10000,
    source_patterns: tuple[str, ...] = ("**/*.py",),
    source_snapshot=None,
    source_provenance: dict | None = None,
) -> dict:
    """Plan, drive the runner under the freshness guard, project, write observed.

    Mirrors ``flows run`` end-to-end (design 1.4 / R4): plan in-process, unlink
    the stale output, snapshot the tree, drive the runner with the flow env into
    PRIVATE nonce-stamped evidence, verify the nonce + the exact plan + that the
    tree did not change during the run, then project to the shared ``observed``
    artifact. The nonce lives only in the runner's private evidence; the observed
    schema has no nonce field.
    """
    phase_started = time.perf_counter_ns()
    if not runner:
        raise ValueError("browser probe requires a runner command")
    execution_plan = plan(model, target, max_states)
    source = Path(source_root)
    out_path = Path(out)
    guard = FreshnessGuard(
        out_path, source, patterns=source_patterns, snapshot=source_snapshot
    )
    run_nonce = guard.begin(nonce)
    with tempfile.TemporaryDirectory(prefix="capcov-browser-") as directory:
        plan_path = Path(directory) / "plan.json"
        plan_path.write_text(json.dumps(execution_plan, sort_keys=True))
        run_out = Path(directory) / "run.json"
        env = {
            **os.environ,
            "CAPCOV_FLOW_PLAN": str(plan_path),
            "CAPCOV_FLOW_OUT": str(run_out),
            "CAPCOV_FLOW_NONCE": run_nonce,
        }
        env.pop("CAPCOV_FLOW_ONLY", None)
        if only is not None:
            env["CAPCOV_FLOW_ONLY"] = only
        proc = subprocess.run(runner, env=env, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise ValueError(f"browser runner failed (exit {proc.returncode})")
        # verify_output: fresh evidence exists, its nonce matches this run, and
        # the source tree is unchanged since begin(). A successful command cannot
        # reuse yesterday's run.
        run = guard.verify_output(run_out)
        if run.get("plan_sha256") != digest(execution_plan):
            raise ValueError(
                "browser runner did not attest the exact plan "
                "(run describes a different scenario set)"
            )

    body = project(execution_plan, run, only=only)
    if source_provenance is not None:
        derived_from = {
            **source_provenance,
            "extractor": "capcov browser-probe",
        }
    else:
        derived_from = guard.snapshot.provenance(
            os.path.basename(str(source)), "capcov browser-probe"
        )
    body["timing"] = {
        "observe_ms": min(
            max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
            86_400_000,
        ),
        "source_verification": guard.verification,
    }
    artifacts.write(
        out_path,
        "observed",
        derived_from,
        body,
    )
    return artifacts.read(out_path, "observed")


def _load_model(target_dir: Path) -> tuple[dict, str, tuple[str, ...]]:
    """Resolve the flow model + plan target + source globs from capcov.toml.

    ``[capcov] flows_model`` names the referenced EFSM (a large graph kept as a
    referenced artifact, not inlined TOML -- design 1.1); ``flows_target``
    defaults to ``"browser"``; the source patterns come from the adapter specs so
    a non-Python target's mid-run change is still caught.

    The pattern set is resolved by `capcov.adapters.source_patterns`, the same
    call `discover` makes. It has to be: `reconcile` refuses a static artifact and
    a runtime artifact whose `artifact_sha256` disagree, so two hand-rolled copies
    of this rule would drift into a pipeline that always refuses itself.
    """
    import tomllib

    config_path = target_dir / "capcov.toml"
    if not config_path.exists():
        raise ValueError(f"browser probe: no capcov.toml at {target_dir}")
    data = tomllib.loads(config_path.read_text())
    capcov = data.get("capcov", {})
    model_ref = capcov.get("flows_model")
    if not model_ref:
        raise ValueError(
            "browser probe: [capcov] flows_model must name the flow model json"
        )
    model_path = (target_dir / model_ref).resolve()
    model = json.loads(model_path.read_text())
    plan_target = capcov.get("flows_target", DEFAULT_TARGET)
    if capcov.get("adapter"):
        specs = [(capcov["adapter"], None)]
    else:
        specs = [(entry.get("name"), entry) for entry in data.get("adapters", [])]
    normalized = []
    for name, config in specs:
        resolved = None if config is None else dict(config)
        if (
            name == "treesitter-routes"
            and resolved is not None
            and not resolved.get("globs")
            and not resolved.get("files")
        ):
            resolved["globs"] = [
                artifacts.language_pattern(
                    resolved.get("language") or resolved.get("scip_language")
                )
            ]
        normalized.append((name, resolved))
    return model, plan_target, adapters.source_patterns(normalized)


def main(argv: list[str] | None = None) -> int:
    """The entry the probe registry points at: drive the browser probe from env.

    Reads the unified observe env contract (design 1.3): ``CAPCOV_OUT`` /
    ``CAPCOV_SOURCE_ROOT`` / ``CAPCOV_TARGET`` / ``CAPCOV_NONCE`` /
    ``CAPCOV_ONLY``. The RUNNER command is ``argv`` (what follows ``--`` on the
    ``capcov observe --probe browser -- <runner>`` line). The flow model and plan
    target come from the target's ``capcov.toml`` ([capcov] flows_model /
    flows_target). Returns 0 on a fresh, valid observation.
    """
    runner = list(argv or [])
    out = os.environ.get(ENV_OUT)
    source_root = os.environ.get(ENV_SOURCE_ROOT)
    target = os.environ.get(ENV_TARGET)
    if not out or not source_root or not target:
        print(
            f"capcov browser-probe: {ENV_OUT}, {ENV_SOURCE_ROOT} and {ENV_TARGET} "
            "must be set",
            file=sys.stderr,
        )
        return 2
    if not runner:
        print(
            "capcov browser-probe: a runner command is required after --",
            file=sys.stderr,
        )
        return 2
    try:
        model, plan_target, patterns = _load_model(Path(target))
        source_snapshot, source_provenance = source_provenance_from_env(source_root)
        # The carried identity settles the glob set. `_load_model` reads only
        # capcov.toml, so with `--adapter` overriding it the guard would take
        # its before-digest over one language and its after-digest over another
        # and call an unchanged tree changed.
        if source_snapshot is not None:
            patterns = source_snapshot.patterns
        observe(
            model=model,
            source_root=source_root,
            out=out,
            runner=runner,
            target=plan_target,
            nonce=os.environ.get(ENV_NONCE),
            only=os.environ.get(ENV_ONLY),
            source_patterns=patterns,
            source_snapshot=source_snapshot,
            source_provenance=source_provenance,
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"capcov browser-probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
