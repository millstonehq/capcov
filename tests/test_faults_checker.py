"""The fault checker: one run becomes a claims bundle, and the claim kernels judge it."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.claims.ir import Atom, Claim, Column, Constant, RelationDecl, Rule, Variable
from capcov.claims.validation import validate_bundle
from capcov.faults import checker
from capcov.faults.checker import ExtraFacts, FinalRead, Finals, Rows, ServiceFacts
from capcov.faults.cli import main as faults_main
from capcov.faults.history import History, Result
from capcov.faults.service import (ApplyTwice, DelayApply, DelayChangeEntry, EditBeforeNextWrite,
                                   FakeService, ResponseLost, classify)

OP, INTENT, KEY = "publish:note-1", "note-1/visibility", "visibility/note-1"
INVARIANTS = ("effect_applied_twice", "orphan_change", "lost_acknowledgement", "silent_overwrite",
              "unsettled_effect", "split_outcome")


def closed(rows: tuple = ()) -> Rows:
    """All the rows of one consumer relation."""
    return Rows(rows, complete=True)


def whole(store: str, rows: dict) -> FinalRead:
    """A final read that covered the whole store."""
    return FinalRead(store, rows, complete=True)


class Run:
    """A history, a fake index service and the consumer's bookkeeping for one intent."""

    def __init__(self) -> None:
        self.history = History(classify=classify)
        self.service = FakeService(self.history.clock, name="index")
        self.intent: Rows | None = closed(((INTENT, OP, "index", KEY, "public"),))
        self.recorded: Rows | None = closed(((INTENT, "applied"),))
        self.settled: Rows | None = closed(((INTENT,),))
        self.reported: Rows | None = closed(())
        self.reads: tuple[FinalRead, ...] = (whole("objects", {}),)

    def publish(self, *, writes: int = 1, read: bool = True) -> "Run":
        with self.history.operation(OP, "actor-1", "publish", "note-1"):
            if read:
                self.service.read(KEY, "actor-1")
            for _ in range(writes):
                self.service.write(KEY, "public", "actor-1")
        return self

    def finals(self) -> Finals:
        return Finals(reads=self.reads, intent=self.intent, recorded=self.recorded,
                      settled=self.settled, overwrite_reported=self.reported)

    def bundle(self, *, close: bool = True, service: object = "own", extra: tuple = ()):
        if close and not self.history.complete_flag:
            self.history.close()
        return checker.build_bundle(self.history, self.service if service == "own" else service,
                                    self.finals(), extra)

    def verdicts(self, **options) -> checker.Verdicts:
        return checker.check(self.bundle(**options))


def statuses(verdicts: checker.Verdicts) -> dict[str, str]:
    return {verdict.name: verdict.status for verdict in verdicts.verdicts}


def all_supported_except(**other: str) -> dict[str, str]:
    return {name: other.get(name, "supported") for name in INVARIANTS}


