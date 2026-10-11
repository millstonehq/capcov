"""What the judge's report must carry however the run went (C6, C7).

Two defects are pinned here, both of the same shape: something the receipt
DISCLOSED existed only in an input nobody printed.

C6.  ``model_conformance`` -- the admission that no model of intended behaviour
described the run, without which "the two sides agreed" is easily read as "the
candidate is correct" -- is an ``unassessed`` row of every receipt.  It reached
the judge's summary only from the CLOSURE, so in the only configuration
available without Soufflé on PATH (``build`` with no differential run) the
summary printed ``unassessed: []``.  The exporter's own messages -- every open
completeness box with its reason, the absent admissions ledger, the absent log
-- never reached the summary at all.  Now both always do, and the summary says
which of the closure and the receipt the rows came from.

C7.  ``configuration.sides_distinct: false`` was one more message in that
unprinted list: a receipt that never claimed two distinct processes exported
clean and derived agreement.  It is now a refusal (R-15), and the refusal
arrives in the judge's report rather than in a list the reader never sees --
``test_observation_facts_export`` pins the refusal itself, this file pins that
it survives into the report.

The two-kernel differential is not run here (it needs Soufflé and is skipped in
``test_observation_join.KernelDifferentialTest``).  ``_PythonOnly`` stands in for
its result so ``summary`` has a closure to read; nothing here claims the kernels
agree.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capcov.claims import canonical_json
from capcov.claims.differential import run_python
from capcov.claims.observation import observation_facts as facts
from capcov.claims.observation import join as observation_join

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
AGREE = FIXTURES / "observation_receipt_agree"

MANDATORY = ("model_conformance", "fault_sensitivity", "scenario_coverage",
             "write_set_declaration", "producer_independence", "deployed_parity",
             "concurrency", "performance", "effect_ordering")


@dataclass
class _PythonOnly:
    """A stand-in for the differential's result, carrying the Python closure only.

    The closure itself is the real thing -- ``differential.run_python`` is the
    kernel the differential runs -- but the KERNEL claim, that two independent
    kernels derived the same rows, is NOT made here and is not made by anything
    that skips Soufflé.
    """
    python: Any
    matched: bool = True


def reseal(receipt: dict) -> dict:
    receipt = copy.deepcopy(receipt)
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt["receipt_digest"] = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return receipt


class _Variant:
    """A committed receipt copied to a tempdir with one edit, resealed."""

    def __init__(self, source: Path, mutate=None) -> None:
        self.source, self.mutate = source, mutate

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-disclosure-")
        root = Path(self._tmp.name) / "receipt"
        shutil.copytree(self.source, root)
        if self.mutate is not None:
            path = root / facts.RECEIPT_FILE
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.mutate(receipt)
            path.write_text(json.dumps(reseal(receipt), indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def judged(directory: Path) -> tuple[Any, dict[str, Any]]:
    """The join with its Python closure attached, and its summary."""
    join = observation_join.build(directory, fixture=True)
    join.result = _PythonOnly(run_python(join.bundle))
    return join, observation_join.summary(join)


class UnassessedAlwaysPrintsTest(unittest.TestCase):
    def test_the_unevaluated_summary_prints_every_unassessed_row(self) -> None:
        """The configuration this machine actually has: built, never closed."""
        join = observation_join.build(AGREE, fixture=True)
        self.assertIsNone(join.result)
        report = observation_join.summary(join)
        self.assertEqual(report["status"], "not-evaluated")
        dimensions = [row["dimension"] for row in report["unassessed"]]
        self.assertIn("model_conformance", dimensions)
        for dimension in MANDATORY:
            self.assertIn(dimension, dimensions)
        self.assertEqual(report["unassessed_source"], "receipt")

    def test_the_closed_summary_prints_them_from_the_closure(self) -> None:
        _join, report = judged(AGREE)
        self.assertEqual(report["unassessed_source"], "closure")
        self.assertEqual(len(report["unassessed"]), 10)
        self.assertIn("model_conformance", [row["dimension"] for row in report["unassessed"]])

    def test_both_paths_print_the_same_rows_with_the_same_reasons(self) -> None:
        """The fallback is the same disclosure, not a thinner one."""
        built = observation_join.summary(observation_join.build(AGREE, fixture=True))
        _join, closed = judged(AGREE)
        self.assertEqual(built["unassessed"], closed["unassessed"])

    def test_the_reasons_are_the_receipts_own_words(self) -> None:
        receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
        declared = {row["dimension"]: row["reason"] for row in receipt["unassessed"]}
        report = observation_join.summary(observation_join.build(AGREE, fixture=True))
        for row in report["unassessed"]:
            self.assertEqual(row["reason"], declared[row["dimension"]])

    def test_a_receipt_with_no_unassessed_block_prints_an_empty_list(self) -> None:
        """Defensive: the fallback reads an untrusted document and must not raise."""
        self.assertEqual(observation_join.receipt_unassessed({}), [])
        self.assertEqual(observation_join.receipt_unassessed({"unassessed": "no"}), [])
        self.assertEqual(observation_join.receipt_unassessed({"unassessed": [1, {"a": "b"}]}), [])


class ExporterMessagesSurviveTest(unittest.TestCase):
    def test_the_summary_carries_every_message_the_exporter_produced(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        report = observation_join.summary(join)
        self.assertEqual(report["exporter_status"], facts.STATUS_COMPLETE)
        self.assertEqual(len(report["exporter_messages"]), len(join.exported.messages))
        self.assertEqual(report["exporter_messages"],
                         [m.replace(str(AGREE), "<receipt-dir>")
                          for m in join.exported.messages])

    def test_the_open_completeness_boxes_and_their_reasons_are_in_the_report(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        report = observation_join.summary(join)
        rendered = "\n".join(report["exporter_messages"])
        self.assertIn("completeness.by_side.incumbent.effects is open", rendered)
        self.assertIn("this capability performs no writes; no store was inspected", rendered)
        self.assertIn("no observation_effects_closed witness for incumbent", rendered)
        self.assertIn("the admissions ledger admits no masked difference", rendered)

    def test_an_absent_ledger_is_in_the_report(self) -> None:
        with _Variant(AGREE) as root:
            (root / facts.ADMISSIONS_FILE).unlink()
            join = observation_join.build(root, fixture=True)
            report = observation_join.summary(join)
        rendered = "\n".join(report["exporter_messages"])
        self.assertIn("observation admission record absent", rendered)
        self.assertIn("no reviewer admitted this comparison policy", rendered)

    def test_an_absent_log_is_a_refusal_in_the_report(self) -> None:
        """The log is required: bytes that are absent verify nothing, so the export refuses."""
        with _Variant(AGREE) as root:
            (root / facts.LOG_FILE).unlink()
            join = observation_join.build(root, fixture=True)
            report = observation_join.summary(join)
        self.assertEqual(report["exporter_status"], facts.STATUS_INVALID_INPUT)
        rendered = "\n".join(report["exporter_messages"])
        self.assertIn(f"R-3: {facts.LOG_FILE} is absent", rendered)

    def test_no_local_path_travels_with_the_messages(self) -> None:
        with _Variant(AGREE) as root:
            join = observation_join.build(root, fixture=True)
            report = observation_join.summary(join)
            rendered = json.dumps(report)
            self.assertNotIn(str(root), rendered)
        # Redacted, not dropped: the identity message is still there.
        self.assertIn("<receipt-dir>", "\n".join(report["exporter_messages"]))
        self.assertIn("observation identity", "\n".join(report["exporter_messages"]))

    def test_the_artifact_drops_no_message_either(self) -> None:
        with _Variant(AGREE) as root:
            join = observation_join.build(root, fixture=True)
            with tempfile.TemporaryDirectory() as out:
                document = observation_join.write_artifacts(join, Path(out))
                written = json.loads((Path(out) / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(len(document["export"]["messages"]), len(join.exported.messages))
            self.assertEqual(document["export"]["messages"],
                             document["join"]["exporter_messages"])
            self.assertNotIn(str(root), json.dumps(written))
        self.assertIn("observation identity", "\n".join(written["export"]["messages"]))


class RefusedReceiptStillDisclosesTest(unittest.TestCase):
    """C7 and C5's neighbour: a refusal is a reported outcome, with its disclosures."""

    @staticmethod
    def _not_distinct(receipt: dict) -> None:
        receipt["configuration"]["sides_distinct"] = False

    def test_a_receipt_that_does_not_claim_distinct_sides_is_blocked_not_agreeing(self) -> None:
        with _Variant(AGREE, self._not_distinct) as root:
            join = observation_join.build(root, fixture=True)
            report = observation_join.summary(join)
        self.assertIsNone(join.bundle)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["exporter_status"], facts.STATUS_INVALID_INPUT)
        self.assertNotIn("qualification", report)

    def test_the_refusal_names_sides_distinct_in_the_judges_report(self) -> None:
        with _Variant(AGREE, self._not_distinct) as root:
            report = observation_join.summary(observation_join.build(root, fixture=True))
        rendered = "\n".join(report["contract_findings"] + report["exporter_messages"])
        self.assertIn("R-15", rendered)
        self.assertIn("sides_distinct is false", rendered)
        self.assertIn("may be one process answering twice", rendered)

    def test_the_refused_report_still_prints_what_was_never_assessed(self) -> None:
        with _Variant(AGREE, self._not_distinct) as root:
            report = observation_join.summary(observation_join.build(root, fixture=True))
        dimensions = [row["dimension"] for row in report["unassessed"]]
        for dimension in MANDATORY:
            self.assertIn(dimension, dimensions)
        self.assertEqual(report["unassessed_source"], "receipt")
        self.assertIn("not a correctness claim", report["not_a_claim"])

    def test_a_refused_receipt_never_reads_as_an_absent_run(self) -> None:
        """The blocked report names the run and the refusing exporter status."""
        with _Variant(AGREE, self._not_distinct) as root:
            join = observation_join.build(root, fixture=True)
            report = observation_join.summary(join)
        self.assertEqual(report["run"], join.run)
        self.assertTrue(report["run"])
        self.assertTrue(report["contract_findings"])
        self.assertTrue(report["exporter_messages"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
