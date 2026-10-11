"""Producer-class authority for the observation primitives, end to end.

The pack declares exactly two producer classes -- ``observe`` (the check
harness) and ``reviewer`` -- and nothing else.  There is no ``shen``,
``modelcheck``, ``mut``, ``php-census``, ``replay``, ``php`` or ``go`` class
anywhere in it, so a model row is not merely absent from these receipts: it
cannot be ingested at all.  That is the structural half of R10 (two-lane
confusion).

Authority is enforced at ONE boundary, evidence ingestion in
``claims.validation``, which compares the first whitespace token of
``Evidence.source`` against the relation's declared ``producer_classes`` and
raises ``evidence-producer``.  The exporter deliberately does not pre-check it,
so a ledger claiming a Lane B class is refused for the right reason rather than
for a weaker one in front of it.

Both directions are tested: the harness may not sign a reviewer's row, and a
reviewer's ledger may not claim the harness's class or a Lane B class.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims import ValidationError, assert_valid, validate_bundle
from capcov.claims.evaluator import evaluate
from capcov.claims.ir import Atom, Constant, Context, Evidence
from capcov.claims.static.combine import combine
from capcov.claims.observation import observation_facts as facts
from capcov.claims.observation.pack import load_pack, pack_bundle


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
AGREE = HERE / "fixtures" / "observation_receipt_agree"

OBSERVE, REVIEWER = "observe", "reviewer"
#: Lane B's producer classes.  None of them is declarable here.
FOREIGN_CLASSES = ("shen", "modelcheck", "mut", "php-census", "replay", "php", "go")
#: The relations only a reviewer may sign: the three claim-time observations
#: (spec P18-P20), the three admissions (P21-P23), and the closure of the
#: admission list itself (P38) -- which is the reviewer's own statement that the
#: ledger is complete, and so cannot be the harness's to make.
REVIEWER_RELATIONS = ("observation_nonce_observed", "observation_source_observed",
                      "observation_fixture_observed", "policy_admitted",
                      "scenario_set_admitted", "masked_difference_admitted",
                      "masked_admissions_closed")


class DeclaredClassesTest(unittest.TestCase):
    def test_the_pack_declares_only_observe_and_reviewer(self) -> None:
        declared = set()
        for relation in load_pack()["primitives"]:
            declared.update(relation["producer_classes"])
        self.assertEqual(declared, {OBSERVE, REVIEWER})
        for foreign in FOREIGN_CLASSES:
            self.assertNotIn(foreign, declared)

    def test_the_evidence_prefixes_cover_exactly_those_classes(self) -> None:
        self.assertEqual(set(facts.EVIDENCE_PREFIXES), {OBSERVE, REVIEWER})

    def test_the_reviewer_relations_are_reviewer_owned(self) -> None:
        by_name = {relation["name"]: relation for relation in load_pack()["primitives"]}
        for name in REVIEWER_RELATIONS:
            self.assertEqual(by_name[name]["producer_classes"], [REVIEWER], name)

    def test_the_harness_relations_are_observe_owned(self) -> None:
        by_name = {relation["name"]: relation for relation in load_pack()["primitives"]}
        harness = [name for name in by_name if name not in REVIEWER_RELATIONS]
        for name in harness:
            self.assertEqual(by_name[name]["producer_classes"], [OBSERVE], name)


class LedgerProducerTest(unittest.TestCase):
    """A reviewer's ledger that claims another class fails at ingestion."""

    def _with_producer(self, producer: str):
        with tempfile.TemporaryDirectory(prefix="capcov-observation-producer-") as tmp:
            root = Path(tmp) / "receipt"
            shutil.copytree(AGREE, root)
            path = root / facts.ADMISSIONS_FILE
            document = json.loads(path.read_text(encoding="utf-8"))
            document["producer"] = producer
            path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
            return _export_fixture(root)

    def test_the_committed_ledger_is_accepted(self) -> None:
        result = _export_fixture(AGREE)
        self.assertEqual(result.status, facts.STATUS_COMPLETE)
        assert_valid(result.bundle)

    def test_a_ledger_claiming_a_lane_b_class_is_refused(self) -> None:
        for foreign in FOREIGN_CLASSES:
            with self.subTest(foreign):
                result = self._with_producer(f"{foreign} a-reviewer")
                self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
                self.assertIsNone(result.bundle)
                self.assertTrue(any(message.startswith("evidence-producer:")
                                    for message in result.messages), result.messages)
                self.assertTrue(any("policy_admitted" in message
                                    for message in result.messages), result.messages)

    def test_a_ledger_claiming_the_harness_class_is_refused(self) -> None:
        """The harness may not sign its own admission: that is the whole point."""
        result = self._with_producer(f"{OBSERVE} the-harness")
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertTrue(any(message.startswith("evidence-producer:")
                            for message in result.messages), result.messages)


class HarnessMaySignNoReviewerRowTest(unittest.TestCase):
    """A reviewer-class row emitted under the harness's class is refused."""

    @staticmethod
    def _joined(source: str):
        exported = _export_fixture(AGREE)
        pack = pack_bundle()
        decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
        decl = decls["observation_nonce_observed"]
        receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
        run, nonce = receipt["run"]["id"], receipt["run"]["nonce"]
        atom = Atom("observation_nonce_observed",
                    (Constant(run, decl.columns[0].type), Constant(nonce, decl.columns[1].type)))
        record = Evidence("forged:nonce-observed", atom, Context.from_mapping({}), source, (),
                          "fact")
        return combine(exported.bundle, pack, facts=[atom], evidence=[record], validate=False)

    def test_a_reviewer_row_signed_by_the_harness_is_refused(self) -> None:
        bundle = self._joined(f"{OBSERVE} capcov.claims.observation.observation_facts v1")
        codes = {issue.code for issue in validate_bundle(bundle)}
        self.assertIn("evidence-producer", codes)
        with self.assertRaises(ValidationError):
            assert_valid(bundle)

    def test_the_same_row_signed_by_a_reviewer_is_accepted(self) -> None:
        bundle = self._joined("reviewer claim-time observation")
        self.assertEqual(validate_bundle(bundle), ())

    def test_the_python_kernel_does_not_evaluate_an_unauthorized_row(self) -> None:
        bundle = self._joined(f"{OBSERVE} capcov.claims.observation.observation_facts v1")
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "invalid-input")
        self.assertIn("evidence-producer", report.message)
        self.assertEqual(report.relations, ())


if __name__ == "__main__":
    unittest.main()