class InvariantTest(unittest.TestCase):
    def test_a_clean_run_supports_every_invariant(self) -> None:
        verdicts = Run().publish().verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except())
        self.assertTrue(verdicts.ok)
        self.assertEqual(verdicts.failed, ())
        self.assertEqual({(v.semantic, v.operational) for v in verdicts.verdicts},
                         {("supported", "complete")})

    def test_two_requests_applying_one_intent_is_effect_applied_twice(self) -> None:
        verdicts = Run().publish(writes=2).verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(effect_applied_twice="violated"))
        self.assertEqual(verdicts["effect_applied_twice"].violations, (("run-1", INTENT),))
        self.assertEqual(verdicts["effect_applied_twice"].semantic, "refuted")
        self.assertFalse(verdicts.ok)

    def test_one_request_the_service_applied_twice_is_not_the_callers_violation(self) -> None:
        run = Run()
        run.service.inject(ApplyTwice())
        self.assertEqual(statuses(run.publish().verdicts()), all_supported_except())

    def test_a_change_with_no_recorded_intent_is_an_orphan(self) -> None:
        run = Run().publish()
        run.service.write("visibility/note-2", "public", "actor-1")
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(orphan_change="violated"))
        self.assertEqual(verdicts["orphan_change"].violations, (("run-1", "index", 2),))

    def test_an_outside_editors_change_is_not_an_orphan(self) -> None:
        run = Run().publish()
        run.service.inject(EditBeforeNextWrite("visibility/note-9", "private"))
        run.service.read("visibility/note-9", "actor-1")
        run.intent = closed((*run.intent.rows, ("note-9/visibility", "publish:note-9", "index",
                                              "visibility/note-9", "public")))
        run.recorded = closed((*run.recorded.rows, ("note-9/visibility", "applied")))
        run.settled = closed((*run.settled.rows, ("note-9/visibility",)))
        run.service.write("visibility/note-9", "public", "actor-1")
        self.assertEqual(statuses(run.verdicts())["orphan_change"], "supported")

    def test_an_acknowledged_operation_whose_effect_is_absent_is_a_lost_acknowledgement(self) -> None:
        run = Run().publish(writes=0)
        run.recorded = closed(())
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(lost_acknowledgement="violated"))
        self.assertEqual(verdicts["lost_acknowledgement"].violations, (("run-1", OP, INTENT),))

    def test_an_unknown_outcome_is_not_an_acknowledgement(self) -> None:
        run = Run()
        with self.assertRaises(TimeoutError):
            with run.history.operation(OP, "actor-1", "publish", "note-1"):
                raise TimeoutError("timed out")
        run.recorded = closed(())
        self.assertEqual(statuses(run.verdicts())["lost_acknowledgement"], "supported")

    def test_an_unreported_overwrite_of_an_unread_value_is_silent(self) -> None:
        run = Run()
        run.service.inject(EditBeforeNextWrite(KEY, "private"))
        verdicts = run.publish().verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(silent_overwrite="violated"))
        self.assertEqual(verdicts["silent_overwrite"].violations, (("run-1", "index", 2),))

    def test_a_reported_overwrite_is_not_silent(self) -> None:
        run = Run()
        run.service.inject(EditBeforeNextWrite(KEY, "private"))
        run.publish()
        run.reported = closed((("index", 2),))
        self.assertEqual(statuses(run.verdicts()), all_supported_except())

    def test_an_intent_nobody_settled_is_an_unsettled_effect(self) -> None:
        run = Run().publish()
        run.settled = closed(())
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(unsettled_effect="violated"))
        self.assertEqual(verdicts["unsettled_effect"].violations, (("run-1", INTENT),))

    def test_recorded_applied_but_absent_is_a_split_outcome(self) -> None:
        run = Run()
        with self.assertRaises(TimeoutError):
            with run.history.operation(OP, "actor-1", "publish", "note-1"):
                raise TimeoutError("timed out")
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(split_outcome="violated"))
        self.assertEqual(verdicts["split_outcome"].violations, (("run-1", INTENT),))

    def test_recorded_absent_but_present_is_a_split_outcome(self) -> None:
        run = Run().publish()
        run.recorded = closed(((INTENT, "absent"),))
        self.assertEqual(statuses(run.verdicts()), all_supported_except(split_outcome="violated"))

    def test_an_intent_in_another_store_is_judged_against_that_stores_final_read(self) -> None:
        run = Run().publish()
        snapshot = ("note-1/snapshot", OP, "objects", "snapshots/note-1", "first draft")
        run.intent = closed((*run.intent.rows, snapshot))
        run.recorded = closed((*run.recorded.rows, ("note-1/snapshot", "applied")))
        run.settled = closed((*run.settled.rows, ("note-1/snapshot",)))
        self.assertEqual(statuses(run.verdicts()),
                         all_supported_except(lost_acknowledgement="violated", split_outcome="violated"))
        run.reads = (whole("objects", {"snapshots/note-1": "first draft"}),)
        self.assertEqual(statuses(run.verdicts()), all_supported_except())


