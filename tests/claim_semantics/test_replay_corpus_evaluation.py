"""The one place the replay review tables meet an engine (Phase 4, judge side).

Every reviewed expectation is compared with the Python reference evaluator
and, when Souffle is on PATH, both kernels are compared through the
fail-closed differential.  A disagreement is a finding to investigate, never
a reason to edit the expectation in place.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest

try:
    from .replay_rules.adapter import CASES_DIR, case_paths, load_case, load_pack, read_json
    from .replay_rules.cases import (CLOSE, CREATE, DELETE, DELETE_TARGET, FIRST_DELETE, INDEX, REPEAT_DELETE,
                                     RUN)
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules.adapter import CASES_DIR, case_paths, load_case, load_pack, read_json
    from replay_rules.cases import (CLOSE, CREATE, DELETE, DELETE_TARGET, FIRST_DELETE, INDEX, REPEAT_DELETE,
                                    RUN)

from capcov.claims import canonical_json
from capcov.claims.ir import BundleIngestionError
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.output import VerifiedProofEvidence, render_outputs


def sorted_json(values):
    return sorted(canonical_json(value) for value in values)


class PythonEvaluatorAgreesWithReviewedExpectations(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()

    def _report(self, stem: str):
        return evaluate(load_case(CASES_DIR / f"{stem}.json", self.pack))

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
                    self.assertEqual(result.basis.value, "derivational")
                    # an unresolved op_qualified names exactly one absent leaf
                    if result.semantic.value == "unresolved":
                        self.assertEqual(len(result.missing_premises), 1)
                        missing = {item["relation"] for item in result.missing_premises}
                        rendered_missing = render_outputs(bundle, entry.claim.id, evidence_ids, None,
                                                          "unresolved", missing)
                        self.assertEqual([item["relation"] for item in rendered_missing], sorted(missing))

    def test_control_case_qualifies_exactly_the_three_declared_ops(self) -> None:
        report = self._report("00-positive-control")
        every_op = {(INDEX, RUN, op) for op in (CREATE, CLOSE, DELETE)}
        self.assertEqual(set(report.relation_rows("op_qualified")), every_op)
        self.assertEqual(set(report.relation_rows("op_qualified_rt")), set(report.relation_rows("op_qualified")))
        self.assertEqual(set(report.relation_rows("replayed")), {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(set(report.relation_rows("corpus_constrains")), {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(len(report.relation_rows("php_model_agree")), 5)
        self.assertEqual(len(report.relation_rows("go_model_agree")), 5)
        for empty in ("php_model_disagree", "go_model_disagree", "undeclared_write", "surviving_mutant",
                      "kill_closure_gap", "replay_run_stale", "php_disagree_any", "go_disagree_any", "undeclared_any"):
            self.assertEqual(report.relation_rows(empty), (), empty)
        self.assertEqual(report.relation_rows("replay_run_current"), ((RUN,),))

    def test_control_case_derives_the_order_repeat_and_stability_verdicts(self) -> None:
        report = self._report("00-positive-control")
        for empty in ("effect_order_violation", "effect_order_any", "repeat_delete_has_effect",
                      "repeat_delete_violation", "repeat_delete_any", "oracle_unstable"):
            self.assertEqual(report.relation_rows(empty), (), empty)
        # the order is not merely unviolated, it is exercised on both sides
        self.assertEqual(set(report.relation_rows("effect_order_respected")),
                         {(RUN, side, req) for side in ("php", "go") for req in ("req-1", "req-2", "req-4")})
        self.assertEqual(set(report.relation_rows("effect_order_exercised")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(set(report.relation_rows("effect_order_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        # the repeat delete: req-5 repeats req-4's target, 404 on both sides, no effect
        self.assertEqual(set(report.relation_rows("repeat_delete")), {(RUN, REPEAT_DELETE, DELETE_TARGET)})
        self.assertEqual(set(report.relation_rows("first_delete_committed")), {(RUN, FIRST_DELETE, DELETE_TARGET)},
                         "req-4 is the first delete of the target, not merely a committing one")
        self.assertEqual(report.relation_rows("earlier_delete"), ((RUN, "tenant-a", DELETE_TARGET, 5),),
                         "only req-5 has a delete of the same target before it")
        self.assertEqual(set(report.relation_rows("repeat_delete_not_found")), {(RUN, DELETE_TARGET)})
        self.assertEqual(set(report.relation_rows("repeat_delete_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(report.relation_rows("oracle_stable"), ((RUN,),))

    def test_planted_faults_derive_exactly_their_blocker(self) -> None:
        report = self._report("01-planted-disagreement")
        self.assertEqual(len(report.relation_rows("php_model_disagree")), 1)
        self.assertEqual(report.relation_rows("go_model_disagree"), ())
        self.assertEqual(set(report.relation_rows("php_disagree_any")), {(RUN, CLOSE)})
        self.assertEqual(set(report.relation_rows("op_qualified")), {(INDEX, RUN, CREATE), (INDEX, RUN, DELETE)})
        report = self._report("02-planted-undeclared-write")
        self.assertEqual(set(report.relation_rows("undeclared_write")), {(RUN, CLOSE, "audit_log")})
        self.assertEqual(set(report.relation_rows("op_qualified")), {(INDEX, RUN, CREATE), (INDEX, RUN, DELETE)})
        report = self._report("03-surviving-mutant")
        self.assertEqual(set(report.relation_rows("surviving_mutant")), {(RUN, CLOSE, "m-2")})
        self.assertEqual(set(report.relation_rows("corpus_constrains")), {(RUN, CREATE), (RUN, DELETE)})
        self.assertEqual(set(report.relation_rows("op_qualified")), {(INDEX, RUN, CREATE), (INDEX, RUN, DELETE)})

    def test_missing_witnesses_leave_no_derived_rows_behind(self) -> None:
        report = self._report("04-missing-model-witness")
        for relation in ("php_model_agree", "php_model_disagree", "go_model_agree", "go_model_disagree",
                         "corpus_constrains", "op_surviving_closed", "php_disagreement_closed",
                         "undeclared_writes_closed", "op_qualified_rt", "op_qualified"):
            self.assertEqual(report.relation_rows(relation), (), relation)
        self.assertEqual(report.relation_rows("replay_run_current"), ((RUN,),))
        report = self._report("05-missing-snapshot-witness")
        self.assertEqual(report.relation_rows("replay_run_current"), ())
        self.assertEqual(report.relation_rows("replay_run_stale"), ())
        self.assertEqual(len(report.relation_rows("php_model_agree")), 5)
        self.assertEqual(report.relation_rows("op_qualified"), ())

    def test_open_tables_block_closure_without_touching_the_observations(self) -> None:
        report = self._report("10-missing-effects-closure")
        self.assertEqual(report.relation_rows("php_effects_closed"), ())
        self.assertEqual(report.relation_rows("go_effects_closed"), ((RUN,),))
        self.assertEqual(report.relation_rows("undeclared_writes_closed"), ())
        self.assertEqual(set(report.relation_rows("php_disagreement_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(set(report.relation_rows("corpus_constrains")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(report.relation_rows("undeclared_write"), ())
        self.assertEqual(report.relation_rows("op_qualified"), ())
        report = self._report("11-missing-admissible-closure")
        self.assertEqual(report.relation_rows("model_admissible_closed"), ())
        self.assertEqual(len(report.relation_rows("model_describes_run")), 1)
        self.assertEqual(len(report.relation_rows("php_model_agree")), 5)
        self.assertEqual(report.relation_rows("php_model_disagree"), ())
        self.assertEqual(report.relation_rows("php_disagreement_closed"), ())
        self.assertEqual(set(report.relation_rows("undeclared_writes_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(report.relation_rows("op_qualified"), ())

    def test_reviewer_exclusions_are_explicit_and_closed_or_nothing(self) -> None:
        report = self._report("13-excluded-undeclared-write")
        self.assertEqual({row[1] for row in report.relation_rows("model_scope_excluded")},
                         {"authentication", "redis", "audit_log"})
        self.assertEqual(set(report.relation_rows("exclusion_applied")), {(RUN, CLOSE, "audit_log")})
        self.assertEqual(report.relation_rows("undeclared_write"), ())
        self.assertEqual(set(report.relation_rows("op_qualified")),
                         {(INDEX, RUN, op) for op in (CREATE, CLOSE, DELETE)})
        # the same write without the exclusion (case 02) blocks the op
        self.assertEqual(set(self._report("02-planted-undeclared-write").relation_rows("undeclared_write")),
                         {(RUN, CLOSE, "audit_log")})
        for stem in ("14-exclusions-not-closed", "15-no-exclusions-no-closure"):
            report = self._report(stem)
            self.assertEqual(report.relation_rows("model_scope_exclusions_closed"), (), stem)
            self.assertEqual(report.relation_rows("model_scope_excluded_closed"), (), stem)
            self.assertEqual(report.relation_rows("undeclared_write"), (), stem)
            self.assertEqual(report.relation_rows("undeclared_writes_closed"), (), stem)
            self.assertEqual(report.relation_rows("op_qualified"), (), stem)
        self.assertEqual({row[1] for row in self._report("14-exclusions-not-closed").relation_rows("model_scope_excluded")},
                         {"authentication", "redis", "audit_log"})
        self.assertEqual(self._report("15-no-exclusions-no-closure").relation_rows("model_scope_excluded"), ())

    def test_stale_case_derives_replay_run_stale_and_not_replay_run_current(self) -> None:
        report = self._report("06-stale-replay")
        self.assertEqual(report.relation_rows("replay_run_current"), ())
        self.assertEqual(len(report.relation_rows("replay_run_stale")), 1)
        self.assertEqual(report.relation_rows("op_qualified"), ())

    def test_lying_closure_poisons_the_run_and_exposes_the_gap(self) -> None:
        report = self._report("08-lying-closure")
        self.assertEqual(set(report.relation_rows("kill_closure_gap")), {(RUN, "m-1", "req-9")})
        self.assertEqual(set(report.relation_rows("mutant_killed_in")), {(RUN, "m-1"), (RUN, "m-2"), (RUN, "m-3")})
        self.assertEqual(report.relation_rows("surviving_mutant"), ())
        # without the contradiction gate the lie would qualify every op
        self.assertEqual(set(report.relation_rows("corpus_constrains")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(set(report.relation_rows("kill_closure_gap_any")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(report.relation_rows("kill_gap_closed"), ((RUN,),))
        self.assertEqual(report.relation_rows("op_qualified"), ())


    def test_missing_post_state_is_a_gap_only_the_gate_catches(self) -> None:
        report = self._report("09-missing-post-state")
        self.assertEqual(set(report.relation_rows("post_state_gap")), {(RUN, "req-3", "php")})
        self.assertEqual(set(report.relation_rows("post_state_any")), {(RUN, CREATE)})
        # the hole the gate closes: op_exercised and both disagreement closures still hold for issues.create
        self.assertIn((RUN, CREATE), set(report.relation_rows("op_exercised")))
        self.assertIn((RUN, CREATE), set(report.relation_rows("php_disagreement_closed")))
        self.assertEqual(report.relation_rows("php_disagree_any"), ())
        self.assertEqual(set(report.relation_rows("op_qualified")), {(INDEX, RUN, CLOSE), (INDEX, RUN, DELETE)})


    def test_order_violation_blocks_only_the_op_whose_request_is_out_of_order(self) -> None:
        report = self._report("17-effect-order-violation")
        self.assertEqual(set(report.relation_rows("effect_order_violation")),
                         {(RUN, "go", "req-1", "issue", "entity_statistics")},
                         "the model puts issue before entity_statistics; Go observed the reverse")
        self.assertEqual(set(report.relation_rows("effect_order_any")), {(RUN, CREATE)})
        self.assertEqual(set(report.relation_rows("op_qualified")), {(INDEX, RUN, CLOSE), (INDEX, RUN, DELETE)})
        # the folded effect tables are identical to the control's: only the order differs
        control = self._report("00-positive-control")
        for relation in ("php_effect_seq", "go_effect", "php_effect", "undeclared_write"):
            self.assertEqual(set(report.relation_rows(relation)), set(control.relation_rows(relation)), relation)
        self.assertNotEqual(set(report.relation_rows("go_effect_seq")), set(control.relation_rows("go_effect_seq")))

    def test_a_repeat_delete_that_writes_is_a_violation_and_defeats_the_claim(self) -> None:
        report = self._report("18-repeat-delete-with-effects")
        self.assertEqual(set(report.relation_rows("repeat_delete_has_effect")), {(RUN, REPEAT_DELETE)})
        self.assertEqual(set(report.relation_rows("repeat_delete_violation")), {(RUN, REPEAT_DELETE, "effects")})
        self.assertEqual(set(report.relation_rows("repeat_delete_any")), {(RUN, DELETE)})
        self.assertEqual(report.relation_rows("repeat_delete_not_found"), (),
                         "the negated premise fails: the repeat is not effect-free")
        self.assertEqual(set(report.relation_rows("op_qualified")),
                         {(INDEX, RUN, CREATE), (INDEX, RUN, CLOSE)},
                         "op_qualified_rt is gated on the repeat, and the repeat belongs to delete-issue")
        self.assertEqual(set(report.relation_rows("repeat_delete_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)},
                         "the closure derives for every replayed op; only the violation is per-request")
        # the status pair is still 200/404, so no status violation was planted
        self.assertNotIn((RUN, REPEAT_DELETE, "php"), set(report.relation_rows("repeat_delete_violation")))

    def test_an_excluded_bookkeeping_write_on_the_repeat_is_not_a_finding(self) -> None:
        report = self._report("23-repeat-delete-excluded-write")
        self.assertEqual(report.relation_rows("repeat_delete_has_effect"), (),
                         "authentication is a reviewer-excluded table: the touch is not an effect the claim counts")
        self.assertEqual(report.relation_rows("repeat_delete_violation"), ())
        self.assertEqual(set(report.relation_rows("repeat_delete_not_found")), {(RUN, DELETE_TARGET)})
        self.assertEqual(set(report.relation_rows("exclusion_applied")), {(RUN, DELETE, "authentication")})
        self.assertEqual(report.relation_rows("undeclared_write"), ())
        self.assertEqual(set(report.relation_rows("op_qualified")),
                         {(INDEX, RUN, op) for op in (CREATE, CLOSE, DELETE)})
        # the same row on a table the reviewer did not exclude is case 18's finding
        self.assertEqual(set(self._report("18-repeat-delete-with-effects").relation_rows("repeat_delete_has_effect")),
                         {(RUN, REPEAT_DELETE)})

    def test_the_first_delete_is_the_first_one_and_a_committed_one(self) -> None:
        inverted = self._report("24-repeat-before-the-commit")
        self.assertEqual(set(inverted.relation_rows("earlier_delete")),
                         {(RUN, "tenant-a", DELETE_TARGET, 5)},
                         "req-4 now sits at tape position 5, behind the 404")
        self.assertEqual(inverted.relation_rows("first_delete_committed"), (),
                         "the first delete of the target answered 404; the committing one is not the first")
        self.assertEqual(inverted.relation_rows("repeat_delete_violation"), (),
                         "no violation is reported against a tape whose first delete never committed")
        self.assertEqual(inverted.relation_rows("repeat_delete_not_found"), ())
        self.assertEqual(set(inverted.relation_rows("op_qualified")),
                         {(INDEX, RUN, op) for op in (CREATE, CLOSE, DELETE)})

        uncommitted = self._report("25-first-delete-not-committed")
        self.assertEqual(uncommitted.relation_rows("first_delete_committed"), (),
                         "a 200 whose side wrote no issue update did not commit the delete")
        self.assertEqual(uncommitted.relation_rows("repeat_delete_not_found"), ())
        self.assertEqual(set(uncommitted.relation_rows("repeat_delete")), {(RUN, REPEAT_DELETE, DELETE_TARGET)})
        self.assertEqual(set(uncommitted.relation_rows("op_qualified")), {(INDEX, RUN, CREATE), (INDEX, RUN, CLOSE)})

    def test_an_unstable_oracle_disqualifies_every_op_of_the_run(self) -> None:
        report = self._report("19-unstable-oracle")
        self.assertEqual(report.relation_rows("oracle_unstable"), ((RUN,),))
        self.assertEqual(report.relation_rows("oracle_stable"), ())
        self.assertEqual(report.relation_rows("op_qualified"), ())
        # the per-request relations still derive: only the cross-run verdict changed
        self.assertEqual(set(report.relation_rows("corpus_constrains")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(report.relation_rows("effect_order_violation"), ())
        self.assertEqual(set(report.relation_rows("repeat_delete_not_found")), {(RUN, DELETE_TARGET)})

    def test_one_unstable_side_is_enough_and_the_negation_is_what_refuses(self) -> None:
        report = self._report("26-unstable-on-one-side")
        self.assertEqual(report.relation_rows("oracle_unstable"), ((RUN,),))
        self.assertEqual(report.relation_rows("oracle_stable"), ())
        self.assertEqual(report.relation_rows("op_qualified"), ())
        # oracle_stable's own positive premise (a "true" php row) still holds here,
        # which is what makes the negated atom load-bearing
        rows = {row[3:] for row in report.relation_rows("replay_stability")}
        self.assertEqual(rows, {("php", "true"), ("go", "false")})

        def drop_negation(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "oracle_stable")
            rule["body"] = [a for a in rule["body"]
                            if a.get("relation") not in {"oracle_unstable", "oracle_unstable_closed"}]
        pack = json.loads(json.dumps(load_pack()))
        drop_negation(pack)
        mutated = evaluate(load_case(CASES_DIR / "26-unstable-on-one-side.json", pack))
        self.assertEqual(mutated.status.value, "complete", mutated.message)
        self.assertEqual(mutated.relation_rows("oracle_stable"), ((RUN,),),
                         "without !oracle_unstable the unstable side is invisible")
        self.assertEqual(len(mutated.relation_rows("op_qualified")), 3)
        # case 19 flips the only row instead, so it cannot catch that mutation
        nineteen = evaluate(load_case(CASES_DIR / "19-unstable-oracle.json", pack))
        self.assertEqual(nineteen.relation_rows("oracle_stable"), ())

    def test_an_open_new_table_blocks_the_gate_that_needs_it_and_no_other(self) -> None:
        stability = self._report("20-missing-stability-closure")
        self.assertEqual(stability.relation_rows("replay_stability_closed"), ())
        self.assertEqual(stability.relation_rows("oracle_stable"), ())
        self.assertEqual(stability.relation_rows("oracle_unstable"), (),
                         "the row says stable = true; it is the closure that is missing")
        self.assertEqual(set(stability.relation_rows("effect_order_closed")),
                         {(RUN, op) for op in (CREATE, CLOSE, DELETE)})
        self.assertEqual(stability.relation_rows("op_qualified"), ())

        sequence = self._report("21-missing-effect-seq-closure")
        self.assertEqual(sequence.relation_rows("php_effect_seqs_closed"), ())
        self.assertEqual(sequence.relation_rows("go_effect_seqs_closed"), ((RUN,),))
        self.assertEqual(sequence.relation_rows("effect_order_closed"), ())
        self.assertEqual(sequence.relation_rows("effect_order_violation"), (),
                         "the rows are all there and all in order; only the closure is absent")
        self.assertEqual(sequence.relation_rows("oracle_stable"), ((RUN,),))
        self.assertEqual(sequence.relation_rows("op_qualified"), ())
        for entry in evaluate(load_case(CASES_DIR / "21-missing-effect-seq-closure.json", self.pack)).claims:
            if entry.claim.id.startswith("claim-qualified-"):
                self.assertEqual([item["relation"] for item in entry.result.missing_premises],
                                 ["php_effect_seqs_closed"], entry.claim.id)


class PackMutationsFailTheCorpus(unittest.TestCase):
    """Dropping a post-state closure input, or the gate, is caught by the corpus."""

    def _mutated(self, mutate):
        pack = json.loads(json.dumps(load_pack()))
        mutate(pack)
        return pack

    def _create_verdict(self, pack) -> str:
        report = evaluate(load_case(CASES_DIR / "09-missing-post-state.json", pack))
        self.assertEqual(report.status.value, "complete", report.message)
        return next(entry.result.semantic.value for entry in report.claims if entry.claim.id == "claim-qualified-create")

    def test_the_reviewed_pack_leaves_the_planted_gap_unresolved(self) -> None:
        self.assertEqual(self._create_verdict(load_pack()), "unresolved")

    def _repeat_claim_verdict(self, pack, stem: str = "23-repeat-delete-excluded-write") -> str:
        report = evaluate(load_case(CASES_DIR / f"{stem}.json", pack))
        self.assertEqual(report.status.value, "complete", report.message)
        return next(entry.result.semantic.value for entry in report.claims
                    if entry.claim.id == "claim-repeat-delete-not-found")

    def test_dropping_the_exclusion_guard_from_the_repeat_flips_the_excluded_write_case(self) -> None:
        # the guard the live systems need: a 404 repeat still authenticates, so both
        # sides write the excluded session row and nothing else
        self.assertEqual(self._repeat_claim_verdict(load_pack()), "supported")

        def drop_guard(pack):
            for name in ("repeat_delete_has_effect_php", "repeat_delete_has_effect_go"):
                rule = next(r for r in pack["rules"] if r["name"] == name)
                rule["body"] = [a for a in rule["body"]
                                if a.get("relation") not in {"model_describes_run", "model_scope_excluded_closed",
                                                             "model_scope_excluded"}]
        self.assertEqual(self._repeat_claim_verdict(self._mutated(drop_guard)), "unresolved")
        # and the guard does not excuse a write to a table the reviewer did not exclude
        self.assertEqual(self._repeat_claim_verdict(load_pack(), "18-repeat-delete-with-effects"), "unresolved")

    def test_the_first_delete_conditions_are_load_bearing(self) -> None:
        def relations(pack, stem):
            report = evaluate(load_case(CASES_DIR / f"{stem}.json", pack))
            self.assertEqual(report.status.value, "complete", report.message)
            return report

        def drop_earlier_delete(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "first_delete_committed")
            rule["body"] = [a for a in rule["body"]
                            if a.get("relation") not in {"earlier_delete", "earlier_delete_closed"}]
        # without the "no earlier delete" condition the pack reads the tape of case 24
        # backwards: the committing delete becomes the repeat and is reported as a violation
        mutated = relations(self._mutated(drop_earlier_delete), "24-repeat-before-the-commit")
        self.assertEqual(len(mutated.relation_rows("first_delete_committed")), 1)
        self.assertEqual({row[2] for row in mutated.relation_rows("repeat_delete_violation")}, {"php", "go", "effects"})

        def drop_effect_premises(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "first_delete_committed")
            rule["body"] = [a for a in rule["body"] if a.get("relation") not in {"php_effect", "go_effect"}]
        # without them a 200 that wrote no issue update would count as a commit and
        # license the repeat claim (case 25)
        self.assertEqual(self._repeat_claim_verdict(load_pack(), "25-first-delete-not-committed"), "unresolved")
        self.assertEqual(self._repeat_claim_verdict(self._mutated(drop_effect_premises),
                                                    "25-first-delete-not-committed"), "supported")

    def _qualified_verdict(self, pack, stem: str) -> str:
        report = evaluate(load_case(CASES_DIR / f"{stem}.json", pack))
        self.assertEqual(report.status.value, "complete", report.message)
        return next(entry.result.semantic.value for entry in report.claims
                    if entry.claim.id == "claim-qualified-create")

    def test_dropping_a_half_of_the_well_formedness_premise_flips_its_case(self) -> None:
        """Each half of the Stage D premise is load-bearing on its own case."""
        for stem, relation in (("27-model-not-well-formed", "model_well_formed"),
                               ("28-well-formed-other-model", "model_well_formed"),
                               ("29-checker-not-admitted", "model_checker_admitted")):
            with self.subTest(case=stem):
                self.assertEqual(self._qualified_verdict(load_pack(), stem), "unresolved")

                def drop(pack, relation=relation):
                    rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
                    rule["body"] = [a for a in rule["body"] if a.get("relation") != relation]

                self.assertEqual(self._qualified_verdict(self._mutated(drop), stem), "supported")
        # and dropping the model binding lets case 28's foreign certificate through
        def drop_binding(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
            rule["body"] = [a for a in rule["body"] if a.get("relation") != "model_describes_run"]

        self.assertEqual(self._qualified_verdict(self._mutated(drop_binding), "28-well-formed-other-model"),
                         "supported", "without the binding the certificate need not be this model's")
        self.assertEqual(self._qualified_verdict(self._mutated(drop_binding), "27-model-not-well-formed"),
                         "unresolved", "an absent certificate is absent however the join is written")

    def _delete_verdict(self, pack, stem: str) -> str:
        report = evaluate(load_case(CASES_DIR / f"{stem}.json", pack))
        self.assertEqual(report.status.value, "complete", report.message)
        return next(entry.result.semantic.value for entry in report.claims
                    if entry.claim.id == "claim-qualified-delete")

    def test_dropping_the_repeat_gate_from_op_qualified_rt_flips_the_planted_case(self) -> None:
        """The cross-request claim must be load-bearing, not merely derived.

        Case 18 plants an ``issue`` update on the second delete of a committed
        target.  Every per-request premise still holds, so with the gate removed the
        op qualifies and the receipt's only defect is reported in a claim nothing
        reads.  With the gate the op is unresolved, and the two halves are checked
        separately: the negation alone and its completeness alone.
        """
        self.assertEqual(self._delete_verdict(load_pack(), "18-repeat-delete-with-effects"), "unresolved")

        def drop_gate(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
            rule["body"] = [a for a in rule["body"]
                            if a.get("relation") not in {"repeat_delete_any", "repeat_delete_closed"}]

        self.assertEqual(self._delete_verdict(self._mutated(drop_gate), "18-repeat-delete-with-effects"), "supported")

        # the negation alone: without it the closure is a premise that proves nothing
        def drop_negation(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
            rule["body"] = [a for a in rule["body"] if a.get("relation") != "repeat_delete_any"]

        self.assertEqual(self._delete_verdict(self._mutated(drop_negation), "18-repeat-delete-with-effects"),
                         "supported")
        # the other ops of the same run are untouched either way: the gate is per op
        self.assertEqual(self._qualified_verdict(load_pack(), "18-repeat-delete-with-effects"), "supported")

    def test_the_repeat_closure_is_what_licenses_the_negation(self) -> None:
        """Case 31 opens the PHP response table; the gate withholds instead of passing.

        Dropping ``repeat_delete_closed`` from the op gate makes the negation
        unlicensed-but-free, and the case qualifies on a table the harness never
        claimed was complete.  Dropping either response closure from
        ``repeat_delete_closed`` itself does the same, which is what makes listing
        every positive input of the negated chain load-bearing rather than decorative.
        """
        self.assertEqual(self._delete_verdict(load_pack(), "31-missing-response-closure"), "unresolved")

        def drop_closure(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
            rule["body"] = [a for a in rule["body"] if a.get("relation") != "repeat_delete_closed"]

        # the strongest form: the closure cannot even be dropped.  Without it the
        # negation is unsafe and the ingestion boundary refuses the pack outright,
        # rather than letting a rule negate a table nobody closed.
        with self.assertRaises(BundleIngestionError) as caught:
            self._delete_verdict(self._mutated(drop_closure), "31-missing-response-closure")
        self.assertIn("missing-completeness", str(caught.exception))
        self.assertIn("repeat_delete_any", str(caught.exception))

        def drop_response_input(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "repeat_delete_closed")
            rule["body"] = [a for a in rule["body"] if a.get("relation") != "php_responses_closed"]

        self.assertEqual(self._delete_verdict(self._mutated(drop_response_input), "31-missing-response-closure"),
                         "supported")

    def test_dropping_the_gate_from_op_qualified_rt_flips_the_case(self) -> None:
        def drop_gate(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "op_qualified_rt")
            rule["body"] = [a for a in rule["body"] if a.get("relation") not in {"post_state_any", "post_state_gap_closed"}]
        self.assertEqual(self._create_verdict(self._mutated(drop_gate)), "supported")

    def test_dropping_the_post_state_closure_from_a_closure_rule_flips_a_witnessless_variant(self) -> None:
        # With php_post_states_closed withheld the gate is open only because the
        # closures that carry it are absent; a closure rule that no longer lists
        # the witness re-admits the omission.
        case = read_json(CASES_DIR / "09-missing-post-state.json")
        case["facts"] = [f for f in case["facts"] if f["relation"] != "php_post_states_closed"]
        case["outputs"] = [o for o in case["outputs"]
                           if not (set(o.get("excludes_evidence", ())) & {f["id"] for f in read_json(CASES_DIR / "09-missing-post-state.json")["facts"] if f["relation"] == "php_post_states_closed"})]
        from capcov.claims import bundle_from_json
        try:
            from .replay_rules.adapter import bundle_payload
        except ImportError:
            from replay_rules.adapter import bundle_payload

        def verdict(pack):
            report = evaluate(bundle_from_json(bundle_payload(case, pack), validate=True))
            self.assertEqual(report.status.value, "complete", report.message)
            return next(entry.result.semantic.value for entry in report.claims if entry.claim.id == "claim-qualified-create")

        self.assertEqual(verdict(load_pack()), "unresolved")

        def drop_witness_inputs_and_the_php_gap_rule(pack):
            for name in ("php_disagreement_closed", "post_state_gap_closed"):
                rule = next(r for r in pack["rules"] if r["name"] == name)
                rule["body"] = [a for a in rule["body"] if a.get("relation") != "php_post_states_closed"]
            pack["rules"] = [r for r in pack["rules"] if r["name"] != "post_state_gap_php"]
        # with the witness no longer an input to either closure and no PHP gap
        # rule, the omitted post-state is admitted: the inputs are load-bearing
        self.assertEqual(verdict(self._mutated(drop_witness_inputs_and_the_php_gap_rule)), "supported")

        def drop_from_one_closure(pack):
            rule = next(r for r in pack["rules"] if r["name"] == "php_disagreement_closed")
            rule["body"] = [a for a in rule["body"] if a.get("relation") != "php_post_states_closed"]
        # one closure alone cannot re-admit it: the gate still needs post_state_gap_closed
        self.assertEqual(verdict(self._mutated(drop_from_one_closure)), "unresolved")

        def drop_witness_inputs_but_keep_the_gap_rule(pack):
            for name in ("php_disagreement_closed", "post_state_gap_closed"):
                rule = next(r for r in pack["rules"] if r["name"] == name)
                rule["body"] = [a for a in rule["body"] if a.get("relation") != "php_post_states_closed"]
            rule = next(r for r in pack["rules"] if r["name"] == "php_observed_closed")
            rule["body"] = [{"relation": "replay_requests_closed", "terms": [{"variable": "Run"}]}]
        # the gap rule alone still catches the omission once its closure is derivable
        self.assertEqual(verdict(self._mutated(drop_witness_inputs_but_keep_the_gap_rule)), "unresolved")


@unittest.skipIf(shutil.which("souffle") is None, "souffle is not on PATH; run inside the nix devShell")
class SouffleAgreesWithPythonOnEveryReplayCase(unittest.TestCase):
    def test_kernels_agree_on_closure_and_verdicts(self) -> None:
        pack = load_pack()
        replay_root = tempfile.mkdtemp(prefix="capcov-replay-differential-")
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
