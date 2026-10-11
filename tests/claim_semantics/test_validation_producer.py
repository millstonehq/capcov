"""Producer-class authority is enforced at evidence ingestion (``evidence-producer``).

``RelationDecl.producer_classes`` used to be validated for shape only.  Now a
relation that declares a non-empty tuple admits evidence only from a source
whose producer class -- the first whitespace-delimited token of
``Evidence.source`` -- is in that tuple.  Facts are already tied to evidence
(``fact-without-evidence``), so evidence is the single boundary.  An empty
tuple is unconstrained, which is why the frozen static schema and every
existing corpus are unaffected.
"""
from __future__ import annotations

import unittest

from capcov.claims import (Atom, Bundle, BundleIngestionError, Column, Constant,
                           Context, Evidence, RelationDecl, ValidationError,
                           bundle_from_json, canonical_json, validate_bundle)


def _bundle(source: str, producer_classes=("php", "go")) -> Bundle:
    relation = RelationDecl(
        "php_effect",
        (Column("run", "symbol", True), Column("req", "symbol"), Column("table", "symbol")),
        context_indices=("run",), producer_classes=producer_classes)
    atom = Atom("php_effect", (Constant("run-1"), Constant("req-1"), Constant("issues")))
    record = Evidence("php:run-1:php_effect:0", atom, Context.from_mapping({"run": "run-1"}),
                      source=source)
    return Bundle((relation,), facts=(atom,), evidence=(record,))


class EvidenceProducerTest(unittest.TestCase):
    def test_matching_producer_class_passes(self) -> None:
        for source in ("php target-cloud 1a2b3c", "go target-go 4d5e6f", "php"):
            with self.subTest(source=source):
                self.assertEqual(validate_bundle(_bundle(source)), ())

    def test_mismatched_producer_yields_exactly_one_issue_and_ingestion_raises(self) -> None:
        bundle = _bundle("shen model-runner v1")
        issues = validate_bundle(bundle)
        self.assertEqual([issue.code for issue in issues], ["evidence-producer"])
        [issue] = issues
        self.assertEqual(issue.path, "evidence[0]")
        self.assertIn("'php_effect' admits ('go', 'php')", issue.message)
        self.assertIn("'shen model-runner v1'", issue.message)
        # the strict wire boundary wraps the ValidationError as the one named
        # ingestion error and carries the code through
        with self.assertRaises(BundleIngestionError) as ctx:
            bundle_from_json(canonical_json(bundle), validate=True)
        self.assertIn("evidence-producer", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ValidationError)
        # only the producer token counts: a longer source that merely mentions
        # an admitted class elsewhere is still rejected
        self.assertEqual([i.code for i in validate_bundle(_bundle("shen via php"))],
                         ["evidence-producer"])
        # and an empty source is the pre-existing evidence-source issue, not a
        # second producer complaint on top of it
        self.assertEqual([i.code for i in validate_bundle(_bundle(""))], ["evidence-source"])

    def test_an_evidence_free_fact_bearing_bundle_cannot_bypass_the_boundary(self) -> None:
        # Attribution used to run only ``if bundle.evidence:``, so a bundle with
        # facts and an empty evidence tuple validated with zero issues and never
        # met evidence-producer.  Attribution is unconditional now.
        relation = RelationDecl(
            "php_effect",
            (Column("run", "symbol", True), Column("req", "symbol"), Column("table", "symbol")),
            context_indices=("run",), producer_classes=("php",))
        atom = Atom("php_effect", (Constant("run-1"), Constant("req-1"), Constant("issues")))
        bare = Bundle((relation,), facts=(atom,), evidence=())
        issues = validate_bundle(bare)
        self.assertEqual([issue.code for issue in issues], ["fact-without-evidence"])
        self.assertEqual(issues[0].path, "facts[0]")
        with self.assertRaises(BundleIngestionError) as ctx:
            bundle_from_json(canonical_json(bare), validate=True)
        self.assertIn("fact-without-evidence", str(ctx.exception))
        # the same holds for an unconstrained relation and for an assumption:
        # every fact needs a record, whatever the producer classes say
        plain = RelationDecl("seen", (Column("x", "symbol"),))
        assumed = RelationDecl("trusted__accepted", (Column("x", "symbol"),), modality="assumption")
        two = Bundle((plain, assumed), facts=(Atom("seen", (Constant("v"),)),
                                             Atom("trusted__accepted", (Constant("v"),))))
        self.assertEqual([(i.code, i.path) for i in validate_bundle(two)],
                         [("fact-without-evidence", "facts[0]"), ("fact-without-evidence", "facts[1]")])
        # partial attribution names exactly the unattributed fact
        record = Evidence("seen:v", Atom("seen", (Constant("v"),)), source="anyone")
        partial = Bundle((plain, assumed), facts=two.facts, evidence=(record,))
        self.assertEqual([(i.code, i.path) for i in validate_bundle(partial)],
                         [("fact-without-evidence", "facts[1]")])
        # and the public evaluator fails closed on the bare bundle
        from capcov.claims.evaluator import evaluate
        report = evaluate(bare)
        self.assertEqual(report.status.value, "invalid-input")
        self.assertIn("fact-without-evidence", report.message)

    def test_empty_producer_classes_is_unconstrained(self) -> None:
        for source in ("shen model-runner v1", "anything at all", "capcov.claims.static.scip_facts v1"):
            with self.subTest(source=source):
                self.assertEqual(validate_bundle(_bundle(source, producer_classes=())), ())
                bundle_from_json(canonical_json(_bundle(source, producer_classes=())), validate=True)


if __name__ == "__main__":
    unittest.main()