class CompletenessTest(unittest.TestCase):
    def test_an_incomplete_change_read_leaves_orphan_change_unresolved(self) -> None:
        run = Run()
        run.service.inject(DelayChangeEntry(50))
        run.publish()
        self.assertFalse(run.service.changes().complete)
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(orphan_change="unresolved"))
        orphan = verdicts["orphan_change"]
        self.assertEqual((orphan.semantic, orphan.operational), ("unresolved", "complete"))
        self.assertEqual(orphan.missing_premises, ({"relation": "changes_closed"},))
        self.assertFalse(verdicts.ok)
        run.service.heal()
        self.assertEqual(statuses(run.verdicts()), all_supported_except())

    def test_an_orphan_visible_in_an_incomplete_read_is_still_a_violation(self) -> None:
        run = Run().publish()
        run.service.write("visibility/note-2", "public", "actor-1")
        run.service.inject(DelayChangeEntry(50))
        run.service.write("visibility/note-3", "public", "actor-1")
        self.assertEqual(statuses(run.verdicts())["orphan_change"], "violated")

    def test_an_open_history_leaves_the_invariants_over_operations_unresolved(self) -> None:
        verdicts = Run().publish().verdicts(close=False)
        self.assertEqual(statuses(verdicts), all_supported_except(
            orphan_change="unresolved", lost_acknowledgement="unresolved"))
        self.assertEqual(verdicts["lost_acknowledgement"].missing_premises, ({"relation": "ops_closed"},))

    def test_an_incomplete_final_read_names_the_store(self) -> None:
        run = Run().publish()
        run.reads = (FinalRead("objects", {}, complete=False),)
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(
            lost_acknowledgement="unresolved", split_outcome="unresolved"))
        self.assertEqual(verdicts["split_outcome"].missing_premises,
                         ({"relation": "final_closed", "store": "objects"},))

    def test_an_intent_in_a_store_with_no_final_read_names_the_store(self) -> None:
        run = Run().publish()
        run.reads = ()
        run.intent = closed((*run.intent.rows, ("note-1/snapshot", OP, "objects", "snapshots/note-1", "x")))
        self.assertEqual(run.verdicts()["split_outcome"].missing_premises,
                         ({"relation": "final_closed", "store": "objects"},))

    def test_each_consumer_relation_that_was_not_read_is_named(self) -> None:
        expected = {"intent": "intents_closed", "recorded": "recorded_closed",
                    "settled": "settled_closed", "reported": "reports_closed"}
        for attribute, relation in expected.items():
            with self.subTest(attribute):
                run = Run().publish()
                setattr(run, attribute, None)
                unresolved = [v for v in run.verdicts().verdicts if v.status == "unresolved"]
                self.assertTrue(unresolved)
                for verdict in unresolved:
                    self.assertIn({"relation": relation}, verdict.missing_premises)

    def test_an_incomplete_consumer_relation_emits_its_rows_and_no_closure(self) -> None:
        run = Run().publish()
        run.settled = Rows(((INTENT,),), complete=False)
        bundle = run.bundle()
        relations = {fact.relation for fact in bundle.facts}
        self.assertIn("settled", relations)
        self.assertNotIn("settled_closed", relations)
        self.assertEqual(statuses(checker.check(bundle))["unsettled_effect"], "unresolved")

    def test_no_service_leaves_the_service_invariants_unresolved(self) -> None:
        run = Run()
        run.intent, run.recorded, run.settled = closed(()), closed(()), closed(())
        verdicts = run.verdicts(service=None)
        self.assertEqual(statuses(verdicts), all_supported_except(
            effect_applied_twice="unresolved", orphan_change="unresolved",
            lost_acknowledgement="unresolved", silent_overwrite="unresolved",
            split_outcome="unresolved"))
        self.assertIn({"relation": "applied_closed"}, verdicts["silent_overwrite"].missing_premises)


def pending_run() -> Run:
    """One write accepted and not applied; the program recorded it absent and settled it."""
    run = Run()
    run.service.inject(DelayApply(50))
    token = run.history.invoke(OP, "actor-1", "publish", "note-1")
    try:
        run.service.write(KEY, "public", "actor-1")
    except ResponseLost:
        run.history.complete(token, Result.INFO, error="ResponseLost")
    run.recorded = closed(((INTENT, "absent"),))
    return run


