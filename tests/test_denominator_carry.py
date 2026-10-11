"""The honest denominator, end to end through the CORE path.

Property 3 is the one most at risk in the consolidation: the generic route/spec
discovery knows things it cannot resolve -- surfaces a verb allowlist dropped,
dynamic route paths, boundary limits, an empty document -- and if that material
dies at the reconcile or gate boundary, N shrinks silently and a green gate lies.

These tests drive `discover -> reconcile -> gate` through the real core reconcile
and gate. The `discover` half is expressed as the capabilities artifact the
promoted route/spec adapters are DOCUMENTED to emit (design 1.2 / 2): a surface
obligation bound to itself, plus the first-class `excluded_surfaces` / `unresolved`
carriers. (The promoted adapters themselves are another task's files; this pins
the core contract they must produce to, so it runs on this branch and identically
under `--extra treesitter`, exercising core, not tree-sitter.)

The load-bearing assertions:
  - the carriers survive into coverage (not dropped at the reconcile boundary);
  - an unexempted unresolved obligation of surface/spec kind MOVES the gate;
  - excluded_surfaces are reported, never auto-failed (legitimate narrowing);
  - an empty route table / empty OpenAPI doc has NO green denominator through the
    core path -- the surviving analogue of the removed flows zoho guard; and,
    shown explicitly, dropping the carrier is exactly the false green it prevents.
"""

import tempfile
import unittest
from pathlib import Path

from capcov.core.gate import gate
from capcov.core.reconcile import reconcile

ROUTE = "http:GET /jobs/{id}"

_ZERO_RESIDUE = {
    "resolved_by_import": 0, "resolved_by_name": 0, "ambiguous": 0,
    "external": 0, "chained": 0, "builtin_shadowed": 0,
}


def route_capabilities(
    *,
    excluded: dict | None = None,
    unresolved: list[dict] | None = None,
    entities: list[dict] | None = None,
    surfaces: list[dict] | None = None,
    caps: list[dict] | None = None,
) -> dict:
    """The capabilities artifact a promoted route adapter is documented to emit:
    one surface obligation that is its own entity and surface (design 1.2), plus
    empty-but-present core keys and the honest-denominator carriers."""
    return {
        "entities": entities if entities is not None else [{"name": ROUTE}],
        "surfaces": surfaces if surfaces is not None else [
            {"id": ROUTE, "handler": ROUTE, "mounted": True}
        ],
        "capabilities": caps if caps is not None else [
            {
                "entity": ROUTE, "surface": ROUTE, "operations": ["read"],
                "evidence": {"hops": 0, "chain": [ROUTE], "kind": "direct"},
            }
        ],
        "blind_spots": [],
        "residue": [],
        "residue_summary": dict(_ZERO_RESIDUE),
        "excluded_surfaces": excluded if excluded is not None
        else {"count": 0, "surfaces": []},
        "unresolved": unresolved if unresolved is not None else [],
    }


def route_observed(
    *,
    bindings: list[dict] | None = None,
    excluded: dict | None = None,
    unresolved: list[dict] | None = None,
) -> dict:
    return {
        "bindings": bindings if bindings is not None else [
            {
                "surface": ROUTE, "entity": ROUTE, "operations": ["read"],
                "tests": ["0:GET /jobs/{id}"],
            }
        ],
        "excluded_surfaces": excluded if excluded is not None
        else {"count": 0, "surfaces": []},
        "unresolved": unresolved if unresolved is not None else [],
    }


def dropped_ws() -> dict:
    return {
        "count": 1,
        "surfaces": [{
            "file": "api.py", "line": 9, "method": "WEBSOCKET", "path": "/ws",
            "handler": "ws", "reason": "verb not in the recognised HTTP verb set",
        }],
    }


def dynamic_route() -> dict:
    return {
        "adapter": "treesitter-routes", "kind": "dynamic-route",
        "id": "route:dynamic:api.py:42",
        "reason": "route path is a non-literal expression; unreadable statically",
    }


class CarriersSurviveAndMoveTheGate(unittest.TestCase):
    def rules(self, failures: list) -> list[str]:
        return sorted(f.rule for f in failures)

    def test_excluded_and_unresolved_survive_into_coverage(self) -> None:
        coverage = reconcile(
            route_capabilities(excluded=dropped_ws(), unresolved=[dynamic_route()]),
            route_observed(),
        )
        self.assertEqual(coverage["excluded_surfaces"], dropped_ws())
        self.assertEqual(coverage["unresolved"], [dynamic_route()])
        # the route itself is genuinely covered; the carriers ride alongside it.
        self.assertEqual(coverage["summary"]["both"], 1)

    def test_an_unexempted_unresolved_obligation_moves_the_gate(self) -> None:
        coverage = reconcile(
            route_capabilities(excluded=dropped_ws(), unresolved=[dynamic_route()]),
            route_observed(),
        )
        failures = gate(coverage, None)
        # only the unresolved obligation fails -- excluded_surfaces do not.
        self.assertEqual(self.rules(failures), ["unresolved-obligation"])
        self.assertEqual(failures[0].subject, "route:dynamic:api.py:42")

    def test_excluded_surfaces_are_reported_not_auto_failed(self) -> None:
        """Removing the unresolved obligation leaves the gate green even though a
        surface was excluded: verb narrowing is legitimate; N stays legible."""
        coverage = reconcile(
            route_capabilities(excluded=dropped_ws()), route_observed()
        )
        self.assertEqual(coverage["excluded_surfaces"]["count"], 1)
        self.assertEqual(gate(coverage, None), [])

    def test_the_unresolved_obligation_is_what_moved_the_gate(self) -> None:
        """The delta made explicit: same inventory, with vs without the carrier."""
        with_it = reconcile(
            route_capabilities(unresolved=[dynamic_route()]), route_observed()
        )
        without_it = reconcile(route_capabilities(), route_observed())
        self.assertEqual(self.rules(gate(with_it, None)), ["unresolved-obligation"])
        self.assertEqual(gate(without_it, None), [])


