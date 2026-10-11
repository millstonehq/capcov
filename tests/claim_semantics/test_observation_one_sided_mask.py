"""The reproduced false green: a normalization that fires on ONE side and erases a real difference.

Before this round both the producer and this exporter skipped any firing key
with fewer than two sides (``if len(sides) < 2``).  So a receipt in which the
candidate answered with a body the incumbent did not, and a ``drop`` /
``preserves: none`` normalization fired on the candidate ALONE and made the two
normalized bodies equal, exported ``complete`` with no ``masked_differences``
row, and the judge derived ``observations_agree`` -- supported, complete,
"agreeing" -- with nothing for a reviewer to admit and nothing in the summary
saying a difference had been erased.

The variant below is built from the committed agreeing receipt with exactly
that edit and nothing else: the candidate's ``tenant-own`` raw body digest
differs from the incumbent's, a one-sided ``drop-audit`` firing takes it back
to the incumbent's digest, the policy declaring that normalization is admitted
by the reviewer, and ``masked_differences`` is left empty.  Under the old
rule ``OneSidedMaskTest`` fails on its first assertion with ``'complete' !=
'invalid-input'``; under the new rule (R-9, one-sided form) the receipt is
refused, the judge builds no bundle, and the summary carries the refusal.

``DisclosedOneSidedMaskTest`` is the positive half: the same receipt WITH the
masked-difference row exports, is blocked on ``observation_masked_unadmitted``
until a reviewer admits the mask, and derives agreement once one does -- the
control is a gate, not a wall, exactly as for a two-sided mask.

``RawBodyRuleTest`` is the judge's own copy of the rule: ``observation_body_raw``
is read by ``body_masked`` / ``observation_mask_undisclosed``, so a row set in
which the raw bodies differ, the normalized bodies match and no masked row
discloses it does not derive agreement even if it somehow reaches the bundle
without passing through the exporter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims import canonical_json
from capcov.claims.evaluator import evaluate
from capcov.claims.ir import Atom, Constant, Context, Evidence
from capcov.claims.static.combine import combine
from capcov.claims.observation import observation_facts as facts
from capcov.claims.observation import join as observation_join


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
AGREE = HERE / "fixtures" / "observation_receipt_agree"

SCENARIO = "tenant-own"
#: A drop that preserves nothing: the exact shape that can erase a difference.
NORMALIZATION = {
    "id": "drop-audit",
    "facet": "body",
    "target": "body:/audit",
    "kind": "drop",
    "preserves": "none",
    "reason": "the candidate emits an audit block the incumbent never did",
    "source_sha256": "a" * 64,
}
#: The candidate's body before the drop: a real wire difference.
CANDIDATE_RAW = "d" * 64

ONE_SIDED_REFUSAL = (
    "R-9: normalization 'drop-audit' fired on the candidate side only of scenario "
    "'tenant-own' and erased a body difference (raw digests differ, normalized digests "
    "match) that masked_differences does not export")


def _digest(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def reseal(receipt: dict) -> dict:
    receipt = copy.deepcopy(receipt)
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt["receipt_digest"] = _digest(body)
    return receipt


def one_sided_mask(receipt: dict, *, disclose: bool) -> None:
    """Apply the false-green edit to the agreeing receipt, in place.

    The policy gains the drop, so its digest -- and with it ``run.id`` -- is
    recomputed the way the emitter recomputes them.  ``results`` and
    ``agreement`` are left exactly as committed: the normalized bodies still
    match, so the harness's own summary still says ``agree``.
    """
    policy = receipt["policy"]
    policy["normalizations"].append(copy.deepcopy(NORMALIZATION))
    policy["digest"] = _digest({"facets": policy["facets"],
                                "normalizations": policy["normalizations"],
                                "exclusions": policy["exclusions"]})
    run = receipt["run"]
    run["id"] = _digest([receipt["check"]["id"], receipt["sources"]["source_digest"],
                         receipt["fixture"]["digest"], receipt["scenario_set"]["digest"],
                         policy["digest"], run["nonce"], run["started_at"]])[:16]
    incumbent = next(e for e in receipt["observations"]
                     if e["scenario"] == SCENARIO and e["side"] == "incumbent")
    candidate = next(e for e in receipt["observations"]
                     if e["scenario"] == SCENARIO and e["side"] == "candidate")
    candidate["raw_digest"] = CANDIDATE_RAW
    candidate["body_digest"] = incumbent["body_digest"]
    candidate["normalizations_applied"] = [NORMALIZATION["id"]]
    receipt["normalization_firings"] = [{
        "scenario": SCENARIO, "side": "candidate", "facet": "body",
        "normalization": NORMALIZATION["id"],
        "before_digest": CANDIDATE_RAW, "after_digest": incumbent["body_digest"]}]
    receipt["masked_differences"] = [{
        "scenario": SCENARIO, "facet": "body", "normalization": NORMALIZATION["id"],
        "field_path": NORMALIZATION["target"],
        "incumbent_before_digest": incumbent["raw_digest"],
        "candidate_before_digest": CANDIDATE_RAW}] if disclose else []


class _Variant:
    """The agreeing receipt with the one-sided mask applied, resealed, ledger rebound."""

    def __init__(self, *, disclose: bool, admit: bool = False) -> None:
        self.disclose, self.admit = disclose, admit

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-one-sided-")
        root = Path(self._tmp.name) / "receipt"
        shutil.copytree(AGREE, root)
        path = root / facts.RECEIPT_FILE
        receipt = json.loads(path.read_text(encoding="utf-8"))
        one_sided_mask(receipt, disclose=self.disclose)
        receipt = reseal(receipt)
        path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        ledger_path = root / facts.ADMISSIONS_FILE
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        # The reviewer admitted the policy that declares the drop -- that is
        # not the same as admitting the difference it erased.
        ledger["reviewed_against"]["policy"] = receipt["policy"]["digest"]
        if self.admit:
            ledger["rows"].append({"relation": "masked_difference_admitted",
                                   "normalization": NORMALIZATION["id"],
                                   "field_path": NORMALIZATION["target"]})
        ledger_path.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
        self.run = receipt["run"]["id"]
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def judge(directory: Path):
    join = observation_join.build(directory, fixture=True)
    if join.bundle is None:
        return join, None, {}
    report = evaluate(join.bundle)
    return join, report, dict(report.relations)


def verdict(report, claim_id: str) -> tuple[str, str]:
    entry = next(result for result in report.claims if result.claim.id == claim_id)
    return entry.semantic.value, entry.operational.value


class OneSidedMaskTest(unittest.TestCase):
    """The false green, refused."""

    def test_the_undisclosed_one_sided_mask_is_refused_at_export(self) -> None:
        with _Variant(disclose=False) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT,
                         f"the false green: the exporter returned {result.status!r} for a "
                         f"receipt whose only normalization fired on one side and erased a "
                         f"real difference with no masked_differences row; messages: "
                         f"{result.messages}")
        self.assertIsNone(result.bundle)
        self.assertEqual(result.messages, (ONE_SIDED_REFUSAL,))
        self.assertEqual(facts.refusal_rule(result.messages[0]), "R-9")

    def test_the_judge_derives_no_agreement_and_reports_the_refusal(self) -> None:
        with _Variant(disclose=False) as root:
            join, report, relations = judge(root)
            summary = observation_join.summary(join)
        if report is not None:
            # The old behaviour, spelled out so the failure names it.
            self.fail(f"the false green: the judge built a bundle and returned "
                      f"{verdict(report, join.agree_claim)} for observations_agree with "
                      f"blocking premise "
                      f"{observation_join.blocking_premise(relations, join.run)}")
        self.assertIsNone(join.bundle)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["exporter_status"], facts.STATUS_INVALID_INPUT)
        self.assertEqual(summary["exporter_messages"], [ONE_SIDED_REFUSAL])
        self.assertNotIn("observations_agree", summary)
        self.assertTrue(any(ONE_SIDED_REFUSAL in finding
                            for finding in summary["contract_findings"]))
        # A refused receipt still discloses what it never assessed.
        self.assertEqual({row["dimension"] for row in summary["unassessed"]} >=
                         {"model_conformance"}, True)

    def test_a_two_sided_mask_message_is_not_reused_for_the_one_sided_case(self) -> None:
        """The two forms of R-9 are distinguishable by their message, not just their rule."""
        self.assertIn("on the candidate side only", ONE_SIDED_REFUSAL)
        self.assertNotIn("fired on both sides", ONE_SIDED_REFUSAL)


class OneSidedFiringThatIsNotAMaskTest(unittest.TestCase):
    """A one-sided firing whose normalized digests still differ is a difference, not a mask."""

    def test_a_one_sided_firing_that_leaves_a_difference_is_a_disagreement(self) -> None:
        after = "e" * 64

        def still_differs(receipt: dict) -> None:
            one_sided_mask(receipt, disclose=False)
            candidate = next(e for e in receipt["observations"]
                             if e["scenario"] == SCENARIO and e["side"] == "candidate")
            incumbent = next(e for e in receipt["observations"]
                             if e["scenario"] == SCENARIO and e["side"] == "incumbent")
            candidate["body_digest"] = after
            receipt["normalization_firings"][0]["after_digest"] = after
            for row in receipt["results"]:
                if row["scenario"] == SCENARIO:
                    row["outcome"] = "differ"
                    row["difference"] = {"facet": "body", "field_path": "body",
                                         "incumbent_digest": incumbent["body_digest"],
                                         "candidate_digest": after}
            receipt["agreement"] = {"compared": 12, "agree": 11, "differ": 1,
                                    "not_observed": 0, "degenerate": 0, "complete": False}

        with tempfile.TemporaryDirectory(prefix="capcov-observation-one-sided-") as tmp:
            root = Path(tmp) / "receipt"
            shutil.copytree(AGREE, root)
            path = root / facts.RECEIPT_FILE
            receipt = json.loads(path.read_text(encoding="utf-8"))
            still_differs(receipt)
            receipt = reseal(receipt)
            path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
            ledger_path = root / facts.ADMISSIONS_FILE
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["reviewed_against"]["policy"] = receipt["policy"]["digest"]
            ledger_path.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n",
                                   encoding="utf-8")
            join, report, relations = judge(root)
        # Not refused: nothing was erased.  Not agreeing: the difference is real.
        self.assertIsNotNone(join.bundle, join.contract_findings)
        self.assertNotIn("observation_masked_difference", join.exported.counts)
        self.assertFalse(relations.get("body_masked"))
        self.assertTrue(relations.get("observation_disagree_any"))
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_disagree_any")


class DisclosedOneSidedMaskTest(unittest.TestCase):
    """The same receipt with the row it owes: exported, gated, admitted."""

    def test_with_the_masked_row_the_receipt_exports_and_is_blocked_on_admission(self) -> None:
        with _Variant(disclose=True) as root:
            join, report, relations = judge(root)
        self.assertIsNotNone(join.bundle, join.contract_findings)
        self.assertEqual(join.exported.counts["observation_masked_difference"], 1)
        self.assertEqual(join.exported.counts["observation_normalization"], 1)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        blocking = observation_join.blocking_premise(relations, join.run)
        self.assertEqual(blocking["relation"], "observation_masked_unadmitted")
        self.assertEqual(observation_join.masked_differences(relations, join.run),
                         [{"scenario": SCENARIO, "facet": "body",
                           "normalization": NORMALIZATION["id"],
                           "field_path": NORMALIZATION["target"]}])
        # The judge's own copy of the rule sees the erased difference too, and
        # sees that it was disclosed.
        self.assertEqual([row[1] for row in relations.get("body_masked", ())], [SCENARIO])
        self.assertFalse(relations.get("observation_mask_undisclosed"))
        self.assertIn(NORMALIZATION["id"],
                      observation_join.normalization_note(relations, join.run))

    def test_admitting_the_one_sided_mask_makes_the_same_receipt_derive(self) -> None:
        with _Variant(disclose=True, admit=True) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim), ("supported", "complete"))
        self.assertIsNone(observation_join.blocking_premise(relations, join.run))
        # ... and the erased difference is still exported and still reported.
        self.assertEqual(len(observation_join.masked_differences(relations, join.run)), 1)
        self.assertIn(NORMALIZATION["id"],
                      observation_join.normalization_note(relations, join.run))
        self.assertEqual(
            observation_join.qualification(*verdict(report, join.agree_claim)),
            observation_join.QUALIFICATION_AGREEING)


class RawBodyRuleTest(unittest.TestCase):
    """``observation_body_raw`` is load-bearing: the judge re-derives the erased difference."""

    def _with_body_raw_row(self, scenario: str, side: str, raw_digest: str):
        join = observation_join.build(AGREE, fixture=True)
        decl = next(d for d in join.bundle.relations if d.name == "observation_body_raw")
        values = {"run": join.run, "scenario": scenario, "side": side, "raw_digest": raw_digest}
        atom = Atom("observation_body_raw", tuple(Constant(values[c.name], c.type)
                                                  for c in decl.columns))
        record = Evidence("observe:direct:observation_body_raw", atom,
                          Context.from_mapping({"run": join.run}),
                          "observe capcov.claims.observation.observation_facts v1", (), "fact")
        return combine(join.bundle, facts=[atom], evidence=[record], validate=False), join.run

    def test_the_agreeing_receipt_derives_no_masked_body(self) -> None:
        join, report, relations = judge(AGREE)
        self.assertFalse(relations.get("body_masked"))
        self.assertFalse(relations.get("observation_mask_undisclosed"))
        self.assertTrue(relations.get("observation_mask_disclosure_closed"))
        self.assertEqual(verdict(report, join.agree_claim), ("supported", "complete"))

    def test_a_raw_row_that_differs_while_the_bodies_match_blocks_the_claim(self) -> None:
        bundle, run = self._with_body_raw_row(SCENARIO, "candidate", CANDIDATE_RAW)
        relations = dict(evaluate(bundle).relations)
        self.assertEqual(sorted(row[1] for row in relations.get("body_masked", ())), [SCENARIO])
        self.assertTrue(relations.get("observation_mask_undisclosed"))
        self.assertFalse(relations.get("observations_agree"))
        blocking = observation_join.blocking_premise(relations, run)
        self.assertEqual(blocking["relation"], "observation_mask_undisclosed")
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, run)}
        self.assertEqual(statuses.get("observation_mask_undisclosed"), "refuted")


if __name__ == "__main__":
    unittest.main()