class PendingWriteTest(unittest.TestCase):
    def test_absence_is_not_concluded_while_a_write_is_pending(self) -> None:
        verdicts = pending_run().verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(
            effect_applied_twice="unresolved", orphan_change="unresolved",
            lost_acknowledgement="unresolved", silent_overwrite="unresolved",
            split_outcome="unresolved"))
        for name in ("effect_applied_twice", "lost_acknowledgement", "silent_overwrite",
                     "split_outcome"):
            self.assertIn({"relation": "applied_closed"}, verdicts[name].missing_premises, name)
        self.assertEqual(verdicts["orphan_change"].missing_premises, ({"relation": "changes_closed"},))
        self.assertIn({"relation": "final_closed", "store": "index"},
                      verdicts["split_outcome"].missing_premises)
        self.assertFalse(verdicts.ok)

    def test_the_same_run_is_a_split_outcome_once_the_write_lands(self) -> None:
        run = pending_run()
        run.service.heal()
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(split_outcome="violated"))
        self.assertEqual(verdicts["split_outcome"].violations, (("run-1", INTENT),))

    def test_service_facts_carry_whether_a_write_is_pending(self) -> None:
        run = pending_run()
        self.assertFalse(ServiceFacts.from_service(run.service).applied_complete)
        run.service.heal()
        self.assertTrue(ServiceFacts.from_service(run.service).applied_complete)
        with self.assertRaises(TypeError):
            ServiceFacts("index", run.service.changes(), (), (), {})  # type: ignore[call-arg]

    def test_a_late_write_over_a_value_its_sender_read_is_a_silent_overwrite(self) -> None:
        run = pending_run()
        run.recorded = closed(((INTENT, "applied"),))
        run.service.write(KEY, "private", "actor-2")
        run.service.read(KEY, "actor-1")
        run.service.heal()
        verdicts = run.verdicts()
        self.assertEqual(verdicts["silent_overwrite"].status, "violated")
        self.assertEqual(verdicts["silent_overwrite"].violations, (("run-1", "index", 2),))


class CompletenessDefaultTest(unittest.TestCase):
    def test_no_completeness_flag_defaults_to_true(self) -> None:
        self.assertFalse(Rows(()).complete)
        self.assertFalse(Rows().complete)
        self.assertFalse(FinalRead("objects", {}).complete)

    def test_rows_passed_without_the_flag_leave_their_invariants_unresolved(self) -> None:
        run = Run().publish()
        run.settled = Rows(((INTENT,),))
        run.reads = (FinalRead("objects", {}),)
        verdicts = run.verdicts()
        self.assertEqual(statuses(verdicts), all_supported_except(
            unsettled_effect="unresolved", lost_acknowledgement="unresolved",
            split_outcome="unresolved"))
        self.assertEqual(verdicts["unsettled_effect"].missing_premises, ({"relation": "settled_closed"},))
        self.assertIn({"relation": "final_closed", "store": "objects"},
                      verdicts["split_outcome"].missing_premises)


class BundleShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = Run().publish().bundle()
        self.relations = {relation.name: relation for relation in self.bundle.relations}

    def test_the_bundle_validates(self) -> None:
        self.assertEqual(validate_bundle(self.bundle), ())

    def test_every_fact_has_evidence_from_its_producer_class(self) -> None:
        evidence = {record.atom: record for record in self.bundle.evidence}
        self.assertEqual(set(evidence), set(self.bundle.facts))
        for atom, record in evidence.items():
            declared = self.relations[atom.relation].producer_classes
            self.assertEqual(len(declared), 1, atom.relation)
            self.assertEqual(record.source.split(" ", 1)[0], declared[0], atom.relation)
            self.assertEqual(record.context.as_dict(), {"run": "run-1"})

    def test_primitive_relations_carry_the_run_context_and_a_producer(self) -> None:
        producers = {"op_invoked": "history", "op_completed": "history", "effect_sent": "history",
                     "change": "service-log", "applied": "ground-truth",
                     "overwrote_unread": "ground-truth", "final_value": "final-read",
                     "intent": "consumer", "recorded": "consumer", "settled": "consumer",
                     "overwrite_reported": "consumer"}
        for name, producer in producers.items():
            relation = self.relations[name]
            self.assertTrue(relation.primitive, name)
            self.assertEqual(relation.producer_classes, (producer,), name)
            self.assertEqual(relation.context_indices, ("run",), name)
            self.assertEqual((relation.columns[0].name, relation.columns[0].context), ("run", True))

    def test_completeness_relations_name_what_they_complete(self) -> None:
        completes = {"ops_closed": "op_invoked", "changes_closed": "change",
                     "applied_closed": "applied", "final_closed": "final_value",
                     "intents_closed": "intent", "recorded_closed": "recorded",
                     "settled_closed": "settled", "reports_closed": "overwrite_reported"}
        for name, target in completes.items():
            relation = self.relations[name]
            self.assertEqual((relation.modality.value, relation.completes), ("completeness", target))
        self.assertEqual([column.name for column in self.relations["final_closed"].columns],
                         ["run", "store"])

    def test_each_negated_atom_has_its_completeness_atom_in_the_same_body(self) -> None:
        negated = 0
        for rule in self.bundle.rules:
            for atom in (item for item in rule.body if isinstance(item, Atom) and item.negated):
                negated += 1
                witnesses = [item.relation for item in rule.body
                             if isinstance(item, Atom) and not item.negated
                             and self.relations[item.relation].completes == atom.relation]
                self.assertTrue(witnesses, rule.name)
        self.assertGreaterEqual(negated, 6)

    def test_there_is_one_claim_per_invariant(self) -> None:
        self.assertEqual(sorted(checker.invariant_names()), sorted(INVARIANTS))
        self.assertEqual(sorted(claim.terms[1].value for claim in self.bundle.claims), sorted(INVARIANTS))
        self.assertEqual({claim.relation for claim in self.bundle.claims}, {"holds"})
        self.assertEqual(self.relations["holds"].modality.value, "claim")

    def test_effects_recorded_at_boundaries_become_effect_sent_rows(self) -> None:
        from capcov.faults.crash import Boundaries

        run = Run()
        boundaries = Boundaries(run.history)
        boundaries.effect("write", lambda: run.service.write(KEY, "public", "actor-1"),
                          "index", "actor-1", OP)
        rows = [tuple(term.value for term in fact.terms) for fact in run.bundle().facts
                if fact.relation == "effect_sent"]
        self.assertEqual(rows, [("run-1", OP, 1, "index", "write", "actor-1", "ok")])
        self.assertEqual(statuses(checker.check(run.bundle()))["orphan_change"], "supported")