class UnresolvedExemption(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.coverage = reconcile(
            route_capabilities(unresolved=[dynamic_route()]), route_observed()
        )

    def exemptions(self, text: str) -> Path:
        path = Path(self.dir.name) / "exemptions.toml"
        path.write_text(text)
        return path

    def rules(self, failures: list) -> list[str]:
        return sorted(f.rule for f in failures)

    def test_a_dated_reasoned_exemption_waives_it(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nsurface="route:dynamic:api.py:42"\ncell="unresolved"\n'
            'date="2026-09-13"\nreason="path is a request param; the catch-all '
            'handler test covers every value."\n'
        )
        self.assertEqual(gate(self.coverage, path), [])

    def test_an_exemption_filed_under_the_wrong_cell_is_stale(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nsurface="route:dynamic:api.py:42"\ncell="static_only"\n'
            'date="2026-09-13"\nreason="misfiled."\n'
        )
        self.assertEqual(self.rules(gate(self.coverage, path)), ["stale-exemption"])

    def test_an_exemption_naming_nothing_present_is_unused(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nsurface="route:dynamic:gone.py:1"\ncell="unresolved"\n'
            'date="2026-09-13"\nreason="an obligation that no longer exists."\n'
        )
        # the live obligation still fails; the exemption names itself for deletion.
        self.assertEqual(
            self.rules(gate(self.coverage, path)),
            ["unresolved-obligation", "unused-exemption"],
        )


class EmptyInputHasNoGreenDenominator(unittest.TestCase):
    """The surviving analogue of the removed flows zoho guard, on the core path."""

    def rules(self, failures: list) -> list[str]:
        return sorted(f.rule for f in failures)

    def empty_with_carrier(self, unresolved: list[dict]) -> dict:
        return reconcile(
            route_capabilities(entities=[], surfaces=[], caps=[], unresolved=unresolved),
            route_observed(bindings=[]),
        )

    def test_empty_route_table_has_no_green_denominator(self) -> None:
        coverage = self.empty_with_carrier([{
            "adapter": "treesitter-routes", "kind": "treesitter-routes",
            "reason": "the route query matched no literal-path routes",
        }])
        self.assertEqual(coverage["summary"]["both"], 0)
        self.assertEqual(self.rules(gate(coverage, None)), ["unresolved-obligation"])

    def test_empty_openapi_document_has_no_green_denominator(self) -> None:
        coverage = self.empty_with_carrier([{
            "adapter": "structured-spec", "kind": "structured-spec",
            "reason": "the OpenAPI document declared no paths",
        }])
        self.assertEqual(coverage["summary"]["both"], 0)
        self.assertEqual(self.rules(gate(coverage, None)), ["unresolved-obligation"])

    def test_dropping_the_carrier_is_the_false_green_this_guards(self) -> None:
        """Precisely the regression 2 forbids: a naive promotion that drops the
        unresolved carrier turns an empty target into a passing gate."""
        naive = reconcile(
            route_capabilities(entities=[], surfaces=[], caps=[]),
            route_observed(bindings=[]),
        )
        self.assertEqual(naive["summary"], {"both": 0, "static_only": 0,
                                            "runtime_only": 0, "neither": 0})
        self.assertEqual(gate(naive, None), [])  # the lie the carrier prevents


class DenominatorUnionAcrossSides(unittest.TestCase):
    def rules(self, failures: list) -> list[str]:
        return sorted(f.rule for f in failures)

    def test_static_and_runtime_carriers_are_unioned(self) -> None:
        capabilities = route_capabilities(
            excluded=dropped_ws(),
            unresolved=[{
                "adapter": "treesitter-routes", "kind": "boundary",
                "id": "boundary:file:api.py",
                "reason": "module could not be parsed; its routes are unread",
            }],
        )
        observed = route_observed(
            excluded={"count": 1, "surfaces": [{
                "file": None, "line": None, "method": "GET", "path": "/health",
                "handler": "health", "reason": "scenario out of scope at runtime",
            }]},
            unresolved=[{
                "adapter": "browser", "kind": "assertion-unevaluable",
                "reason": "a scenario assertion could not be evaluated",
                "gating": False,
            }],
        )
        coverage = reconcile(capabilities, observed)
        self.assertEqual(coverage["excluded_surfaces"]["count"], 2)
        self.assertEqual(len(coverage["unresolved"]), 2)

    def test_runtime_side_reported_only_material_does_not_fail_the_gate(self) -> None:
        """A probe's own unevaluable/unattributed material is carried for
        legibility (gating=False) but is not a static surface/spec obligation, so
        the static boundary is the only thing that moves the gate."""
        capabilities = route_capabilities(unresolved=[{
            "adapter": "treesitter-routes", "kind": "boundary",
            "id": "boundary:file:api.py",
            "reason": "module could not be parsed",
        }])
        observed = route_observed(unresolved=[{
            "adapter": "load", "kind": "sample-unattributed",
            "reason": "a load sample could not be attributed to a surface",
            "gating": False,
        }])
        coverage = reconcile(capabilities, observed)
        # both carried into coverage...
        self.assertEqual(len(coverage["unresolved"]), 2)
        # ...but only the static boundary gates.
        failures = gate(coverage, None)
        self.assertEqual(self.rules(failures), ["unresolved-obligation"])
        self.assertEqual(failures[0].subject, "boundary:file:api.py")


if __name__ == "__main__":
    unittest.main()
