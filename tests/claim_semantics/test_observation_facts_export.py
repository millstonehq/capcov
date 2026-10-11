"""Ingestion of the four committed observation receipts, and the fifteen refusals.

Three parts.  ``CommittedReceiptExportTest`` proves the four golden receipt
directories export, that what they export is what they say, and that the
exporter recomputed rather than believed -- the receipt's ``results`` and
``agreement`` blocks are its own summary and are never the source of a row.
``NonconformingReceiptExample`` is the documented negative set: one copy of the
agreeing receipt per rule, each with exactly ONE field peeled off, each pinned to
the exact refusal message of the rule it breaks (spec section 2, R-1..R-15).
``ForkHygieneTest`` greps every committed byte of the new package and of the
four fixtures for the strings this public fork may not carry.

Nothing here runs a kernel; ``test_observation_join`` does that.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims import canonical_json, validate_bundle
from capcov.claims.observation import observation_facts as facts


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
PACKAGE = HERE.parents[1] / "src" / "capcov" / "claims" / "observation"

AGREE = FIXTURES / "observation_receipt_agree"
GAP = FIXTURES / "observation_receipt_gap"
DISAGREE = FIXTURES / "observation_receipt_disagree"
MASKED = FIXTURES / "observation_receipt_masked"
ALL_FIXTURES = (AGREE, GAP, DISAGREE, MASKED)

SCENARIO_COUNT = 12


def read_receipt(directory: Path) -> dict:
    return json.loads((directory / facts.RECEIPT_FILE).read_text(encoding="utf-8"))


def reseal(receipt: dict) -> dict:
    """Recompute ``receipt_digest`` after a mutation, the way the emitter would.

    Used only by the POSITIVE in-test variants, which must ingest cleanly.  The
    negative cases below deliberately do NOT reseal: each of their refusals
    fires before the digest is reached, which is itself part of the contract --
    a receipt does not have to be self-consistent to be refused for the right
    reason.
    """
    receipt = copy.deepcopy(receipt)
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt["receipt_digest"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")).hexdigest()
    return receipt


class _Mutated:
    """A copy of a committed receipt directory with one edit applied."""

    def __init__(self, source: Path, mutate, *, seal: bool = False,
                 ledger=None) -> None:
        self.source, self.mutate, self.seal, self.ledger = source, mutate, seal, ledger

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-")
        self.root = Path(self._tmp.name) / "receipt"
        shutil.copytree(self.source, self.root)
        receipt = read_receipt(self.root)
        self.mutate(receipt)
        if self.seal:
            receipt = reseal(receipt)
        (self.root / facts.RECEIPT_FILE).write_text(
            json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if self.ledger is not None:
            path = self.root / facts.ADMISSIONS_FILE
            document = json.loads(path.read_text(encoding="utf-8"))
            self.ledger(document)
            path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        return self.root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class CommittedReceiptExportTest(unittest.TestCase):
    """The four golden receipts ingest, and export what they actually say."""

    def test_all_four_export_complete_and_validate(self) -> None:
        for directory in ALL_FIXTURES:
            with self.subTest(directory.name):
                result = _export_fixture(directory)
                self.assertEqual(result.status, facts.STATUS_COMPLETE,
                                 "; ".join(result.messages))
                self.assertIsNotNone(result.bundle)
                self.assertEqual(validate_bundle(result.bundle), ())

    def test_the_agreeing_receipt_exports_one_row_per_observation(self) -> None:
        result = _export_fixture(AGREE)
        counts = result.counts
        self.assertEqual(counts["observation_run"], 1)
        self.assertEqual(counts["observation_scenario"], SCENARIO_COUNT)
        self.assertEqual(counts["observation_observed"], SCENARIO_COUNT * 2)
        self.assertEqual(counts["observation_status"], SCENARIO_COUNT * 2)
        self.assertEqual(counts["observation_class"], SCENARIO_COUNT * 2)
        self.assertEqual(counts["observation_body"], SCENARIO_COUNT * 2)
        self.assertEqual(counts["observation_body_raw"], SCENARIO_COUNT * 2)
        self.assertNotIn("observation_unobserved", counts)
        # This capability writes nothing; the receipt says "we did not look",
        # which is why no effects row and no effects closure exist.  (Per-effect
        # rows are never exported as relations for any receipt: the digest is
        # the comparison unit and the rows are its receipt-side evidence.)
        self.assertNotIn("observation_effects", counts)
        self.assertNotIn("observation_effects_closed", counts)
        self.assertEqual(counts["observation_unassessed"], 10)
        self.assertEqual(counts["observation_stability"], 1)

    def test_only_observe_and_reviewer_ever_appear_as_producers(self) -> None:
        for directory in ALL_FIXTURES:
            with self.subTest(directory.name):
                bundle = _export_fixture(directory).bundle
                classes = {record.source.split()[0] for record in bundle.evidence}
                self.assertEqual(classes, {"observe", "reviewer"})

    def test_the_ledger_rows_are_assumptions_and_the_rest_are_facts(self) -> None:
        bundle = _export_fixture(AGREE).bundle
        assumptions = {record.atom.relation for record in bundle.evidence
                       if record.kind == "assumption"}
        self.assertEqual(assumptions, {"policy_admitted", "scenario_set_admitted"})

    def test_the_gap_receipt_exports_the_unobserved_row_not_a_silence(self) -> None:
        result = _export_fixture(GAP)
        self.assertEqual(result.status, facts.STATUS_COMPLETE)
        self.assertEqual(result.counts["observation_unobserved"], 1)
        self.assertEqual(result.counts["observation_observed"], SCENARIO_COUNT * 2 - 1)
        rows = [record.atom for record in result.bundle.evidence
                if record.atom.relation == "observation_unobserved"]
        self.assertEqual([term.value for term in rows[0].terms][1:],
                         ["project-malformed", "candidate", "timeout"])

    def test_the_masked_receipt_exports_the_difference_it_hid(self) -> None:
        result = _export_fixture(MASKED)
        self.assertEqual(result.status, facts.STATUS_COMPLETE)
        self.assertEqual(result.counts["observation_masked_difference"], 1)
        self.assertEqual(result.counts["observation_normalization"], 2)
        self.assertEqual(result.counts["observation_policy_entry"], 1)

    def test_identity_is_a_function_of_the_rows_not_of_where_they_were_read(self) -> None:
        first = _export_fixture(AGREE)
        with _Mutated(AGREE, lambda r: None) as elsewhere:
            second = _export_fixture(elsewhere)
        self.assertEqual(dict(first.bundle.metadata)["observation_digest"],
                         dict(second.bundle.metadata)["observation_digest"])
        self.assertEqual(facts.bundle_digest(first.bundle),
                         facts.bundle_digest(second.bundle))

    def test_the_four_receipts_have_four_distinct_identities(self) -> None:
        identities = {directory.name: dict(_export_fixture(directory).bundle.metadata)
                      ["observation_digest"] for directory in ALL_FIXTURES}
        self.assertEqual(len(set(identities.values())), 4, identities)

    def test_an_absent_required_log_refuses_the_receipt(self) -> None:
        with _Mutated(AGREE, lambda r: None) as root:
            (root / facts.LOG_FILE).unlink()
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("log.txt is absent", result.messages[0])

    def test_receipt_local_admissions_are_ignored_by_default(self) -> None:
        result = facts.export_bundle(AGREE)
        self.assertEqual(result.status, facts.STATUS_COMPLETE, "; ".join(result.messages))
        self.assertEqual(result.counts.get("policy_admitted", 0), 0)
        self.assertEqual(result.counts.get("scenario_set_admitted", 0), 0)
        self.assertTrue(any("external observation admission record absent" in message
                            for message in result.messages), result.messages)

    def test_repeat_must_uniquely_cover_the_registered_set(self) -> None:
        def partial(receipt: dict) -> None:
            receipt["repeat"]["compared_scenarios"].pop()

        with _Mutated(AGREE, partial) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("does not cover the admitted set", result.messages[0])

    def test_repeat_duplicate_scenario_is_refused(self) -> None:
        def duplicate(receipt: dict) -> None:
            receipt["repeat"]["compared_scenarios"][0] = receipt["repeat"]["compared_scenarios"][1]

        with _Mutated(AGREE, duplicate) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("contains a duplicate scenario", result.messages[0])

    def test_an_absent_ledger_withholds_the_admissions_rather_than_inventing_them(self) -> None:
        with _Mutated(AGREE, lambda r: None) as root:
            (root / facts.ADMISSIONS_FILE).unlink()
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_COMPLETE)
        self.assertNotIn("policy_admitted", result.counts)
        self.assertNotIn("scenario_set_admitted", result.counts)
        self.assertNotIn("masked_admissions_closed", result.counts)

    def test_a_ledger_that_reviewed_another_policy_is_stale_not_malformed(self) -> None:
        def other_policy(document: dict) -> None:
            document["reviewed_against"]["policy"] = "0" * 64

        with _Mutated(AGREE, lambda r: None, ledger=other_policy) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_STALE)
        self.assertIn("is not a review of this receipt", result.messages[0])

    def test_a_receipt_for_another_run_is_refused(self) -> None:
        result = _export_fixture(AGREE, run="0" * 16)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("caller asked for", result.messages[0])


class NonconformingReceiptExample(unittest.TestCase):
    """One field peeled off the agreeing receipt per rule, with its exact refusal.

    The messages are pinned in full.  Their stable part is the ``R-n:`` prefix --
    ``refusal_rule`` is the contract -- but pinning the prose too is what stops a
    refusal quietly becoming a different, weaker refusal.
    """

    def _refuse(self, mutate, expected: str, *, source: Path = AGREE, forbidden=None) -> None:
        with _Mutated(source, mutate) as root:
            kwargs = {} if forbidden is None else {"forbidden_strings": forbidden}
            result = _export_fixture(root, **kwargs)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertEqual(result.messages, (expected,))
        self.assertIsNone(result.bundle)
        rule = facts.refusal_rule(result.messages[0])
        self.assertIn(rule, facts.REFUSALS)

    def test_r1_another_contract_string_is_refused_not_guessed_at(self) -> None:
        def mutate(receipt): receipt["contract"] = "observation-receipt/v2"
        self._refuse(mutate, "R-1: receipt.json: contract must be 'observation-receipt/v1', "
                             "got 'observation-receipt/v2'")

    def test_r2_a_missing_block_is_refused(self) -> None:
        self._refuse(lambda receipt: receipt.pop("terminal"),
                     "R-2: receipt.json: missing required blocks ['terminal']")

    def test_r2_a_dirty_candidate_tree_is_refused_never_waived(self) -> None:
        def mutate(receipt): receipt["sources"]["candidate"]["dirty"] = True
        self._refuse(mutate, "R-2: sources.candidate.dirty is true; a dirty tree is refused, "
                             "never waived")

    def test_r3_a_digest_that_does_not_recompute_is_refused(self) -> None:
        def mutate(receipt): receipt["fixture"]["digest"] = "0" * 64
        self._refuse(mutate, "R-3: fixture.digest '000000000000' does not recompute from the "
                             "fixture block ('251a23eb6d0c')")

    def test_r4_a_summary_that_disagrees_with_its_own_rows_is_refused(self) -> None:
        def mutate(receipt):
            for row in receipt["results"]:
                if row["scenario"] == "tenant-own":
                    row["outcome"] = "differ"
                    row["difference"] = {"facet": "body", "field_path": "body:/roles",
                                         "incumbent_digest": "a" * 64,
                                         "candidate_digest": "b" * 64}
        self._refuse(mutate, "R-4: results[3] reports 'differ' for 'tenant-own'; recomputing "
                             "from the observations gives 'agree'")

    def test_r5_a_scenario_side_with_no_row_at_all_is_refused(self) -> None:
        def mutate(receipt):
            receipt["observations"] = [entry for entry in receipt["observations"]
                                       if not (entry["scenario"] == "tenant-own"
                                               and entry["side"] == "candidate")]
        self._refuse(mutate, "R-5: ('tenant-own', 'candidate') is neither observed nor "
                             "recorded as unobserved; an omission is not a pass")

    def test_r6_an_observed_entry_with_no_status_is_refused(self) -> None:
        def mutate(receipt):
            for entry in receipt["observations"]:
                if entry["scenario"] == "tenant-own" and entry["side"] == "candidate":
                    entry["status"] = 0
        self._refuse(mutate, "R-6: observations[7].status must be a positive integer for an "
                             "observed entry, got 0")

    def test_r7_a_silent_mask_is_refused(self) -> None:
        def mutate(receipt):
            for entry in receipt["observations"]:
                if entry["scenario"] == "tenant-own" and entry["side"] == "candidate":
                    entry["body_digest"] = "c" * 64
        self._refuse(mutate, "R-7: observations[7]: body_digest differs from raw_digest with "
                             "no normalization named; a silent mask is not a comparison")

    def test_r8_a_normalized_status_is_refused(self) -> None:
        def mutate(receipt): receipt["policy"]["status_normalized"] = True
        self._refuse(mutate, "R-8: policy.status_normalized is true; a normalized status is "
                             "not a status")

    def test_r8_a_firing_on_the_status_facet_is_refused_whatever_the_flag_says(self) -> None:
        def mutate(receipt):
            receipt["policy"]["normalizations"].append({
                "id": "round-status", "facet": "status", "target": "observation_status.status",
                "kind": "round", "preserves": "none", "reason": "n/a", "source_sha256": "a" * 64})
            receipt["normalization_firings"] = [{
                "scenario": "tenant-own", "side": "candidate", "facet": "status",
                "normalization": "round-status", "before_digest": "1" * 64,
                "after_digest": "2" * 64}]
        # policy.digest no longer recomputes once a normalization is appended,
        # and R-3 is reached before the firings are read -- so the policy digest
        # is patched to what the reader recomputes, leaving only the firing to
        # refuse.
        def mutate_sealed(receipt):
            mutate(receipt)
            policy = receipt["policy"]
            policy["digest"] = hashlib.sha256(canonical_json(
                {"facets": policy["facets"], "normalizations": policy["normalizations"],
                 "exclusions": policy["exclusions"]}).encode("utf-8")).hexdigest()
            run = receipt["run"]
            run["id"] = hashlib.sha256(canonical_json(
                [receipt["check"]["id"], receipt["sources"]["source_digest"],
                 receipt["fixture"]["digest"], receipt["scenario_set"]["digest"],
                 policy["digest"], run["nonce"], run["started_at"]]).encode("utf-8")
            ).hexdigest()[:16]
        self._refuse(mutate_sealed, "R-8: normalization_firings[0] fires 'round-status' on the "
                                    "status facet; a normalized status is not a status")

    def test_r9_a_mask_over_a_real_difference_must_be_exported(self) -> None:
        self._refuse(lambda receipt: receipt.__setitem__("masked_differences", []),
                     "R-9: normalization 'sort-roles' fired on both sides of scenario "
                     "'tenant-own' over differing body values and masked_differences does "
                     "not export it", source=MASKED)

    def test_r10_an_agreement_block_that_does_not_recompute_is_refused(self) -> None:
        def mutate(receipt): receipt["agreement"]["agree"] = 11
        self._refuse(mutate, "R-10: agreement.agree is 11 but recomputing from the "
                             "observations gives 12")

    def test_r11_an_open_completeness_box_with_no_reason_is_refused(self) -> None:
        def mutate(receipt):
            receipt["completeness"]["scenarios"] = {"closed": False, "reason": "  "}
        self._refuse(mutate, "R-11: completeness.scenarios is open with no reason; nothing "
                             "downstream may negate over an unexplained gap")

    def test_r12_a_receipt_without_the_model_conformance_row_is_refused(self) -> None:
        def mutate(receipt):
            receipt["unassessed"] = [row for row in receipt["unassessed"]
                                     if row["dimension"] != "model_conformance"]
        self._refuse(mutate, "R-12: unassessed is missing the mandatory dimensions "
                             "['model_conformance']")

    def test_r13_a_credential_marker_in_the_receipt_is_refused(self) -> None:
        def mutate(receipt): receipt["check"]["title"] = "leaked Bearer abc"
        self._refuse(mutate, "R-13: receipt.json contains the credential marker 'Bearer '")

    def test_r14_a_string_the_public_fork_may_not_carry_is_refused(self) -> None:
        # The banned list is supplied by the caller here so this test file need
        # not spell the real entries; ``ForkHygieneTest`` checks the real ones
        # against the committed bytes.
        def mutate(receipt): receipt["check"]["scope"] = "a bannedword capability"
        self._refuse(mutate, "R-14: receipt.json contains 'bannedword', forbidden in the "
                             "public fork", forbidden=("bannedword",))

    def test_r15_a_receipt_that_does_not_claim_distinct_sides_is_refused(self) -> None:
        """Two distinct sides are the premise of the comparison, not a footnote.

        This was a message appended to a list the judge's summary never printed:
        a receipt that did not claim two distinct processes still exported, still
        derived agreement, and the one sentence saying why that agreement might
        be one process answering twice was dropped on the way to the reader.
        """
        def mutate(receipt): receipt["configuration"]["sides_distinct"] = False
        self._refuse(mutate, "R-15: configuration.sides_distinct is false: the receipt does not "
                             "claim the two sides were distinct processes, so an agreement it "
                             "reports may be one process answering twice")

    def test_r15_a_distinctness_claim_is_checked_against_the_identities(self) -> None:
        """Saying the right word is not the same as describing two sides."""
        def mutate(receipt):
            identity = receipt["configuration"]["side_identity"]
            identity["candidate"] = copy.deepcopy(identity["incumbent"])
        self._refuse(mutate, "R-15: configuration.sides_distinct is true but the two sides "
                             "declare identical side_identity, so nothing distinguishes them")

    def test_every_refusal_rule_has_a_test_above(self) -> None:
        covered = {facts.refusal_rule(name.split("_")[1].upper().replace("R", "R-"))
                   for name in dir(self) if name.startswith("test_r")}
        self.assertEqual(sorted(rule for rule in covered if rule),
                         sorted(facts.REFUSALS))


class ForkHygieneTest(unittest.TestCase):
    """The package carries no banned-identifier list of its own, and the fixtures are neutral.

    R-14 is enforced with the list a caller passes (``forbidden_strings``;
    ``NonconformingReceiptExample`` pins the refusal), so nothing here spells
    the identifiers some private producer must keep out of a receipt.
    """

    def test_the_default_list_is_empty(self) -> None:
        self.assertEqual(facts.FORBIDDEN_FORK_STRINGS, ())

    def test_the_fixtures_use_the_neutral_vocabulary(self) -> None:
        for directory in ALL_FIXTURES:
            receipt = read_receipt(directory)
            self.assertEqual(receipt["sources"]["candidate"]["repo"], "candidate")
            self.assertEqual(receipt["fixture"]["tenant"], "oracle")
            sides = {entry["side"] for entry in receipt["observations"]}
            self.assertLessEqual(sides, {"incumbent", "candidate"})


if __name__ == "__main__":
    unittest.main()