class ExtraTest(unittest.TestCase):
    def extra(self) -> tuple:
        run, key = Variable("Run"), Variable("Key")
        symbol = lambda value: Constant(value, "symbol")  # noqa: E731
        return (
            RelationDecl("draft_left", (Column("run", "symbol", True), Column("key", "symbol")),
                         modality="derived", primitive=False, context_indices=("run",)),
            Rule(Atom("draft_left", (run, key)),
                 (Atom("final_value", (run, symbol("primary"), key, Variable("Value"))),), "draft_left"),
            Rule(Atom("violated", (run, symbol("draft_left"))), (Atom("draft_left", (run, key)),),
                 "violated_draft_left"),
            Rule(Atom("violated_closed", (run, symbol("draft_left"))), (Atom("finals_closed", (run,)),),
                 "violated_closed_draft_left"),
            checker.invariant_claim("run-1", "draft_left"),
        )

    def test_a_consumer_adds_its_own_relation_rules_and_claim(self) -> None:
        run = Run().publish()
        verdicts = run.verdicts(extra=self.extra())
        self.assertEqual(verdicts["draft_left"].status, "supported")
        run.reads = (*run.reads, whole("primary", {"note-1": "draft"}))
        verdicts = run.verdicts(extra=self.extra())
        self.assertEqual((verdicts["draft_left"].status, verdicts["draft_left"].violations),
                         ("violated", (("run-1", "note-1"),)))
        self.assertEqual(len(verdicts.verdicts), len(INVARIANTS) + 1)

    def test_other_values_are_refused(self) -> None:
        with self.assertRaises(checker.FaultCheckError):
            Run().publish().bundle(extra=("not an IR value",))


