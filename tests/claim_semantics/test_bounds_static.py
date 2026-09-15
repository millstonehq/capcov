"""A dense synthetic static graph past the row bounds is resource-exhausted in both kernels.

``N`` route handlers on a directed ring make every handler reach every other:
``static_reaches`` and ``static_reaches_eq`` each hold ``N*N`` rows.  With the
bounds passed to ``differential.run_python`` / ``run_souffle`` both kernels
report ``resource-exhausted`` as a named operational failure -- no exception
escapes, no partial closure is admitted.  A small ring of the same shape
completes and matches, so the exhaustion is the bound, not a malformed input.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest

from capcov.claims import Atom, Claim, Constant, Context
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.differential import DifferentialMismatch, compare, run_python, run_souffle
from capcov.claims.evaluator import ResourceLimits

try:
    from .static_rules.adapter import pack_bundle
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import pack_bundle

INDEX = hashlib.sha256(b"dense-static-graph").hexdigest()
TREE = hashlib.sha256(b"dense-static-tree").hexdigest()
ROW_BOUND = 10_000


def _symbol(i: int) -> str:
    return f"scip-go gomod github.com/example/dense . `github.com/example/dense/api`/H{i}()."


def dense_ring(n: int) -> Bundle:
    pack = pack_bundle()
    decls = {decl.name: decl for decl in pack.relations}

    def atom(relation: str, *values):
        return Atom(relation, tuple(Constant(v, c.type) for v, c in zip(values, decls[relation].columns)))

    facts = [
        atom("scip_index", INDEX, "scip-go", "0.2.7", "go", "", "json"),
        atom("scip_index_tree", INDEX, TREE, 1, "**/*.go"),
        atom("source_tree_observed", TREE),
        atom("scip_document", INDEX, "api/h.go", "go", 3 * n, n, False),
        atom("static_route_inventory_closed", INDEX),
    ]
    for i in range(n):
        surface, line = f"http:GET /r{i}", 100 + i
        facts.append(atom("route_site", INDEX, surface, "api/h.go", line, f"H{i}"))
        facts.append(atom("route_handler_location", INDEX, surface, "api/h.go", line))
        facts.append(atom("scip_definition_site", INDEX, "api/h.go", line, _symbol(i)))
        facts.append(atom("scip_symbol", INDEX, _symbol(i), "Function", "callable", f"H{i}"))
        occurrence = hashlib.sha256(f"occ-{i}".encode()).hexdigest()
        facts.append(atom("scip_may_reference", INDEX, _symbol(i), _symbol((i + 1) % n), "api/h.go", line,
                          occurrence, False))
    claim = Claim("static_reaches", (Constant(INDEX, "digest"), Constant(_symbol(0), "symbol"),
                                     Constant(_symbol(n - 1), "symbol")),
                  Context.from_mapping({"index": INDEX}), id="claim-ring-reaches-last")
    return Bundle(pack.relations, facts=tuple(facts), rules=pack.rules, claims=(claim,),
                  diagnostic_policy=pack.diagnostic_policy)


class StaticBoundsTest(unittest.TestCase):
    def test_small_ring_completes_and_matches(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        bundle = dense_ring(6)
        replay_root = tempfile.mkdtemp(prefix="capcov-bounds-static-")
        try:
            result = compare(bundle, replay_root=replay_root)
        except DifferentialMismatch as exc:
            self.fail(f"kernels disagree on the small ring; replay bundle: {exc.result.replay_path}")
        finally:
            shutil.rmtree(replay_root, ignore_errors=True)
        self.assertTrue(result.matched)
        relations = dict(result.python.relations)
        self.assertEqual(len(relations["static_reaches"]), 36)
        self.assertEqual(len(relations["static_reaches_eq"]), 36)
        [claim] = result.python.claims
        self.assertEqual(claim.semantic, "supported")

    def test_dense_ring_past_the_bound_is_resource_exhausted_in_both_kernels(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        n = 120  # 120*120 static_reaches rows alone exceed ROW_BOUND
        bundle = dense_ring(n)
        self.assertLess(len(bundle.facts), ROW_BOUND)
        python = run_python(bundle, limits=ResourceLimits(max_derived_rows=ROW_BOUND, max_provenance=4 * ROW_BOUND))
        self.assertEqual(python.operational_failure, "resource-exhausted", python.message)
        self.assertTrue(python.message)
        souffle = run_souffle(bundle, max_rows=ROW_BOUND)
        self.assertEqual(souffle.operational_failure, "resource-exhausted", souffle.message)
        self.assertIn(str(ROW_BOUND), souffle.message)
        # an exhausted pair is never admitted as agreement, even when both fail the same way
        from capcov.claims.differential import reports_match
        self.assertFalse(reports_match(python, souffle))


if __name__ == "__main__":
    unittest.main()
