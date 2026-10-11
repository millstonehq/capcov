"""Compare static and runtime accounts without equating agreement with proof.

The comparison adapts Software Reflexion Models (Murphy, Notkin & Sullivan
1995; ADR-0001), whose original subject is architectural relationships versus
extracted source. Capcov's entity-level four-cell comparison is not an exact
implementation of that model:

    both          static and runtime support at the recorded granularity
    static_only   static support without observation in this execution scope
    runtime_only  observation without corresponding static support
    neither       declared but unsupported by either account

Neither account is complete by construction. `neither` does not establish dead
code, and `both` does not establish correct effects or matching route evidence.
Behavioral outcomes require their own assertions and execution provenance.
"""

from __future__ import annotations

CELLS = ("both", "static_only", "runtime_only", "neither")
FAILING_CELLS = ("static_only", "runtime_only", "neither")

# The residue summary the python adapter emits, all zero. A route/spec reader has
# no call graph to resolve, so it forwards this shape empty-but-present; forwarding
# an explicit default (rather than dropping the key when a side lacks it) is what
# keeps the denominator legible instead of reading as absent.
_EMPTY_RESIDUE_SUMMARY = {
    "resolved_by_import": 0,
    "resolved_by_name": 0,
    "ambiguous": 0,
    "external": 0,
    "chained": 0,
    "builtin_shadowed": 0,
}


def _merge_excluded_surfaces(capabilities: dict, observed: dict) -> dict:
    """Union the static and runtime saw-but-filtered surfaces.

    Both inputs are visible here, so this is where the narrowed denominator is
    made whole: a surface the query saw and the verb allowlist dropped (static
    side) and a scenario seen but out of scope (runtime side) are different
    origins of the same fact -- N was narrowed, visibly -- so they are kept
    together, not deduplicated. Defaults explicitly to the empty block so a side
    that declares nothing reads as zero rather than as missing.
    """
    surfaces: list[dict] = []
    for source in (capabilities, observed):
        block = source.get("excluded_surfaces") or {}
        surfaces.extend(block.get("surfaces", []))
    return {"count": len(surfaces), "surfaces": surfaces}


def _merge_unresolved(capabilities: dict, observed: dict) -> list[dict]:
    """Union the static and runtime could-not-resolve material.

    Static side: adapters that produced no surface, dynamic (non-literal) route
    paths, and the boundary obligations that are the honest limits of static
    reading. Runtime side: a probe's own unevaluable / unattributed material.
    Concatenated, never dropped -- each named entry reaches the gate.
    """
    out: list[dict] = []
    for source in (capabilities, observed):
        out.extend(source.get("unresolved") or [])
    return out


