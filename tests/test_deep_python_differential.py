"""§4.3 -- the Python differential: the GENERIC SCIP path must equal the native
adapter, capability-set for capability-set.

The Go and PHP e2e prove the deep path resolves a chain; this proves it resolves
the RIGHT chain, by pinning it against an independent oracle. The native
``python-fastapi-sqlalchemy`` adapter resolves its call graph from Python's own
import bindings (an AST pass, no SCIP); the generic path takes the SAME entities,
surfaces, ``_direct`` and ``_ops`` but replaces the call graph with scip-python's
type-resolved edges (``resolve.resolve`` over a real scip-python index of the
fixture). If the two produce the same capability set -- keyed on
``(entity, surface, sorted(operations), evidence.hops)`` and with identical chain
node-sets -- then the SCIP resolver, the python normalizer's ``symbol_to_node``,
``calls_graph`` and the fixpoint agree with a hand-written resolver on real code.

Any disagreement is a bug in the generic path. This is the cheapest, strongest
generality proof, and -- crucially -- it is NOT sufficient on its own: Python
exercises only the one namespace form the translator always handled, so the
go/php branches are invisible to it (R6). That is why the three-indexer proof
exists; this test is the necessary half, the Go/PHP e2e the sufficient half.

It upgrades the pre-existing single-binding spot check
(``test_scip_resolve.LiveResolveTest.test_scip_resolved_graph_binds_an_entity_
through_a_call_chain``) to a full capability-set differential. It needs no
tree-sitter -- the native adapter is pure ``ast`` -- so it runs under both the
plain and the ``--extra treesitter`` suites, given scip-python + the scip CLI.
"""

from __future__ import annotations

import unittest

from capcov.adapters import python_fastapi_sqlalchemy as adapter
from capcov.core import fixpoint
from capcov.scip import resolve as scip_resolve

from .support import APP, Project

_HAVE_SCIP = scip_resolve.tools_available("python")


def _capabilities(raw: dict) -> dict:
    """cmd_discover's capability assembly, reduced to the differential key.

    Returns ``{(entity, surface, operations, hops): frozenset(chain)}`` -- the
    exact projection ``cli.cmd_discover`` builds, keyed the way §4.3 compares
    (operations sorted into a tuple, the chain as a node-set)."""
    direct, calls, ops = raw["_direct"], raw["_calls"], raw["_ops"]
    roots = [s["handler"] for s in raw["surfaces"]]
    per_root, _ = fixpoint.bind(roots, calls, direct)
    out: dict = {}
    for surface in raw["surfaces"]:
        bound = per_root.get(surface["handler"], {})
        reach = fixpoint.distances(surface["handler"], calls)
        for entity, hops in sorted(bound.items()):
            observed: set[str] = set()
            for node in reach:
                observed |= ops.get(node, {}).get(entity, set())
            chain = fixpoint.chain(surface["handler"], entity, calls, direct)
            out[(entity, surface["id"], tuple(sorted(observed)), hops)] = frozenset(chain)
    return out


@unittest.skipUnless(
    _HAVE_SCIP, "needs scip-python + the scip CLI (set SCIP_CLI)"
)
class PythonDifferentialTest(unittest.TestCase):
    """capset(generic-scip) == capset(native-ast) over the checked-in APP fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        with Project(APP) as project:
            cls.native = adapter.discover(project.source, project.root)
            cls.hybrid = scip_resolve.resolve(
                project.source, cls.native, language="python"
            )
        cls.native_caps = _capabilities(cls.native)
        cls.scip_caps = _capabilities(cls.hybrid)

    def test_capability_sets_are_identical(self) -> None:
        native, scip = set(self.native_caps), set(self.scip_caps)
        self.assertEqual(
            native, scip,
            "generic SCIP path disagrees with the native adapter\n"
            f"  native only: {sorted(native - scip)}\n"
            f"  scip only:   {sorted(scip - native)}",
        )

    def test_chain_node_sets_are_identical(self) -> None:
        # The keys match; the chains behind them must too (a chain that reaches the
        # same entity by a different path is still a disagreement).
        mismatches = {
            key for key in self.native_caps
            if self.native_caps[key] != self.scip_caps.get(key)
        }
        self.assertEqual(mismatches, set(), f"chain-set disagreements: {mismatches}")

    def test_the_differential_is_not_vacuous(self) -> None:
        # A differential that compared two empty sets would pass and prove nothing.
        # The APP fixture has a genuine multi-hop capability (get_widgets reaches
        # `widgets` two hops away through service -> repo); assert it is present on
        # BOTH sides, so the equality above is over a real, non-trivial chain.
        multihop = {
            (entity, surface, ops, hops)
            for (entity, surface, ops, hops) in self.native_caps
            if hops >= 2
        }
        self.assertTrue(multihop, "the fixture produced no multi-hop capability")
        self.assertTrue(
            multihop <= set(self.scip_caps),
            "the SCIP path did not reproduce the multi-hop capabilities",
        )
        # And a direct (hop-0) one, so both binding depths are compared.
        self.assertTrue(
            any(hops == 0 for (_e, _s, _o, hops) in self.native_caps),
            "the fixture produced no direct capability",
        )


if __name__ == "__main__":
    unittest.main()
