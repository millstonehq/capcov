"""Every adversarial replay case yields its reviewed verdict in both kernels (Phase 4, judge side).

Unlike ``test_replay_corpus_evaluation`` this test never skips: Soufflé is a
precondition (the gate rejects "skipped" output).  Per case it runs the
fail-closed differential, then checks the Python and the Soufflé claim results
against ``expected.json`` (verdict, operational status, canonical missing
premises), the Python kernel's leaves against the reviewed leaf sets, and that
the certificates re-derived from both closures agree for every claim row.
The rejected case is evaluated *unvalidated* in the Python engine to show that
the producer-class check is the only thing standing between it and a
supported verdict.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from capcov.claims import canonical_json, validate_bundle
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import ResourceLimits, _Engine, evaluate
from capcov.claims.static import certificate

try:
    from .replay_rules.adapter import (EXPECTED_PATH, REJECTED_DIR, REJECTED_PATH, case_paths, load_case,
                                       load_pack, read_json)
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules.adapter import (EXPECTED_PATH, REJECTED_DIR, REJECTED_PATH, case_paths, load_case,
                                      load_pack, read_json)


def sorted_json(values):
    return sorted(canonical_json(value) for value in values)


class AdversarialReplayCasesInBothEngines(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = load_pack()
        cls.table = read_json(EXPECTED_PATH)["cases"]
        cls.replay_root = tempfile.mkdtemp(prefix="capcov-replay-adversarial-")
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
        for stem, (_, result) in sorted(self.results.items()):
            expected = self.table[stem]["claims"]
            for report in (result.python, result.souffle):
                self.assertEqual({claim.key for claim in report.claims}, set(expected))
                for claim in report.claims:
                    table = expected[claim.key]
                    with self.subTest(case=stem, kernel=report.backend, claim=claim.key):
                        self.assertEqual(claim.semantic, table["semantic_verdict"])
                        self.assertEqual(claim.operational, table["operational_status"])
                        self.assertEqual(sorted(claim.missing_premises), sorted_json(table["missing_premises"]))
                        self.assertEqual(claim.basis, "derivational")

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

    def test_both_closures_carry_the_same_qualification_rows(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        qualified = {"00-positive-control": 3, "01-planted-disagreement": 2, "02-planted-undeclared-write": 2,
                     "03-surviving-mutant": 2, "04-missing-model-witness": 0, "05-missing-snapshot-witness": 0,
                     "06-stale-replay": 0, "08-lying-closure": 0, "09-missing-post-state": 2,
                     "10-missing-effects-closure": 0, "11-missing-admissible-closure": 0,
                     "13-excluded-undeclared-write": 3, "14-exclusions-not-closed": 0, "15-no-exclusions-no-closure": 0,
                     # the ordering, repeat-delete and cross-run shapes
                     # 18: the repeat's write blocks its own op only (repeat_delete_any is joined
                     # on the violating request), so create and close still qualify
                     "17-effect-order-violation": 2, "18-repeat-delete-with-effects": 2, "19-unstable-oracle": 0,
                     "20-missing-stability-closure": 0, "21-missing-effect-seq-closure": 0,
                     "23-repeat-delete-excluded-write": 3, "24-repeat-before-the-commit": 3,
                     "25-first-delete-not-committed": 2, "26-unstable-on-one-side": 0,
                     # the Stage D well-formedness premise: each half withheld in turn
                     "27-model-not-well-formed": 0, "28-well-formed-other-model": 0,
                     "29-checker-not-admitted": 0,
                     # the completeness half of the cross-request gate
                     "31-missing-response-closure": 0}
        # one derived row per shape the new rules are there to catch, in both kernels
        planted = {"17-effect-order-violation": ("effect_order_violation", 1),
                   "18-repeat-delete-with-effects": ("repeat_delete_violation", 1),
                   "19-unstable-oracle": ("oracle_unstable", 1),
                   "26-unstable-on-one-side": ("oracle_unstable", 1)}
        for stem, (_, result) in sorted(self.results.items()):
            for report in (result.python, result.souffle):
                relations = dict(report.relations)
                with self.subTest(case=stem, kernel=report.backend):
                    self.assertEqual(len(relations["op_qualified"]), qualified[stem])
                    self.assertEqual(relations["op_qualified"], relations["op_qualified_rt"])
                    self.assertEqual(len(relations["replay_run_stale"]), 1 if stem == "06-stale-replay" else 0)
                    self.assertEqual(len(relations["kill_closure_gap"]), 1 if stem == "08-lying-closure" else 0)
                    for relation in ("effect_order_violation", "repeat_delete_violation", "oracle_unstable"):
                        expected = planted.get(stem, (None, 0))
                        self.assertEqual(len(relations[relation]),
                                         expected[1] if expected[0] == relation else 0,
                                         (stem, relation))
                    # the repeat-delete claim holds wherever the repeat is clean, both effect
                    # tables are closed and the reviewer's exclusion set is closed and bound to the
                    # run by a model witness (case 04 drops that witness, case 10 opens php_effects,
                    # cases 14/15 leave the exclusions open, case 18 plants a write)
                    self.assertEqual(len(relations["repeat_delete_not_found"]),
                                     0 if stem in ("04-missing-model-witness", "10-missing-effects-closure",
                                                   "14-exclusions-not-closed", "15-no-exclusions-no-closure",
                                                   "18-repeat-delete-with-effects", "24-repeat-before-the-commit",
                                                   "25-first-delete-not-committed") else 1, stem)

    def test_certificates_from_both_closures_agree_on_every_derived_claim_row(self) -> None:
        self.assertTrue(self.results, "no differential results; is souffle on PATH?")
        certified = 0
        for stem, (bundle, result) in sorted(self.results.items()):
            known = {record.id for record in bundle.evidence}
            for claim in bundle.claims:
                rows = certificate.claim_conclusions(bundle, result.python.relations, claim)
                self.assertEqual(rows, certificate.claim_conclusions(bundle, result.souffle.relations, claim))
                expected = self.table[stem]["claims"][claim.id]["semantic_verdict"]
                self.assertEqual(bool(rows), expected == "supported", (stem, claim.id))
                for row in rows:
                    with self.subTest(case=stem, claim=claim.id, row=canonical_json(row)):
                        from_python = certificate.certify(bundle, result.python.relations, claim.relation, row)
                        from_souffle = certificate.certify(bundle, result.souffle.relations, claim.relation, row)
                        self.assertEqual(from_python, from_souffle)
                        self.assertFalse(from_python["truncated"])
                        self.assertTrue(certificate.recheck(bundle, from_python, result.souffle.relations).ok)
                        self.assertTrue(certificate.recheck(bundle, from_souffle, result.python.relations).ok)
                        self.assertTrue(set(from_python["leaves"]) <= known)
                        certified += 1
        self.assertGreaterEqual(certified, 12)

    def test_each_rejected_case_would_be_supported_were_it_not_for_producer_authority(self) -> None:
        tables = read_json(REJECTED_PATH)["cases"]
        self.assertEqual(set(tables), {path.stem for path in case_paths(REJECTED_DIR)})
        for path in case_paths(REJECTED_DIR):
            table = tables[path.stem]
            with self.subTest(case=path.stem):
                lenient = load_case(path, self.pack, validate=False)
                self.assertEqual([issue.code for issue in validate_bundle(lenient)], table["validation_issues"])
                # the public entry point fails closed
                report = evaluate(lenient)
                self.assertEqual(report.status.value, "invalid-input")
                self.assertTrue(all(entry.result.operational.value == "invalid-input" for entry in report.claims))
                # the engine itself, run without the validation boundary, would qualify both ops
                engine = _Engine(lenient, ResourceLimits())
                engine.run()
                verdicts = {claim.id: engine.evaluate_claim(index, claim).result.semantic.value
                            for index, claim in enumerate(lenient.claims)}
                self.assertEqual(verdicts, table["unvalidated_verdicts"])
                self.assertEqual({v for v in verdicts.values()}, {"supported"})
                control, _ = self.results.get("00-positive-control", (None, None))
                if control is not None:
                    self.assertEqual(set(engine._canonical_rows("op_qualified")),
                                     set(evaluate(control).relation_rows("op_qualified")))


if __name__ == "__main__":
    unittest.main()
