"""static_capability_op on go_app equals the stdlib fixpoint resolver's capabilities (section 29).

Resolver path (no tree-sitter, no subprocess):
``scip.map.call_edges(normalized)`` -> ``scip.resolve.calls_graph(edges, language="go")``
-> ``core.fixpoint.bind(roots, calls, direct)`` with ``direct`` built from the
same op sites the exporter received.  The Datalog side is the Python kernel's
closure of the go_app bundle mapped to fixpoint node ids through
``scip_symbol_node``.  Every observed difference is classified; only the
classes in ``LEGITIMATE_DIFFERENCES`` are admitted, and the observed set must
equal ``EXPECTED_DIFFERENCES`` exactly -- anything else is a
``differential-mismatch``.
"""
from __future__ import annotations

import unittest

from capcov.claims.evaluator import evaluate
from capcov.core import fixpoint
from capcov.scip import map as scip_map
from capcov.scip import resolve

try:
    from .static_rules import go_app
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules import go_app

# Difference classes that are legitimate by construction, each with the reason
# a reviewer accepts it.  A difference that fits none of these is a mismatch.
LEGITIMATE_DIFFERENCES = {
    "unrooted-endpoint": (
        "the Datalog side keeps a symbol-level edge whose endpoint has no scip_symbol_node "
        "(local, parameter, unknown scheme); calls_graph cannot root it and drops the edge"),
    "module-scope-reference": (
        "a reference with no enclosing definition is scip_module_scope_reference on the Datalog side "
        "and a caller=None edge the fixpoint consumer skips; neither side roots it"),
}

# What go_app actually exhibits on the stdlib path: nothing.  Both sides read
# the same reference occurrences (map.reference_edges(..., "callable")), so the
# function-as-value reference Register -> GetJob is an edge on BOTH sides; it
# would be a difference against the tree-sitter AST call resolver (which sees
# no call there), and that comparison is skip-guarded and not a gate.
EXPECTED_DIFFERENCES: frozenset[tuple[str, str, str]] = frozenset()


def classify(kind: str, datalog_only: bool, endpoint_rooted: bool) -> str:
    """Name the class of one difference; unknown shapes are mismatches."""
    if kind == "edge" and datalog_only and endpoint_rooted is None:
        return "module-scope-reference"
    if kind == "edge" and datalog_only and endpoint_rooted is False:
        return "unrooted-endpoint"
    return "differential-mismatch"


class FixpointCrossCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle, cls.exported = go_app.go_app_bundle(runtime=False, hops=False)
        report = evaluate(cls.bundle)
        assert report.status.value == "complete", report.message
        cls.relations = {name: set(rows) for name, rows in report.relations}
        cls.normalized = go_app.normalized()
        cls.to_node = resolve.normalizer(go_app.LANGUAGE).symbol_to_node
        cls.node_of = {symbol: node for _, symbol, node in cls.relations["scip_symbol_node"]}
        cls.unrooted = {symbol for _, symbol, _ in cls.relations["scip_symbol_unrooted"]}
        # --- resolver side --------------------------------------------------
        cls.edges = scip_map.call_edges(cls.normalized)
        cls.calls = resolve.calls_graph(cls.edges, language=go_app.LANGUAGE)
        op_sites = go_app.ast_raw()["op_sites"]
        cls.direct: dict[str, set[str]] = {}
        for owner, op in zip(scip_map.site_owners(cls.normalized, op_sites, line_base=1), op_sites):
            node = cls.to_node(owner["owner"])
            assert node is not None, owner
            cls.direct.setdefault(node, set()).add(op["entity"])
        cls.roots = sorted({cls.node_of[handler] for _, _, handler in cls.relations["static_route_handler"]})
        cls.per_root, cls.history = fixpoint.bind(cls.roots, cls.calls, cls.direct)

    # --- helpers -------------------------------------------------------------

    def datalog_capabilities(self) -> dict[str, set[str]]:
        handler_of = {surface: handler for _, surface, handler in self.relations["static_route_handler"]}
        out: dict[str, set[str]] = {root: set() for root in self.roots}
        for _, surface, entity, _verb in self.relations["static_capability_op"]:
            out[self.node_of[handler_of[surface]]].add(entity)
        return out

    def datalog_reach(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {root: set() for root in self.roots}
        for _, src, dst in self.relations["static_reaches"]:
            out[self.node_of[src]].add(self.node_of.get(dst, dst))
        return out

    def differences(self) -> set[tuple[str, str, str]]:
        found: set[tuple[str, str, str]] = set()
        # capabilities per root
        datalog = self.datalog_capabilities()
        for root in self.roots:
            for entity in datalog[root] ^ set(self.per_root.get(root, {})):
                found.add(("capability", f"{root}->{entity}", classify("capability", entity in datalog[root], True)))
        # reachable nodes per root
        reach = self.datalog_reach()
        for root in self.roots:
            resolver = set(fixpoint.distances(root, self.calls)) - {root}
            for node in reach[root] ^ resolver:
                found.add(("reach", f"{root}->{node}", classify("reach", node in reach[root], True)))
        # edges
        datalog_edges = {(src, dst) for _, src, dst in self.relations["static_edge"]}
        resolver_edges = {(self.to_node(e["caller"]), self.to_node(e["callee"])) for e in self.edges}
        rooted = {(self.node_of.get(s, s), self.node_of.get(d, d)) for s, d in datalog_edges}
        for src, dst in rooted ^ resolver_edges:
            datalog_only = (src, dst) in rooted and (src, dst) not in resolver_edges
            symbols = next(((s, d) for s, d in datalog_edges if (self.node_of.get(s, s), self.node_of.get(d, d)) == (src, dst)), None)
            endpoint_rooted = None if src is None else not (symbols and (symbols[0] in self.unrooted or symbols[1] in self.unrooted))
            found.add(("edge", f"{src}->{dst}", classify("edge", datalog_only, endpoint_rooted)))
        return found

    # --- assertions -----------------------------------------------------------

    def test_resolver_chain_is_the_documented_two_hop_chain(self) -> None:
        self.assertEqual(self.roots, [go_app.HANDLER_NODE])
        self.assertEqual(self.per_root, {go_app.HANDLER_NODE: {"jobs": 2, "audit_logs": 2}})
        self.assertEqual(self.history, [0, 0, 2, 2])
        self.assertEqual(fixpoint.chain(go_app.HANDLER_NODE, "jobs", self.calls, self.direct),
                         [go_app.HANDLER_NODE, f"{go_app.MODULE}/internal/service:Service.Fetch",
                          f"{go_app.MODULE}/internal/jobs:Repo.Get"])
        self.assertEqual(fixpoint.chain(go_app.HANDLER_NODE, "audit_logs", self.calls, self.direct),
                         [go_app.HANDLER_NODE, f"{go_app.MODULE}/internal/service:Service.Fetch",
                          f"{go_app.MODULE}/internal/jobs:Repo.Write"])

    def test_capabilities_equal_modulo_the_enumerated_differences(self) -> None:
        self.assertEqual(self.datalog_capabilities(), {root: set(bound) for root, bound in self.per_root.items()})
        self.assertEqual(self.datalog_capabilities(), {go_app.HANDLER_NODE: {"jobs", "audit_logs"}})
        verbs = {(entity, verb) for _, _, entity, verb in self.relations["static_capability_op"]}
        self.assertEqual(verbs, {("jobs", "read"), ("audit_logs", "create")})
        observed = self.differences()
        mismatches = {item for item in observed if item[2] not in LEGITIMATE_DIFFERENCES}
        self.assertEqual(mismatches, set(), f"differential-mismatch: {sorted(mismatches)}")
        self.assertEqual(observed, set(EXPECTED_DIFFERENCES))

    def test_reach_and_edges_agree_including_the_function_as_value_reference(self) -> None:
        reach = self.datalog_reach()[go_app.HANDLER_NODE]
        self.assertEqual(reach, set(fixpoint.distances(go_app.HANDLER_NODE, self.calls)) - {go_app.HANDLER_NODE})
        self.assertEqual(reach, {f"{go_app.MODULE}/internal/service:Service.Fetch",
                                 f"{go_app.MODULE}/internal/jobs:Repo.Get", f"{go_app.MODULE}/internal/jobs:Repo.Write",
                                 f"{go_app.MODULE}/gorm:DB.First", f"{go_app.MODULE}/gorm:DB.Create"})
        register = f"{go_app.MODULE}/api:Register"
        self.assertIn(go_app.HANDLER_NODE, self.calls[register])
        self.assertIn((go_app.REGISTER, go_app.GET_JOB), {(s, d) for _, s, d in self.relations["static_edge"]})
        self.assertEqual(len(self.relations["static_edge"]), len(self.edges))
        self.assertEqual(self.relations["scip_module_scope_reference"], set())

    def test_hop_counts_agree_with_the_fixpoint_distances(self) -> None:
        distances = fixpoint.distances(go_app.HANDLER_NODE, self.calls)
        for entity, hop in self.per_root[go_app.HANDLER_NODE].items():
            owners = [node for node, entities in self.direct.items() if entity in entities]
            self.assertEqual(hop, min(distances[node] for node in owners))

    def test_classifier_admits_only_the_enumerated_classes(self) -> None:
        self.assertEqual(classify("edge", True, False), "unrooted-endpoint")
        self.assertEqual(classify("edge", True, None), "module-scope-reference")
        self.assertEqual(classify("edge", True, True), "differential-mismatch")
        self.assertEqual(classify("edge", False, True), "differential-mismatch")
        self.assertEqual(classify("capability", True, True), "differential-mismatch")
        self.assertEqual(classify("reach", False, True), "differential-mismatch")
        self.assertEqual(set(LEGITIMATE_DIFFERENCES), {"unrooted-endpoint", "module-scope-reference"})
        self.assertTrue(all(reason for reason in LEGITIMATE_DIFFERENCES.values()))


if __name__ == "__main__":
    unittest.main()