class ExtraFactsTest(unittest.TestCase):
    """A consumer supplies rows for the relations it declares in ``extra``."""

    def extra(self, *facts: ExtraFacts) -> tuple:
        run, key = Variable("Run"), Variable("Key")
        symbol = lambda value: Constant(value, "symbol")  # noqa: E731
        context = {"producer_classes": ("consumer",), "context_indices": ("run",)}
        return (
            RelationDecl("checked", (Column("run", "symbol", True), Column("key", "symbol")),
                         **context),
            RelationDecl("check_pass", (Column("run", "symbol", True), Column("number", "unsigned")),
                         **context),
            RelationDecl("checks_closed", (Column("run", "symbol", True),), modality="completeness",
                         completes="checked", **context),
            RelationDecl("unchecked", (Column("run", "symbol", True), Column("key", "symbol")),
                         modality="derived", primitive=False, context_indices=("run",)),
            Rule(Atom("unchecked", (run, key)),
                 (Atom("intent", (run, Variable("Intent"), Variable("Op"), Variable("Store"), key,
                                  Variable("Value"))),
                  Atom("checks_closed", (run,)),
                  Atom("checked", (run, key), negated=True)), "unchecked"),
            Rule(Atom("violated", (run, symbol("unchecked"))), (Atom("unchecked", (run, key)),),
                 "violated_unchecked"),
            Rule(Atom("violated_closed", (run, symbol("unchecked"))),
                 (Atom("intents_closed", (run,)), Atom("checks_closed", (run,))),
                 "violated_closed_unchecked"),
            checker.invariant_claim("run-1", "unchecked"),
            *facts,
        )

    def verdict(self, *facts: ExtraFacts) -> checker.Verdict:
        return Run().publish().verdicts(extra=self.extra(*facts))["unchecked"]

    def test_supplied_rows_are_facts_of_the_declared_relation(self) -> None:
        bundle = Run().publish().bundle(extra=self.extra(
            ExtraFacts("checked", [(KEY,)]), ExtraFacts("check_pass", [(7,)]),
            ExtraFacts("checks_closed", [()])))
        self.assertEqual(validate_bundle(bundle), ())
        rows = {fact.relation: tuple(term.value for term in fact.terms) for fact in bundle.facts
                if fact.relation in {"checked", "check_pass", "checks_closed"}}
        self.assertEqual(rows, {"checked": ("run-1", KEY), "check_pass": ("run-1", 7),
                                "checks_closed": ("run-1",)})
        sources = {record.source for record in bundle.evidence if record.atom.relation == "checked"}
        self.assertEqual(sources, {"consumer checked"})

    def test_the_consumer_invariant_is_judged_from_them(self) -> None:
        closed_fact = ExtraFacts("checks_closed", [()])
        self.assertEqual(self.verdict(ExtraFacts("checked", [(KEY,)]), closed_fact).status,
                         "supported")
        violated = self.verdict(ExtraFacts("checked", [("visibility/note-2",)]), closed_fact)
        self.assertEqual((violated.status, violated.violations), ("violated", (("run-1", KEY),)))

    def test_without_the_consumers_completeness_fact_the_invariant_is_unresolved(self) -> None:
        self.assertEqual(self.verdict(ExtraFacts("checked", [(KEY,)])).status, "unresolved")
        self.assertEqual(self.verdict().status, "unresolved")

    def test_rows_are_validated_like_the_built_in_facts(self) -> None:
        refused = {
            "arity": ExtraFacts("checked", [(KEY, 1)]),
            "symbol column": ExtraFacts("checked", [(7,)]),
            "unsigned column": ExtraFacts("check_pass", [("first",)]),
            "negative unsigned": ExtraFacts("check_pass", [(-1,)]),
            "boolean as unsigned": ExtraFacts("check_pass", [(True,)]),
            "undeclared relation": ExtraFacts("archived", [(KEY,)]),
            "derived relation": ExtraFacts("unchecked", [(KEY,)]),
            "pack relation": ExtraFacts("applied_closed", [()]),
            "pack consumer relation": ExtraFacts("settled", [(INTENT,)]),
        }
        for name, facts in refused.items():
            with self.subTest(name), self.assertRaises(checker.FaultCheckError):
                Run().publish().bundle(extra=self.extra(facts))

    def test_a_pending_write_cannot_be_closed_through_extra(self) -> None:
        with self.assertRaises(checker.FaultCheckError):
            pending_run().bundle(extra=(ExtraFacts("applied_closed", [()]),))

    def test_a_pack_relation_cannot_be_redeclared(self) -> None:
        redeclared = RelationDecl("applied_closed", (Column("run", "symbol", True),),
                                  modality="completeness", completes="applied",
                                  producer_classes=("consumer",), context_indices=("run",))
        with self.assertRaises(checker.FaultCheckError):
            Run().publish().bundle(extra=(redeclared, ExtraFacts("applied_closed", [()])))

    def test_a_relation_without_the_run_context_or_the_consumer_producer_is_refused(self) -> None:
        no_run = RelationDecl("noted", (Column("key", "symbol"),))
        other = RelationDecl("probed", (Column("run", "symbol", True),), producer_classes=("probe",),
                             context_indices=("run",))
        for declaration in (no_run, other):
            with self.subTest(declaration.name), self.assertRaises(checker.FaultCheckError):
                Run().publish().bundle(extra=(declaration, ExtraFacts(declaration.name, [()])))


