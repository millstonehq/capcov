"""Every adversarial static case yields its reviewed verdict in both kernels (section 29).

Unlike ``test_static_corpus_evaluation`` this test never skips: Soufflé is a
precondition (the gate rejects "skipped" output).  Per case it runs the
fail-closed differential, then checks the Python and the Soufflé claim results
against ``expected.json`` (verdict, operational status, canonical missing
premises), the Python kernel's leaves against the reviewed leaf sets, and that
the certificates re-derived from both closures agree.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from capcov.claims import canonical_json
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.static import certificate

try:
    from .static_rules.adapter import EXPECTED_PATH, case_paths, load_case, load_pack, read_json
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import EXPECTED_PATH, case_paths, load_case, load_pack, read_json


def sorted_json(values):
    return sorted(canonical_json(value) for value in values)


class AdversarialCasesInBothEngines(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = load_pack()
        cls.table = read_json(EXPECTED_PATH)["cases"]
        cls.replay_root = tempfile.mkdtemp(prefix="capcov-static-adversarial-")
        cls.results = {}
        cls.mismatches = {}
        if shutil.which("souffle") is None:
            return
        for path in case_paths():
            bundle = load_case(path, cls.pack)
            try:
                cls.results[path.stem] = (bundle, compare(bundle, replay_root=cls.replay_root))
            except DifferentialMismatch as exc:
                cls.mismatches[path.stem] = exc.result

    @classmethod
    def tearDownClass(cls) -> None:
        if not cls.mismatches:
            shutil.rmtree(cls.replay_root, ignore_errors=True)

    def test_souffle_is_present_not_skipped(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")

    def test_kernels_agree_on_every_case(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        for stem, result in sorted(self.mismatches.items()):
            with self.subTest(case=stem):
                self.fail(f"kernels disagree on {stem}; replay bundle: {result.replay_path}; "
                          f"python={result.python.operational_failure!r} "
                          f"souffle={result.souffle.operational_failure!r} {result.souffle.message[:400]}")
        self.assertEqual(set(self.results), set(self.table))
        for stem, (_, result) in sorted(self.results.items()):
            with self.subTest(case=stem):
                self.assertTrue(result.matched)
                self.assertEqual(result.python.canonical_digest, result.souffle.canonical_digest)

    def test_both_kernels_yield_the_reviewed_verdict_status_and_missing_premises(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        for stem, (bundle, result) in sorted(self.results.items()):
            expected = self.table[stem]["claims"]
            for report in (result.python, result.souffle):
                self.assertEqual({claim.key for claim in report.claims}, set(expected))
                for claim in report.claims:
                    table = expected[claim.key]
                    with self.subTest(case=stem, kernel=report.backend, claim=claim.key):
                        self.assertEqual(claim.semantic, table["semantic_verdict"])
                        self.assertEqual(claim.operational, table["operational_status"])
                        self.assertEqual(sorted(claim.missing_premises), sorted_json(table["missing_premises"]))
                        quantifier = next(c.quantifier.value for c in bundle.claims if c.id == claim.key)
                        self.assertEqual(claim.basis, "bounded-history-model" if quantifier == "forall"
                                         else "derivational")

    def test_python_leaves_match_the_reviewed_leaf_sets(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        for stem, (bundle, _) in sorted(self.results.items()):
            report = evaluate(bundle)
            self.assertEqual(report.status.value, "complete", report.message)
            for entry in report.claims:
                table = self.table[stem]["claims"][entry.claim.id]
                with self.subTest(case=stem, claim=entry.claim.id):
                    self.assertEqual(sorted(entry.result.support), table["support_leaves"])
                    self.assertEqual(sorted(entry.result.refutation), table["refutation_leaves"])

    def test_duplicate_definitions_need_an_entity_category_in_both_kernels(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        get_job = "scip-go gomod github.com/example/jobsvc . api/Handler#GetJob()."
        _, callable_case = self.results["09-duplicate-definitions"]
        _, package_case = self.results["09-duplicate-definitions-package-symbol"]
        for report in (callable_case.python, callable_case.souffle):
            rows = dict(report.relations)["scip_duplicate_definition"]
            with self.subTest(case="09-duplicate-definitions", kernel=report.backend):
                self.assertEqual({row[1] for row in rows}, {get_job})
                self.assertEqual({(row[2], row[4]) for row in rows},
                                 {("api/jobs.go", "api/jobs_legacy.go"), ("api/jobs_legacy.go", "api/jobs.go")})
                self.assertEqual(dict(report.relations)["scip_references_closed"], ())
        for report in (package_case.python, package_case.souffle):
            relations = dict(report.relations)
            with self.subTest(case="09-duplicate-definitions-package-symbol", kernel=report.backend):
                # the package symbol api/ is defined in two files but has category other
                self.assertEqual(relations["scip_duplicate_definition"], ())
                self.assertEqual(len(relations["scip_references_closed"]), 6)
                self.assertEqual(len(relations["static_route_authorized_closed"]), 1)
                self.assertEqual(len(relations["static_route_authorization_gap"]), 1)

    def test_certificates_from_both_closures_agree_on_every_derived_claim_row(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        certified = 0
        for stem, (bundle, result) in sorted(self.results.items()):
            known = {record.id for record in bundle.evidence}
            for claim in bundle.claims:
                for row in certificate.claim_conclusions(bundle, result.python.relations, claim):
                    with self.subTest(case=stem, claim=claim.id, row=canonical_json(row)):
                        from_python = certificate.certify(bundle, result.python.relations, claim.relation, row)
                        from_souffle = certificate.certify(bundle, result.souffle.relations, claim.relation, row)
                        self.assertEqual(from_python, from_souffle)
                        self.assertFalse(from_python["truncated"])
                        self.assertTrue(certificate.recheck(bundle, from_python, result.souffle.relations).ok)
                        self.assertTrue(set(from_python["leaves"]) <= known)
                        certified += 1
        self.assertGreaterEqual(certified, 20)


if __name__ == "__main__":
    unittest.main()
