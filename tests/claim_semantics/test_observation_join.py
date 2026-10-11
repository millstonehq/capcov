"""Judging the four committed receipts, and the cases that must NOT derive agreement.

Every verdict here is read off the closure of the combined bundle -- the
observation pack plus the exported receipt plus the reviewer's three claim-time
rows -- and never off the receipt's own ``results`` or ``agreement`` block.

WHAT IS AND IS NOT EXERCISED.  Soufflé is not required to be installed, so the
verdicts below come from the pure-Python kernel and the certificates from
``static.certificate.certify``, which is engine-independent and re-checked with
``recheck``.  The two-kernel differential (``join.evaluate_join``) is the spec's
own bar for calling any of this verified, and it is SKIPPED where ``souffle`` is
not on PATH -- a skip, never a pass.  ``KernelDifferentialTest`` is that test.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from capcov.claims import canonical_json, validate_bundle
from capcov.claims.evaluator import evaluate
from capcov.claims.ir import Atom, Constant, Context, Evidence
from capcov.claims.static.combine import combine
from capcov.claims.static.certificate import certify, recheck
from capcov.claims.observation import observation_facts as facts
from capcov.claims.observation import join as observation_join


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
AGREE = FIXTURES / "observation_receipt_agree"
GAP = FIXTURES / "observation_receipt_gap"
DISAGREE = FIXTURES / "observation_receipt_disagree"
MASKED = FIXTURES / "observation_receipt_masked"

SIDES = ("incumbent", "candidate")
#: Lane B's vocabulary.  None of it may appear in a Lane A closure.
MODEL_PREMISES = (
    "model_describes_run", "model_observed", "model_admissible", "model_effect",
    "model_writes", "model_scope_exclusion", "model_well_formed", "model_checker_admitted",
    "op_exercised", "op_declared", "corpus_constrains", "mutant", "mutant_killed",
    "surviving_mutant", "php_model_agree", "go_model_agree", "undeclared_write",
    "replay_run_current", "op_qualified", "op_qualified_rt",
)


def reseal(receipt: dict) -> dict:
    receipt = copy.deepcopy(receipt)
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt["receipt_digest"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")).hexdigest()
    return receipt


class _Variant:
    """A committed receipt copied to a tempdir with one edit, resealed."""

    def __init__(self, source: Path, mutate=None, ledger=None) -> None:
        self.source, self.mutate, self.ledger = source, mutate, ledger

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-join-")
        root = Path(self._tmp.name) / "receipt"
        shutil.copytree(self.source, root)
        if self.mutate is not None:
            path = root / facts.RECEIPT_FILE
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.mutate(receipt)
            path.write_text(json.dumps(reseal(receipt), indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        if self.ledger is not None:
            path = root / facts.ADMISSIONS_FILE
            document = json.loads(path.read_text(encoding="utf-8"))
            self.ledger(document)
            path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def judge(directory: Path, **kwargs):
    """Build the join and close it with the Python kernel.

    ``fixture=True`` is the committed-fixture mode: there is no tree on disk for
    these receipts, so source binding and freshness are ASSUMED, not observed.
    ``SourceIdentityTest`` is where that assumption is removed.
    """
    kwargs.setdefault("fixture", True)
    join = observation_join.build(directory, **kwargs)
    if join.bundle is None:
        return join, None, {}
    report = evaluate(join.bundle)
    return join, report, dict(report.relations)


def verdict(report, claim_id: str) -> tuple[str, str]:
    entry = next(result for result in report.claims if result.claim.id == claim_id)
    return entry.semantic.value, entry.operational.value


class CommittedReceiptVerdictTest(unittest.TestCase):
    """The four expected verdicts of spec section 6, by name."""

    def test_the_agreeing_receipt_supports_both_claims(self) -> None:
        join, report, relations = judge(AGREE)
        self.assertEqual(join.contract_findings, [])
        self.assertEqual(validate_bundle(join.bundle), ())
        self.assertEqual(report.status.value, "complete")
        self.assertEqual(verdict(report, join.agree_claim), ("supported", "complete"))
        self.assertEqual(verdict(report, join.stable_claim), ("supported", "complete"))
        self.assertIsNone(observation_join.blocking_premise(relations, join.run))
        self.assertEqual(
            observation_join.qualification(*verdict(report, join.agree_claim)),
            observation_join.QUALIFICATION_AGREEING)

    def test_the_gap_receipt_does_not_derive_and_names_the_gap(self) -> None:
        join, report, relations = judge(GAP)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        blocking = observation_join.blocking_premise(relations, join.run)
        self.assertEqual(blocking["relation"], "observation_gap_any")
        self.assertTrue(blocking["holds"])
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, join.run)}
        self.assertEqual(statuses.get("scenario_timed_out"), "unresolved")
        # A scenario that timed out is not a finding against the candidate.
        self.assertNotIn("scenario_disagree", statuses)

    def test_the_disagreeing_receipt_does_not_derive_and_names_the_difference(self) -> None:
        join, report, relations = judge(DISAGREE)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        blocking = observation_join.blocking_premise(relations, join.run)
        self.assertEqual(blocking["relation"], "observation_disagree_any")
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, join.run)}
        self.assertEqual(statuses.get("scenario_disagree"), "refuted")
        rows = [row for row in relations.get("scenario_disagree", ()) if row[0] == join.run]
        self.assertEqual([row[1] for row in rows], ["tenant-guest"])

    def test_the_masked_receipt_does_not_derive_without_an_admission(self) -> None:
        join, report, relations = judge(MASKED)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        blocking = observation_join.blocking_premise(relations, join.run)
        self.assertEqual(blocking["relation"], "observation_masked_unadmitted")
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, join.run)}
        self.assertEqual(statuses.get("observation_masked_unadmitted"), "refuted")
        # The difference the mask hid is exported whatever the verdict.
        self.assertEqual(observation_join.masked_differences(relations, join.run),
                         [{"scenario": "tenant-own", "facet": "body",
                           "normalization": "sort-roles", "field_path": "body:/roles"}])

    def test_admitting_the_mask_makes_the_same_receipt_derive(self) -> None:
        """The control is a gate, not a wall -- and the pair is what proves it."""
        def admit(document: dict) -> None:
            document["rows"].append({"relation": "masked_difference_admitted",
                                     "normalization": "sort-roles",
                                     "field_path": "body:/roles"})

        with _Variant(MASKED, ledger=admit) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim), ("supported", "complete"))
        self.assertIsNone(observation_join.blocking_premise(relations, join.run))
        # ... and the masked difference is still exported and still reported.
        self.assertEqual(len(observation_join.masked_differences(relations, join.run)), 1)
        self.assertIn("sort-roles", observation_join.normalization_note(relations, join.run))


class MustNotDeriveTest(unittest.TestCase):
    """The shapes that must never come back agreeing."""

    def test_an_empty_scenario_set_is_refused_before_any_judgement(self) -> None:
        def empty(receipt: dict) -> None:
            receipt["scenario_set"]["scenarios"] = []
            receipt["scenario_set"]["count"] = 0

        with _Variant(AGREE, mutate=empty) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("an empty set agrees for free", result.messages[0])

    def test_a_set_that_was_never_exercised_does_not_agree(self) -> None:
        """Every scenario skipped: the set is admitted, and nothing ran."""
        def skip_everything(receipt: dict) -> None:
            receipt["observations"] = []
            receipt["unobserved"] = [{"scenario": scenario["id"], "side": side,
                                      "reason": "skipped"}
                                     for scenario in receipt["scenario_set"]["scenarios"]
                                     for side in SIDES]
            for row in receipt["results"]:
                row["outcome"], row["difference"] = "not_observed", None
            receipt["agreement"] = {"compared": 0, "agree": 0, "differ": 0,
                                    "not_observed": 12, "degenerate": 0, "complete": False}

        with _Variant(AGREE, mutate=skip_everything) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        # set_exercised is empty AND the gap rule fires; the gap is reported
        # first because a missing observation is the more specific finding.
        self.assertEqual(relations.get("set_exercised", ()), ())
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_gap_any")
        self.assertEqual(len(relations.get("scenario_uncompared", ())), 12)

    def test_a_skipped_denial_case_alone_stops_the_claim(self) -> None:
        """One 401 denial scenario dropped on one side; everything else agrees."""
        def skip_denial(receipt: dict) -> None:
            receipt["observations"] = [entry for entry in receipt["observations"]
                                       if not (entry["scenario"] == "anonymous-global"
                                               and entry["side"] == "candidate")]
            receipt["unobserved"] = [{"scenario": "anonymous-global", "side": "candidate",
                                      "reason": "skipped"}]
            for row in receipt["results"]:
                if row["scenario"] == "anonymous-global":
                    row["outcome"] = "not_observed"
            receipt["agreement"] = {"compared": 11, "agree": 11, "differ": 0,
                                    "not_observed": 1, "degenerate": 0, "complete": False}

        with _Variant(AGREE, mutate=skip_denial) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_gap_any")
        uncompared = [row[1] for row in relations.get("scenario_uncompared", ())]
        self.assertEqual(uncompared, ["anonymous-global"])

    def test_a_setup_failure_is_not_a_comparison_and_is_not_a_finding(self) -> None:
        """A harness that could not start reads as unresolved, never as refuted."""
        def setup_failure(receipt: dict) -> None:
            failure = {"stage": "fixture", "code": "seed-refused",
                       "message_digest": "d" * 64}
            kept = [entry for entry in receipt["observations"]
                    if entry["scenario"] != "anonymous-global"]
            receipt["observations"] = [
                {"scenario": "anonymous-global", "side": side, "outcome": "setup_failed",
                 "duration_ms": 0, "failure": dict(failure)} for side in SIDES] + kept
            receipt["unobserved"] = [{"scenario": "anonymous-global", "side": side,
                                      "reason": "setup_failed"} for side in SIDES]
            for row in receipt["results"]:
                if row["scenario"] == "anonymous-global":
                    row["outcome"] = "not_observed"
            receipt["failures"] = [{"phase": "fixture", "class": "setup",
                                    "scenario": "anonymous-global",
                                    "detail": "the fixture refused the anonymous actor seed",
                                    "at": "2026-09-16T09:14:10Z"}]
            receipt["terminal"] = {"outcome": "setup_failed", "exit_code": 1,
                                   "blocking_stage": "fixture",
                                   "reason": "the fixture could not be seeded"}
            receipt["agreement"] = {"compared": 11, "agree": 11, "differ": 0,
                                    "not_observed": 1, "degenerate": 0, "complete": False}

        with _Variant(AGREE, mutate=setup_failure) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        # The harness never reached a compared terminal, and that is reported
        # BEFORE any per-scenario finding: the run did not compare.
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "check_completed")
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, join.run)}
        self.assertEqual(statuses.get("scenario_setup_failed"), "unresolved")
        self.assertNotIn("scenario_disagree", statuses)
        self.assertNotIn("scenario_degenerate", statuses)

    def test_a_compared_terminal_with_a_blocking_failure_is_refused_at_ingest(self) -> None:
        def lying_terminal(receipt: dict) -> None:
            receipt["failures"] = [{"phase": "fixture", "class": "setup", "scenario": None,
                                    "detail": "a store would not reset",
                                    "at": "2026-09-16T09:14:10Z"}]

        with _Variant(AGREE, mutate=lying_terminal) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("a run that failed to set up did not compare", result.messages[0])

    def test_both_sides_identically_off_class_is_degenerate_not_agreement(self) -> None:
        """A pre-registered 2xx scenario that both sides answered 500."""
        def both_broken(receipt: dict) -> None:
            for entry in receipt["observations"]:
                if entry["scenario"] == "support-global":
                    entry["status"], entry["class"] = 500, "5xx"
            for row in receipt["results"]:
                if row["scenario"] == "support-global":
                    row["outcome"] = "degenerate"
            receipt["agreement"] = {"compared": 12, "agree": 11, "differ": 0,
                                    "not_observed": 0, "degenerate": 1, "complete": False}

        with _Variant(AGREE, mutate=both_broken) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_degenerate_any")
        statuses = {entry["relation"]: entry["status"]
                    for entry in observation_join.diagnostics(relations, join.run)}
        self.assertEqual(statuses.get("scenario_degenerate"), "refuted")

    def test_an_unsigned_policy_does_not_derive(self) -> None:
        def unsign_policy(document: dict) -> None:
            document["rows"] = [row for row in document["rows"]
                                if row["relation"] != "policy_admitted"]

        with _Variant(AGREE, ledger=unsign_policy) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "policy_bound")

    def test_an_unadmitted_scenario_set_does_not_derive(self) -> None:
        def unsign_set(document: dict) -> None:
            document["rows"] = [row for row in document["rows"]
                                if row["relation"] != "scenario_set_admitted"]

        with _Variant(AGREE, ledger=unsign_set) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "set_bound")

    def test_an_open_closure_makes_the_negation_unusable(self) -> None:
        """Closure is what licenses a negation; withdraw it and the claim stops."""
        def open_bodies(receipt: dict) -> None:
            receipt["completeness"]["by_side"]["candidate"]["bodies"] = {
                "closed": False,
                "reason": "the candidate's body capture was truncated by the log limit"}

        with _Variant(AGREE, mutate=open_bodies) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_disagreement_closed")


class BlockingOrderTest(unittest.TestCase):
    def test_the_order_is_the_specs_order(self) -> None:
        self.assertEqual([name for name, _ in observation_join._BLOCKING_ORDER], [
            "observation_run_current", "policy_bound", "set_bound", "check_completed",
            "model_conformance_unassessed",
            "observation_failure_gate_closed", "observation_failure_any",
            "observation_gap_closed", "observation_gap_any", "set_exercised",
            "observation_degenerate_closed", "observation_degenerate_any",
            "observation_undeclared_closed", "observation_undeclared_any",
            "scenario_undeclared_closed", "scenario_undeclared_any",
            "observation_masked_gate_closed", "observation_masked_unadmitted",
            "observation_disagreement_closed", "observation_disagree_any",
            # C4: the judge's own copy of the one-sided mask rule, last.
            "observation_mask_disclosure_closed", "observation_mask_undisclosed"])
        self.assertEqual(observation_join.PENDING_PREMISES, ())
        for name, _ in observation_join._BLOCKING_ORDER:
            self.assertIn(name, observation_join.REASONS, name)

    def test_the_first_failure_wins_when_two_premises_fail(self) -> None:
        """A receipt that both times out and disagrees reports the earlier premise."""
        def gap_and_difference(receipt: dict) -> None:
            other = "e" * 64
            receipt["observations"] = [
                entry for entry in receipt["observations"]
                if not (entry["scenario"] == "project-malformed"
                        and entry["side"] == "candidate")]
            for entry in receipt["observations"]:
                if entry["scenario"] == "tenant-guest" and entry["side"] == "candidate":
                    entry["raw_digest"] = entry["body_digest"] = other
            receipt["unobserved"] = [{"scenario": "project-malformed", "side": "candidate",
                                      "reason": "timeout"}]
            for row in receipt["results"]:
                if row["scenario"] == "project-malformed":
                    row["outcome"] = "not_observed"
                if row["scenario"] == "tenant-guest":
                    row["outcome"] = "differ"
                    row["difference"] = {"facet": "body", "field_path": "body:/roles",
                                         "incumbent_digest": "f" * 64,
                                         "candidate_digest": other}
            receipt["agreement"] = {"compared": 11, "agree": 10, "differ": 1,
                                    "not_observed": 1, "degenerate": 0, "complete": False}

        with _Variant(AGREE, mutate=gap_and_difference) as root:
            join, report, relations = judge(root)
        self.assertTrue(relations.get("observation_disagree_any"))
        self.assertTrue(relations.get("observation_gap_any"))
        # gap is ordered before disagreement, so it is what the reviewer is told.
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_gap_any")


class SourceIdentityTest(unittest.TestCase):
    """The receipt must be bound to the tree being judged, and binds nothing by itself."""

    def test_without_a_tree_the_source_row_is_withheld_and_the_claim_stops(self) -> None:
        join, report, relations = judge(AGREE, fixture=False)
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_run_current")
        self.assertEqual(relations.get("observation_source_observed", ()), ())
        self.assertTrue(any("was not observed in any tree" in finding
                            for finding in join.contract_findings), join.contract_findings)

    def test_a_reviewer_nonce_that_is_not_the_receipts_is_stale(self) -> None:
        join, report, relations = judge(AGREE, fixture=False, nonce="a" * 64)
        self.assertTrue(any(finding.startswith("stale:") and "nonce" in finding
                            for finding in join.contract_findings), join.contract_findings)
        self.assertEqual(relations.get("observation_nonce_observed", ()), ())
        self.assertEqual(verdict(report, join.agree_claim)[0], "unresolved")

    def test_a_reviewer_fixture_digest_that_is_not_the_receipts_is_stale(self) -> None:
        join, _report, _relations = judge(AGREE, fixture=False, fixture_digest="b" * 64)
        self.assertTrue(any(finding.startswith("stale:") and "fixture" in finding
                            for finding in join.contract_findings), join.contract_findings)

    @unittest.skipUnless(shutil.which("git"), "git must be on PATH")
    def test_a_tree_that_recomputes_a_different_source_digest_is_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-tree-") as tmp:
            root = Path(tmp) / "candidate"
            root.mkdir()
            (root / "main.go").write_text("package pilot\n", encoding="utf-8")
            git = shutil.which("git")
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                   "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": tmp}
            for args in (("init", "-q", "-b", "main"), ("add", "."),
                         ("commit", "-q", "-m", "candidate")):
                subprocess.run((git, "-C", str(root), *args), check=True,
                               capture_output=True, env=env)
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text('{"prepared": []}\n', encoding="utf-8")
            join = observation_join.build(AGREE, candidate_root=root,
                                          incumbent_manifest=manifest,
                                          nonce=json.loads(
                                              (AGREE / facts.RECEIPT_FILE).read_text()
                                          )["run"]["nonce"])
        relations = dict(evaluate(join.bundle).relations)
        self.assertEqual(join.source_observation.method, "tree")
        self.assertFalse(join.source_observation.matches)
        self.assertTrue(any(finding.startswith("stale:") for finding in join.contract_findings),
                        join.contract_findings)
        self.assertIn("is not a receipt for this tree", join.source_observation.detail)
        self.assertEqual(relations.get("observation_source_observed", ()), ())
        self.assertEqual(observation_join.blocking_premise(relations, join.run)["relation"],
                         "observation_run_current")

    def test_a_receipt_whose_source_digest_does_not_recompute_is_refused(self) -> None:
        def bend(receipt: dict) -> None:
            receipt["sources"]["candidate"]["tree"] = "0" * 40

        with _Variant(AGREE, mutate=bend) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("does not recompute from the candidate tree and incumbent manifest",
                      result.messages[0])


class StabilityTest(unittest.TestCase):
    """Conflict C3: stability is a SECOND claim, optional in the contract."""

    def test_a_receipt_without_a_repeat_block_still_agrees_but_is_not_stable(self) -> None:
        def drop_repeat(receipt: dict) -> None:
            receipt.pop("repeat")
            receipt["unassessed"].append({
                "dimension": "oracle_stability",
                "reason": ("Not assessed. The incumbent was executed once. A scenario that "
                           "happened to agree once is indistinguishable from a stable one.")})

        with _Variant(AGREE, mutate=drop_repeat) as root:
            join, report, relations = judge(root)
        self.assertEqual(verdict(report, join.agree_claim), ("supported", "complete"))
        self.assertEqual(verdict(report, join.stable_claim)[0], "unresolved")
        self.assertEqual(relations.get("observation_stability", ()), ())

    def test_dropping_the_repeat_block_without_saying_so_is_refused(self) -> None:
        with _Variant(AGREE, mutate=lambda receipt: receipt.pop("repeat")) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("unassessed is missing 'oracle_stability'", result.messages[0])


class ModelFreedomTest(unittest.TestCase):
    """No model-dependent premise appears anywhere in the derivation."""

    def test_the_closure_contains_no_lane_b_relation(self) -> None:
        _join, _report, relations = judge(AGREE)
        for name in MODEL_PREMISES:
            self.assertNotIn(name, relations, name)

    def test_the_certificate_rests_only_on_observation_rows(self) -> None:
        join, report, _relations = judge(AGREE)
        certificate = certify(join.bundle, report.relations, "observations_agree",
                              (join.run, join.check, join.scenario_set, join.policy))
        self.assertFalse(certificate["truncated"])
        self.assertTrue(recheck(join.bundle, certificate, report.relations).ok)
        evidence = {record.id: record for record in join.bundle.evidence}
        leaves = [evidence[leaf] for leaf in certificate["leaves"]]
        self.assertTrue(leaves)
        self.assertEqual({record.source.split()[0] for record in leaves},
                         {"observe", "reviewer"})
        for record in leaves:
            name = record.atom.relation
            self.assertNotIn(name, MODEL_PREMISES, name)
            self.assertTrue(name.startswith("observation_") or name.startswith("policy_")
                            or name.startswith("scenario_set_")
                            or name.startswith("masked_"), name)

    def test_the_positive_no_model_premise_is_load_bearing(self) -> None:
        """Remove the model_conformance row and the claim stops deriving.

        The row is refused at ingest (R-12), so the only way to reach the judge
        without it is to keep the dimension and open the closure it rests on --
        which is the same silence, spelled differently, and is also fatal.
        """
        def open_unassessed(receipt: dict) -> None:
            receipt["completeness"]["unassessed"] = {
                "closed": False, "reason": "the dimension list was truncated"}

        with _Variant(AGREE, mutate=open_unassessed) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("an open list of what was not assessed is not a disclosure",
                      result.messages[0])


class UnreachableThroughTheExporterTest(unittest.TestCase):
    """Two premises the exporter's own refusals make unreachable from a receipt.

    ``normalization_undeclared`` (r34) needs a firing whose normalization the
    admitted policy does not declare, and ``scenario_undeclared`` (r37) needs an
    observation of a scenario the admitted set does not declare -- but R-8 and
    R-5 refuse both at ingest, so no receipt this exporter accepts can ever make
    either rule fire.  They are defence in depth behind a stricter gate, and a
    rule that has never been seen to fire is a rule nobody has checked.  Both
    halves are pinned here: the receipt is refused, AND the rule fires when the
    row is placed in the bundle directly.
    """

    @staticmethod
    def _with_row(relation: str, values: dict):
        """The agreeing join -- which derives the claim -- plus one extra row.

        Built on the join rather than on the exported bundle alone so every
        other premise still holds: the only thing that changes the verdict is
        the row placed here, which is what makes the blocking premise below the
        rule under test rather than an unrelated earlier one.
        """
        join = observation_join.build(AGREE, fixture=True)
        decls = {decl.name: decl for decl in join.bundle.relations}
        decl = decls[relation]
        atom = Atom(relation, tuple(Constant(values[column.name], column.type)
                                    for column in decl.columns))
        context = {name: values[name] for name in decl.context_indices}
        record = Evidence(f"observe:direct:{relation}", atom, Context.from_mapping(context),
                          "observe capcov.claims.observation.observation_facts v1", (), "fact")
        bundle = combine(join.bundle, facts=[atom], evidence=[record], validate=False)
        return bundle, join.run

    def test_an_undeclared_normalization_is_refused_at_ingest(self) -> None:
        def undeclared(receipt: dict) -> None:
            receipt["normalization_firings"] = [
                {"scenario": "tenant-own", "side": "candidate", "facet": "body",
                 "normalization": "undeclared-sort", "before_digest": "1" * 64,
                 "after_digest": "2" * 64}]

        with _Variant(AGREE, mutate=undeclared) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("which the admitted policy does not declare as a normalization",
                      result.messages[0])

    def test_but_the_rule_fires_when_such_a_row_reaches_the_bundle(self) -> None:
        receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
        bundle, run = self._with_row("observation_normalization", {
            "run": receipt["run"]["id"], "scenario": "tenant-own", "side": "candidate",
            "facet": "body", "normalization": "undeclared-sort",
            "before_digest": "1" * 64, "after_digest": "2" * 64})
        relations = dict(evaluate(bundle).relations)
        self.assertEqual([row[1] for row in relations.get("normalization_undeclared", ())],
                         ["undeclared-sort"])
        self.assertTrue(relations.get("observation_undeclared_any"))
        self.assertEqual(observation_join.blocking_premise(relations, run)["relation"],
                         "observation_undeclared_any")

    def test_an_observation_of_an_undeclared_scenario_is_refused_at_ingest(self) -> None:
        def ghost(receipt: dict) -> None:
            receipt["observations"].append(
                {"scenario": "ghost-scenario", "side": "candidate", "outcome": "observed",
                 "status": 200, "class": "2xx", "raw_digest": "3" * 64,
                 "body_digest": "3" * 64, "normalizations_applied": [],
                 "effects_observed": False, "effects": [], "duration_ms": 1,
                 "failure": None})

        with _Variant(AGREE, mutate=ghost) as root:
            result = _export_fixture(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("which the admitted set does not declare", result.messages[0])

    def test_but_the_scenario_rule_fires_when_such_a_row_reaches_the_bundle(self) -> None:
        receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
        bundle, run = self._with_row("observation_observed", {
            "run": receipt["run"]["id"], "scenario": "ghost-scenario", "side": "candidate"})
        relations = dict(evaluate(bundle).relations)
        self.assertEqual([row[1] for row in relations.get("scenario_undeclared", ())],
                         ["ghost-scenario"])
        self.assertTrue(relations.get("scenario_undeclared_any"))
        self.assertEqual(observation_join.blocking_premise(relations, run)["relation"],
                         "scenario_undeclared_any")


class ReportTest(unittest.TestCase):
    """Section 5.6: the same disclosures print whether or not the claim holds."""

    def test_the_summary_prints_the_count_the_histogram_and_the_caveats(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        report = observation_join.summary(join)
        # Not evaluated through the differential here; the disclosures print anyway.
        self.assertEqual(report["status"], "not-evaluated")
        self.assertEqual(report["scenario_count"], 12)
        self.assertEqual(report["scenario_kinds"],
                         {"denial": 6, "malformed": 2, "success": 4})
        self.assertEqual(report["recomputed_notice"], observation_join.RECOMPUTED_NOTICE)
        self.assertIn("not a correctness claim", report["not_a_claim"])
        self.assertEqual(report["qualification"],
                         observation_join.QUALIFICATION_UNSUPPORTED)
        self.assertNotIn("stability_notice", report)

    def test_every_unassessed_row_is_reprinted_verbatim(self) -> None:
        join, report, relations = judge(AGREE)
        rows = observation_join.unassessed_rows(relations, join.run)
        self.assertEqual(len(rows), 10)
        dimensions = {row["dimension"] for row in rows}
        self.assertIn("model_conformance", dimensions)
        self.assertIn("producer_independence", dimensions)
        self.assertIn("persisted_effects", dimensions)
        self.assertNotIn("oracle_stability", dimensions)
        receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
        declared = {row["dimension"]: row["reason"] for row in receipt["unassessed"]}
        for row in rows:
            self.assertEqual(row["reason"], declared[row["dimension"]])

    def test_the_verdict_word_is_never_qualified(self) -> None:
        self.assertEqual(observation_join.QUALIFICATION_AGREEING, "agreeing")
        self.assertEqual(observation_join.QUALIFICATION_UNSUPPORTED, "unsupported")
        self.assertNotIn("qualified", observation_join.SUPPORTED_STATEMENT)

    def test_no_normalization_note_where_nothing_fired(self) -> None:
        _join, _report, relations = judge(AGREE)
        self.assertIsNone(observation_join.normalization_note(
            relations, json.loads((AGREE / facts.RECEIPT_FILE).read_text())["run"]["id"]))


@unittest.skipUnless(shutil.which("souffle"),
                     "souffle must be on PATH: the two-kernel differential is the spec's "
                     "own bar and is NOT met without it")
class KernelDifferentialTest(unittest.TestCase):
    """The spec's missing premise: both kernels agreeing on identical certificates."""

    def test_the_agreeing_receipt_derives_identically_in_both_kernels(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        join = observation_join.evaluate_join(join, replay_root=str(HERE))
        self.assertIsNone(join.mismatch)
        self.assertTrue(join.result.matched)
        report = observation_join.summary(join)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["observations_agree"], "supported")
        self.assertEqual(report["qualification"], observation_join.QUALIFICATION_AGREEING)
        self.assertEqual(report["incumbent_stable"], "supported")
        self.assertIn("statement", report)


if __name__ == "__main__":
    unittest.main()
