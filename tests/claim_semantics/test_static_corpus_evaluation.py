"""The one place the static review tables meet an engine.

Every reviewed expectation is compared with the Python reference evaluator and,
when Souffle is on PATH, both kernels are compared through the fail-closed
differential.  A disagreement is a finding to investigate, never a reason to
edit the expectation in place.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

try:
    from .static_rules.adapter import case_paths, load_case, load_pack, read_json
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import case_paths, load_case, load_pack, read_json

from capcov.claims import canonical_json
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.output import VerifiedProofEvidence, render_outputs

SURFACE = "GET /jobs/{id}"


def sorted_json(values):
    return sorted((canonical_json(value) for value in values))


class PythonEvaluatorAgreesWithReviewedExpectations(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()

    def test_every_claim_matches_its_reviewed_table(self) -> None:
        for path in case_paths():
            case = read_json(path)
            bundle = load_case(path, self.pack)
            report = evaluate(bundle)
            with self.subTest(case=path.name):
                self.assertEqual(report.status.value, "complete", report.message)
                self.assertEqual({entry.claim.id for entry in report.claims}, set(case["expected"]["claims"]))
            evidence_ids = {record.id for record in bundle.evidence}
            for entry in report.claims:
                expected = case["expected"]["claims"][entry.claim.id]
                result = entry.result
                with self.subTest(case=path.name, claim=entry.claim.id):
                    self.assertEqual(result.semantic.value, expected["semantic_verdict"])
                    self.assertEqual(result.operational.value, expected["operational_status"])
                    self.assertEqual(sorted(result.support), expected["support_leaves"])
                    self.assertEqual(sorted(result.refutation), expected["refutation_leaves"])
                    self.assertEqual(sorted_json(result.missing_premises), sorted_json(expected["missing_premises"]))
                    leaves = set(result.support) | set(result.refutation)
                    proof = VerifiedProofEvidence.from_bundle(bundle, entry.claim.id, leaves) if leaves else None
                    rendered = render_outputs(bundle, entry.claim.id, evidence_ids, proof,
                                              claim_state=result.semantic.value)
                    discrepancies = [item["fields"] for item in rendered if item["kind"] == "discrepancy"]
                    self.assertEqual(sorted_json(discrepancies), sorted_json(expected["discrepancies"]))
                    basis = "bounded-history-model" if entry.claim.quantifier.value == "forall" else "derivational"
                    self.assertEqual(result.basis.value, basis)

    def test_control_case_derives_exactly_the_go_app_capability_rows(self) -> None:
        path = next(p for p in case_paths() if p.stem == "00-go-app-control")
        case = read_json(path)
        report = evaluate(load_case(path, self.pack))
        index = case["context"]["index"]
        self.assertEqual(set(report.relation_rows("static_capability_op")),
                         {(index, SURFACE, "jobs", "read"), (index, SURFACE, "audit_logs", "create")})
        self.assertEqual(set(report.relation_rows("static_capability")),
                         set(report.relation_rows("static_capability_op")))
        self.assertEqual(set(report.relation_rows("static_route_declared_surface")), {(index, SURFACE)})
        self.assertEqual(set(report.relation_rows("runtime_route_without_static")),
                         {("tenant-a", "POST /jobs", "run-1", index)})
        self.assertEqual(report.relation_rows("static_route_authorization_gap"), ())
        self.assertEqual(len(report.relation_rows("static_reaches")), 4)

    def test_stale_case_derives_scip_index_stale_and_not_static_index_current(self) -> None:
        path = next(p for p in case_paths() if p.stem == "05-stale-index")
        report = evaluate(load_case(path, self.pack))
        self.assertEqual(report.relation_rows("static_index_current"), ())
        self.assertEqual(len(report.relation_rows("scip_index_stale")), 1)
        self.assertEqual(report.relation_rows("static_capability_op"), ())
        self.assertEqual(len(report.relation_rows("static_path_to_storage")), 2)


@unittest.skipIf(shutil.which("souffle") is None, "souffle is not on PATH; run inside the nix devShell")
class SouffleAgreesWithPythonOnEveryStaticCase(unittest.TestCase):
    def test_kernels_agree_on_closure_and_verdicts(self) -> None:
        pack = load_pack()
        replay_root = tempfile.mkdtemp(prefix="capcov-static-differential-")
        try:
            for path in case_paths():
                bundle = load_case(path, pack)
                with self.subTest(case=path.name):
                    try:
                        result = compare(bundle, replay_root=replay_root)
                    except DifferentialMismatch as exc:
                        self.fail(f"kernels disagree on {path.name}; replay bundle: {exc.result.replay_path}; "
                                  f"python={exc.result.python.operational_failure!r} "
                                  f"souffle={exc.result.souffle.operational_failure!r} {exc.result.souffle.message[:400]}")
                    self.assertTrue(result.matched)
                    self.assertIsNone(result.python.operational_failure)
                    self.assertIsNone(result.souffle.operational_failure)
                    self.assertEqual(result.python.canonical_digest, result.souffle.canonical_digest)
        finally:
            shutil.rmtree(replay_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