def reconcile(capabilities: dict, observed: dict) -> dict:
    entities = [e["name"] for e in capabilities["entities"]]
    static_surfaces = {s["id"] for s in capabilities["surfaces"]}
    exercised: set[str] = set()

    static_by_entity: dict[str, dict] = {}
    for cap in capabilities["capabilities"]:
        row = static_by_entity.setdefault(
            cap["entity"], {"surfaces": set(), "ops": set()}
        )
        row["surfaces"].add(cap["surface"])
        row["ops"].update(cap["operations"])

    runtime_by_entity: dict[str, dict] = {}
    unknown_surfaces: dict[str, set[str]] = {}
    direct_from_tests: dict[str, set[str]] = {}
    for binding in observed["bindings"]:
        surface = binding["surface"]
        row = runtime_by_entity.setdefault(
            binding["entity"], {"surfaces": set(), "ops": set(), "tests": set(),
                                "via_surface": False}
        )
        row["surfaces"].add(surface)
        row["ops"].update(binding["operations"])
        row["tests"].update(binding.get("tests", []))
        if surface.startswith("test:"):
            # A test reaching past every surface into the internals. That is
            # observation, not a surface, and gating on it would fail the build
            # for every unit test in the repository. It is still worth counting:
            # an entity ONLY ever reached this way has no surface at all.
            direct_from_tests.setdefault(binding["entity"], set()).add(surface[5:])
            continue
        row["via_surface"] = True
        exercised.add(surface)
        if surface not in static_surfaces:
            unknown_surfaces.setdefault(surface, set()).add(binding["entity"])

    rows = []
    for entity in sorted(set(entities) | set(runtime_by_entity)):
        s = static_by_entity.get(entity)
        r = runtime_by_entity.get(entity)
        cell = (
            "both" if s and r
            else "static_only" if s
            else "runtime_only" if r
            else "neither"
        )
        rows.append(
            {
                "entity": entity,
                "cell": cell,
                "declared": entity in entities,
                "static_surfaces": sorted(s["surfaces"]) if s else [],
                "runtime_surfaces": sorted(r["surfaces"]) if r else [],
                "static_operations": sorted(s["ops"]) if s else [],
                "runtime_operations": sorted(r["ops"]) if r else [],
                # An operation static claims and runtime never saw is an
                # untested operation on a tested entity -- the gap the
                # entity-level cell is too coarse to show.
                "untested_operations": sorted(
                    (s["ops"] if s else set()) - (r["ops"] if r else set())
                ),
                "tests": sorted(r["tests"]) if r else [],
                # Observed only from test code reaching into the internals --
                # no route, no worker, no command. Reported, not gated: it is a
                # smell about the test suite, not a missing capability.
                "reached_only_by_tests": bool(r) and not r["via_surface"],
            }
        )

    # Test observations absent from the static inventory. Keep the legacy
    # artifact key, but absence alone cannot establish historical deletion:
    # generated infrastructure and dynamic tables have the same shape here.
    orphan_tests = []
    for entity, row in sorted(runtime_by_entity.items()):
        if entity not in entities:
            for test in sorted(row["tests"]):
                orphan_tests.append({"test": test, "entity": entity})

    flow_evidence = {}
    if (
        "flows_failures" in observed
        or "flows_execution_scope" in observed
        or observed.get("derived_from", {}).get("extractor") == "capcov browser-probe"
    ):
        flow_evidence["flows_failures"] = observed.get("flows_failures", [{
            "id": "evidence",
            "reason": "browser evidence has no required-flow results; re-run observe",
        }])

    return {
        "rows": rows,
        "summary": {cell: sum(1 for r in rows if r["cell"] == cell) for cell in CELLS},
        "unknown_surfaces": [
            {"surface": surface, "entities": sorted(entities_)}
            for surface, entities_ in sorted(unknown_surfaces.items())
        ],
        "orphan_tests": orphan_tests,
        "reached_only_by_tests": sorted(
            entity for entity, row in runtime_by_entity.items() if not row["via_surface"]
        ),
        # A declared surface no exercise ever reached. The entity behind it is
        # usually still in the `both` cell, reached through some other route, so
        # the four cells cannot show this: an endpoint with no test hides behind
        # a well-tested table. Reported and not gated -- see the TDD.
        "unexercised_surfaces": sorted(
            s["id"] for s in capabilities["surfaces"]
            if s["id"] not in exercised and s.get("mounted", True)
        ),
        "unmounted_surfaces": sorted(
            s["id"] for s in capabilities["surfaces"] if not s.get("mounted", True)
        ),
        # Honest denominator (Property 3): the first-class carriers, threaded
        # discover -> reconcile -> gate. reconcile sees both inputs, so it is where
        # the static-side and runtime-side provenance are unioned. Present with an
        # explicit empty default even on the python-fastapi-sqlalchemy path (which
        # emits neither), so the gate's accounting is byte-for-byte unchanged there
        # while a promoted route/spec adapter's narrowing survives to the gate.
        "excluded_surfaces": _merge_excluded_surfaces(capabilities, observed),
        "unresolved": _merge_unresolved(capabilities, observed),
        # A passing route must not erase a failed required scenario on that route.
        **flow_evidence,
        # Repair the reconcile->coverage leak: residue and its summary were built
        # by discover and written to capabilities.json, then dropped here, so the
        # enumerated-but-unresolved call sites never reached the gate or the report.
        # Forward them from the static inventory (empty-but-present when the adapter
        # read no call graph). This is the leak NOT to replicate for the new
        # carriers above.
        "residue": capabilities.get("residue", []),
        "residue_summary": capabilities.get(
            "residue_summary", dict(_EMPTY_RESIDUE_SUMMARY)
        ),
    }