class EngineTest(unittest.TestCase):
    def bundles(self) -> dict[str, object]:
        overwritten = Run()
        overwritten.service.inject(EditBeforeNextWrite(KEY, "private"))
        withheld = Run()
        withheld.service.inject(DelayChangeEntry(50))
        unsettled = Run().publish()
        unsettled.settled = closed(())
        absent = Run().publish()
        absent.recorded = closed(((INTENT, "absent"),))
        lost = Run().publish(writes=0)
        orphan = Run().publish()
        orphan.service.write("visibility/note-2", "public", "actor-1")
        return {"clean": Run().publish().bundle(), "twice": Run().publish(writes=2).bundle(),
                "overwritten": overwritten.publish().bundle(), "withheld": withheld.publish().bundle(),
                "unsettled": unsettled.bundle(), "absent": absent.bundle(), "lost": lost.bundle(),
                "orphan": orphan.bundle(), "open": Run().publish().bundle(close=False)}

    def test_an_unknown_engine_is_refused(self) -> None:
        with self.assertRaises(checker.FaultCheckError):
            checker.check(Run().publish().bundle(), "prolog")

    def test_both_raises_a_named_error_when_souffle_is_absent(self) -> None:
        with patch("capcov.faults.checker.shutil.which", return_value=None):
            with self.assertRaises(checker.SouffleRequired):
                checker.check(Run().publish().bundle(), "both")

    @unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
    def test_python_and_souffle_agree_on_the_pack(self) -> None:
        for name, bundle in self.bundles().items():
            with self.subTest(name):
                python = checker.check(bundle, "python")
                both = checker.check(bundle, "both")
                self.assertEqual(both.engine, "both")
                self.assertEqual(both.verdicts, python.verdicts)

    @unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
    def test_souffle_reports_the_same_closure_and_claims(self) -> None:
        from capcov.claims.differential import compare

        with tempfile.TemporaryDirectory() as scratch:
            for name, bundle in self.bundles().items():
                with self.subTest(name):
                    result = compare(bundle, replay_root=scratch)
                    self.assertTrue(result.matched)
                    self.assertEqual(result.souffle.claims, result.python.claims)
                    self.assertEqual(result.souffle.relations, result.python.relations)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, run: Run, close: bool = True) -> list[str]:
        if close:
            run.history.close()
        (self.root / "h.json").write_text(run.history.to_json(), encoding="utf-8")
        (self.root / "f.json").write_text(checker.finals_to_json(run.finals(), run.service),
                                          encoding="utf-8")
        return ["check", "--history", str(self.root / "h.json"), "--finals", str(self.root / "f.json")]

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = faults_main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_exit_0_when_every_invariant_is_supported(self) -> None:
        status, out, _ = self.run_cli(self.write(Run().publish()))
        self.assertEqual(status, 0)
        self.assertEqual(sorted(line.split()[1] for line in out.splitlines()), sorted(INVARIANTS))
        self.assertEqual({line.split()[0] for line in out.splitlines()}, {"supported"})

    def test_exit_1_names_the_violated_invariant(self) -> None:
        status, out, _ = self.run_cli(self.write(Run().publish(writes=2)))
        self.assertEqual(status, 1)
        self.assertRegex(out, r"(?m)^violated +effect_applied_twice ")

    def test_exit_1_names_the_missing_premise_of_an_unresolved_invariant(self) -> None:
        status, out, _ = self.run_cli(self.write(Run().publish(), close=False))
        self.assertEqual(status, 1)
        self.assertRegex(out, r'(?m)^unresolved +orphan_change +\[\{"relation": "ops_closed"\}\]$')

    def test_json_output_is_the_verdict_document(self) -> None:
        status, out, _ = self.run_cli([*self.write(Run().publish(writes=2)), "--json"])
        document = json.loads(out)
        self.assertEqual((status, document["ok"], document["run"]), (1, False, "run-1"))
        self.assertEqual({item["name"]: item["status"] for item in document["invariants"]},
                         all_supported_except(effect_applied_twice="violated"))

    def test_the_documents_on_disk_judge_as_the_live_run_does(self) -> None:
        run = Run()
        run.service.inject(EditBeforeNextWrite(KEY, "private"))
        run.publish()
        self.write(run)
        history = History.from_json((self.root / "h.json").read_text(encoding="utf-8"))
        finals, service = checker.finals_from_json((self.root / "f.json").read_text(encoding="utf-8"))
        self.assertEqual(checker.check(checker.build_bundle(history, service, finals)).verdicts,
                         run.verdicts().verdicts)
        self.assertNotIn("private", (self.root / "f.json").read_text(encoding="utf-8"))

    def test_exit_2_on_invalid_input(self) -> None:
        argv = self.write(Run().publish())
        missing = [*argv[:2], str(self.root / "absent.json"), *argv[3:]]
        self.assertEqual(self.run_cli(missing)[0], 2)
        (self.root / "f.json").write_text('{"schema_version": 7}', encoding="utf-8")
        status, _, err = self.run_cli(argv)
        self.assertEqual(status, 2)
        self.assertIn("invalid finals document", err)
        (self.root / "h.json").write_text("not a history\n", encoding="utf-8")
        self.assertEqual(self.run_cli(argv)[0], 2)

    def test_a_pending_write_survives_the_round_trip_and_exits_1(self) -> None:
        status, out, _ = self.run_cli(self.write(pending_run()))
        self.assertEqual(status, 1)
        self.assertRegex(out, r'(?m)^unresolved +silent_overwrite +\[\{"relation": "applied_closed"\}\]$')
        document = json.loads((self.root / "f.json").read_text(encoding="utf-8"))
        self.assertIs(document["service"]["applied_complete"], False)

    def edited(self, edit) -> tuple[int, str]:
        argv = self.write(Run().publish())
        document = json.loads((self.root / "f.json").read_text(encoding="utf-8"))
        edit(document)
        (self.root / "f.json").write_text(json.dumps(document), encoding="utf-8")
        status, _, err = self.run_cli(argv)
        return status, err

    def test_exit_2_on_a_finals_document_whose_service_facts_are_malformed(self) -> None:
        def drop_flag(document): del document["service"]["applied_complete"]
        def flag_as_text(document): document["service"]["applied_complete"] = "yes"
        def bad_origin(document): document["service"]["applied"][0][4] = "bogus-origin"
        def short_row(document): document["service"]["applied"][0].pop()
        def number_digest(document): document["service"]["final"][KEY] = 3
        def text_seq(document): document["service"]["applied"][0][0] = "one"
        for edit in (drop_flag, flag_as_text, bad_origin, short_row, number_digest, text_seq):
            with self.subTest(edit.__name__):
                status, err = self.edited(edit)
                self.assertEqual(status, 2)
                self.assertIn("capcov faults:", err)
                self.assertNotIn("Traceback", err)

    def test_an_untouched_document_still_exits_0(self) -> None:
        self.assertEqual(self.edited(lambda document: None)[0], 0)

    def test_exit_2_on_an_invalid_rule_pack(self) -> None:
        argv = self.write(Run().publish())
        (self.root / "pack.json").write_text('{"schema_version": 1}', encoding="utf-8")
        self.assertEqual(self.run_cli([*argv, "--rules", str(self.root / "pack.json")])[0], 2)

    def test_a_rule_pack_file_replaces_the_shipped_one(self) -> None:
        pack = checker.load_pack()
        pack["invariants"] = [item for item in pack["invariants"] if item["name"] == "orphan_change"]
        (self.root / "pack.json").write_text(json.dumps(pack), encoding="utf-8")
        status, out, _ = self.run_cli([*self.write(Run().publish(writes=2)),
                                       "--rules", str(self.root / "pack.json")])
        self.assertEqual((status, out.split()), (0, ["supported", "orphan_change"]))

    def test_exit_2_when_both_engines_are_asked_for_without_souffle(self) -> None:
        argv = [*self.write(Run().publish()), "--engine", "both"]
        with patch("capcov.faults.checker.shutil.which", return_value=None):
            status, _, err = self.run_cli(argv)
        self.assertEqual(status, 2)
        self.assertIn("souffle", err)

    def test_the_top_level_command_dispatches_to_faults(self) -> None:
        from capcov.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main(["faults", *self.write(Run().publish())])
        self.assertEqual(status, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
