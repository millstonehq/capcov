"""Ground certificates re-derived from either kernel's relations are identical (section 29).

``claims.static.certificate.certify`` sees only the bundle and a relation
closure; it is run over the Python kernel's rows and over Soufflé's rows for
every claim conclusion in the go_app bundle and in every static review case,
and the two certificates must be byte-identical.  ``recheck`` must accept each
certificate and reject a tampered one.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest

from capcov.claims import canonical_json
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.static import certificate
from capcov.core import fixpoint
from capcov.scip import map as scip_map
from capcov.scip import resolve

try:
    from .static_rules import go_app
    from .static_rules.adapter import case_paths, load_case, load_pack, read_json
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules import go_app
    from static_rules.adapter import case_paths, load_case, load_pack, read_json


def _compare(test: unittest.TestCase, bundle, replay_root: str, label: str):
    try:
        return compare(bundle, replay_root=replay_root)
    except DifferentialMismatch as exc:
        r = exc.result
        test.fail(f"kernels disagree on {label}; replay bundle: {r.replay_path}; "
                  f"python={r.python.operational_failure!r} souffle={r.souffle.operational_failure!r}")


def _closure_of(bundle, forbidden: set[str]) -> set[str]:
    """``forbidden`` plus every evidence id that transitively depends on it."""
    out = set(forbidden)
    changed = True
    while changed:
        changed = False
        for record in bundle.evidence:
            if record.id not in out and set(record.depends_on) & out:
                out.add(record.id); changed = True
    return out


def _conclusions(bundle, relations):
    for claim in bundle.claims:
        for row in certificate.claim_conclusions(bundle, relations, claim):
            yield claim, row


class GoAppCertificateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.replay_root = tempfile.mkdtemp(prefix="capcov-certificate-differential-")
        cls.bundle, cls.exported = go_app.go_app_bundle()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.replay_root, ignore_errors=True)

    def setUp(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        if not hasattr(type(self), "result"):
            type(self).result = _compare(self, self.bundle, self.replay_root, "go_app")

    def test_certificates_are_identical_across_engines_and_recheck(self) -> None:
        report = evaluate(self.bundle)
        by_id = {entry.claim.id: entry.result for entry in report.claims}
        known = {record.id for record in self.bundle.evidence}
        decls = {decl.name: decl for decl in self.bundle.relations}
        certified = 0
        for claim, row in _conclusions(self.bundle, self.result.python.relations):
            with self.subTest(claim=claim.id, row=canonical_json(row)):
                from_python = certificate.certify(self.bundle, self.result.python.relations, claim.relation, row)
                from_souffle = certificate.certify(self.bundle, self.result.souffle.relations, claim.relation, row)
                self.assertEqual(from_python, from_souffle)
                self.assertFalse(from_python["truncated"], from_python["truncation"])
                json.dumps(from_python)  # serializable
                self.assertTrue(set(from_python["leaves"]) <= known)
                self.assertTrue(all(leaf.startswith(("scip:", "static:", "runtime:")) for leaf in from_python["leaves"]))
                for relations in (self.result.python.relations, self.result.souffle.relations):
                    checked = certificate.recheck(self.bundle, from_python, relations)
                    self.assertTrue(checked.ok, checked.problems)
                    self.assertEqual(checked.unchecked, ())
                # A certificate is a ground proof; the evaluator's canonical
                # proof for a uniquely-derivable row must use the same leaves.
                polarity = decls[claim.relation].polarity.value
                result = by_id[claim.id]
                if claim.quantifier.value == "exists":
                    expected = result.support if polarity == "positive" else result.refutation
                    self.assertEqual(from_python["leaves"], sorted(expected))
                certified += 1
        self.assertGreaterEqual(certified, 5)

    def test_shortest_path_steps_agree_with_the_hop_relation_and_the_fixpoint(self) -> None:
        relations = dict(self.result.python.relations)
        hops = {(root, dst): hop for _, root, dst, hop in relations["static_reaches_within"]}
        to_node = resolve.normalizer(go_app.LANGUAGE).symbol_to_node
        calls = resolve.calls_graph(scip_map.call_edges(go_app.normalized()), language=go_app.LANGUAGE)
        distances = fixpoint.distances(go_app.HANDLER_NODE, calls, fixpoint_max := 64)
        self.assertEqual(fixpoint_max, certificate.DEFAULT_MAX_DEPTH)
        for index, root, dst in relations["static_reaches"]:
            with self.subTest(dst=dst):
                cert = certificate.certify(self.bundle, self.result.souffle.relations, "static_reaches",
                                           (index, root, dst))
                self.assertEqual(cert["steps"] + 1, hops[(root, dst)])
                self.assertEqual(cert["steps"] + 1, distances[to_node(dst)])
                # exactly k step applications above one base application
                node, depth = cert["derivation"], 0
                while node["rule"] == "static_reaches_step":
                    node = node["premises"][0]; depth += 1
                self.assertEqual(node["rule"], "static_reaches_base")
                self.assertEqual(depth, cert["steps"])

    def test_witnesses_and_absences_are_recorded(self) -> None:
        relations = self.result.python.relations
        [row] = certificate.claim_conclusions(self.bundle, relations,
                                              next(c for c in self.bundle.claims if c.id == "claim-runtime-route-gap"))
        cert = certificate.certify(self.bundle, relations, "runtime_route_without_static", row)
        self.assertEqual({w["relation"] for w in cert["witnesses"]},
                         {"index_describes_run", "static_route_inventory_closed"})
        self.assertEqual({w["modality"] for w in cert["witnesses"]}, {"compatibility", "completeness"})
        self.assertEqual(cert["absent"], [{"relation": "static_route_declared_surface",
                                           "row": [go_app.index_digest(), go_app.UNDECLARED_SURFACE]}])
        self.assertEqual({leaf.split(":")[0] for leaf in cert["leaves"]}, {"runtime", "static"})

    def test_tampered_certificates_are_detected_by_the_ground_recheck(self) -> None:
        relations = self.result.python.relations
        claim = next(c for c in self.bundle.claims if c.id == "claim-cap-jobs-read")
        [row] = certificate.claim_conclusions(self.bundle, relations, claim)
        cert = certificate.certify(self.bundle, relations, claim.relation, row)
        self.assertTrue(certificate.recheck(self.bundle, cert, relations).ok)

        def leaves(node):
            if node["kind"] == "fact":
                yield node
            for premise in node.get("premises", ()):
                yield from leaves(premise)

        # 1. substitute one leaf's evidence id with another real evidence id
        decoy = next(record.id for record in self.bundle.evidence if record.atom.relation == "scip_index_tree")
        tampered = copy.deepcopy(cert)
        leaf = next(n for n in leaves(tampered["derivation"]) if n["relation"] == "scip_may_reference")
        leaf["evidence"] = [decoy]
        checked = certificate.recheck(self.bundle, tampered, relations)
        self.assertFalse(checked.ok)
        self.assertTrue(any("attests" in problem for problem in checked.problems), checked.problems)
        # 2. substitute a leaf row (the edge now claims a different callee)
        tampered = copy.deepcopy(cert)
        leaf = next(n for n in leaves(tampered["derivation"]) if n["relation"] == "scip_may_reference")
        leaf["row"][2] = go_app.REGISTER
        checked = certificate.recheck(self.bundle, tampered, relations)
        self.assertFalse(checked.ok)
        # 3. substitute the conclusion
        tampered = copy.deepcopy(cert)
        tampered["conclusion"]["row"][2] = "users"
        self.assertFalse(certificate.recheck(self.bundle, tampered, relations).ok)
        # 4. an unknown rule
        tampered = copy.deepcopy(cert)
        tampered["derivation"]["rule_digest"] = "0" * 64
        self.assertFalse(certificate.recheck(self.bundle, tampered, relations).ok)
        # 5. a certificate for another bundle
        other, _ = go_app.go_app_bundle(runtime=False, hops=False)
        self.assertFalse(certificate.recheck(other, cert, relations).ok)
        # 6. a negated row smuggled into the closure
        tampered_closure = {name: list(rows) for name, rows in relations}
        tampered_closure["static_route_declared_surface"].append((go_app.index_digest(), go_app.UNDECLARED_SURFACE))
        gap_claim = next(c for c in self.bundle.claims if c.id == "claim-runtime-route-gap")
        [gap_row] = certificate.claim_conclusions(self.bundle, relations, gap_claim)
        gap = certificate.certify(self.bundle, relations, gap_claim.relation, gap_row)
        self.assertTrue(certificate.recheck(self.bundle, gap, relations).ok)
        checked = certificate.recheck(self.bundle, gap, tampered_closure)
        self.assertFalse(checked.ok)
        self.assertTrue(any("present in the closure" in problem for problem in checked.problems))
        # without a closure the absence is reported as unchecked, not as proven
        without = certificate.recheck(self.bundle, gap)
        self.assertTrue(without.ok)
        self.assertEqual(len(without.unchecked), 1)

    def test_bounds_truncate_instead_of_lying(self) -> None:
        relations = self.result.python.relations
        row = (go_app.index_digest(), go_app.GET_JOB, go_app.DB_FIRST)
        deep = certificate.certify(self.bundle, relations, "static_reaches", row)
        self.assertEqual(deep["steps"], 2)
        shallow = certificate.certify(self.bundle, relations, "static_reaches", row, max_depth=1)
        self.assertTrue(shallow["truncated"])
        self.assertIn("max_depth", shallow["truncation"])
        self.assertIsNone(shallow["derivation"])
        starved = certificate.certify(self.bundle, relations, "static_reaches", row, max_nodes=3)
        self.assertTrue(starved["truncated"])
        self.assertIn("max_nodes", starved["truncation"])
        self.assertFalse(certificate.recheck(self.bundle, shallow, relations).ok)
        with self.assertRaises(certificate.CertificateError):
            certificate.certify(self.bundle, relations, "static_reaches",
                                (go_app.index_digest(), go_app.GET_JOB, go_app.REGISTER))


class StaticCaseCertificateTest(unittest.TestCase):
    def test_every_case_certifies_identically_in_both_engines(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        pack = load_pack()
        replay_root = tempfile.mkdtemp(prefix="capcov-case-certificates-")
        certified = 0
        try:
            for path in case_paths():
                case = read_json(path)
                bundle = load_case(path, pack)
                result = _compare(self, bundle, replay_root, path.name)
                known = {record.id for record in bundle.evidence}
                for claim, row in _conclusions(bundle, result.python.relations):
                    with self.subTest(case=path.name, claim=claim.id, row=canonical_json(row)):
                        from_python = certificate.certify(bundle, result.python.relations, claim.relation, row)
                        from_souffle = certificate.certify(bundle, result.souffle.relations, claim.relation, row)
                        self.assertEqual(from_python, from_souffle)
                        self.assertFalse(from_python["truncated"])
                        for relations in (result.python.relations, result.souffle.relations):
                            checked = certificate.recheck(bundle, from_python, relations)
                            self.assertTrue(checked.ok, checked.problems)
                        leaves = set(from_python["leaves"])
                        self.assertTrue(leaves <= known)
                        table = case["expected"]["claims"][claim.id]
                        if table["semantic_verdict"] in {"supported", "refuted"}:
                            # the reviewer weighed every input a derivation may use
                            observed = set(table["observed_leaves"])
                            self.assertTrue(leaves <= observed, sorted(leaves - observed))
                        else:
                            # a closure row whose claim the review left unresolved
                            # must rest on evidence the review forbade (a rejected
                            # assumption or a row that depends on one)
                            forbidden = set(table["forbidden_leaves"])
                            self.assertTrue(leaves & _closure_of(bundle, forbidden),
                                            (claim.id, sorted(leaves), sorted(forbidden)))
                        certified += 1
        finally:
            shutil.rmtree(replay_root, ignore_errors=True)
        self.assertGreaterEqual(certified, 20)


if __name__ == "__main__":
    unittest.main()
