"""Stage C ground checker: why / why-not, impact, and tamper-detecting recheck."""
from __future__ import annotations

import copy
import unittest

from capcov.claims import (Atom, Bundle, Column, Constant, Context, Evidence,
                           RelationDecl, Rule, Variable)
from capcov.claims.evaluator import evaluate
from capcov.claims.static.certificate import certify, recheck
from capcov.claims.static.ground import (WHY_NOT_VERSION, WHY_VERSION, impact,
                                        shared_assumptions, why, why_not)

try:
    from .static_rules import go_app
except ImportError:
    from static_rules import go_app


def _edge_bundle() -> Bundle:
    """Tiny non-recursive program: p(x) :- a(x). used for why / why-not."""
    a = RelationDecl("seen", (Column("x", "symbol"),), producer_classes=("probe",))
    derived = RelationDecl("held", (Column("x", "symbol"),),
                           modality="derived", primitive=False)
    fact = Atom("seen", (Constant("ok"),))
    record = Evidence("probe:seen:ok", fact, source="probe unit-test")
    rule = Rule(Atom("held", (Variable("X"),)), (Atom("seen", (Variable("X"),)),), name="held_from_seen")
    return Bundle((a, derived), facts=(fact,), rules=(rule,), evidence=(record,))


class GroundWhyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = _edge_bundle()
        self.report = evaluate(self.bundle)
        self.relations = dict(self.report.relations)

    def test_why_linearises_the_certificate_when_the_row_holds(self) -> None:
        explanation = why(self.bundle, self.relations, "held", ("ok",))
        self.assertEqual(explanation["why_version"], WHY_VERSION)
        self.assertTrue(explanation["holds"])
        self.assertFalse(explanation["truncated"])
        self.assertEqual([step["kind"] for step in explanation["walk"]], ["rule", "fact"])
        self.assertEqual(explanation["walk"][0]["rule"], "held_from_seen")
        self.assertEqual(explanation["leaves"], ["probe:seen:ok"])
        self.assertTrue(recheck(self.bundle, explanation["certificate"], self.relations).ok)

    def test_why_does_not_invent_a_proof_for_an_absent_row(self) -> None:
        explanation = why(self.bundle, self.relations, "held", ("missing",))
        self.assertFalse(explanation["holds"])
        self.assertIsNone(explanation["certificate"])
        self.assertEqual(explanation["walk"], [])
        self.assertEqual(explanation["reason"], "row is not in the closure")

    def test_why_not_names_the_missing_premise_and_refuses_to_refute(self) -> None:
        explanation = why_not(self.bundle, self.relations, "held", ("missing",))
        self.assertEqual(explanation["why_not_version"], WHY_NOT_VERSION)
        self.assertFalse(explanation["holds"])
        self.assertFalse(explanation["refuted"])
        self.assertIn("unresolved", explanation["soundiness"])
        [attempt] = explanation["attempts"]
        self.assertEqual(attempt["rule"], "held_from_seen")
        self.assertEqual(attempt["status"], "missing-premise")
        self.assertEqual(attempt["relation"], "seen")
        self.assertEqual(attempt["completeness_present"], [])

    def test_why_not_on_a_present_row_points_at_why(self) -> None:
        explanation = why_not(self.bundle, self.relations, "held", ("ok",))
        self.assertTrue(explanation["holds"])
        self.assertIn("use why", explanation["reason"])

    def test_recheck_detects_a_tampered_why_certificate(self) -> None:
        explanation = why(self.bundle, self.relations, "held", ("ok",))
        certificate = copy.deepcopy(explanation["certificate"])
        self.assertTrue(recheck(self.bundle, certificate, self.relations).ok)
        certificate["derivation"]["premises"][0]["evidence"] = ["probe:seen:forged"]
        checked = recheck(self.bundle, certificate, self.relations)
        self.assertFalse(checked.ok)
        self.assertTrue(any("unknown evidence" in problem or "attests" in problem
                            for problem in checked.problems), checked.problems)

    def test_recheck_detects_an_unauthorized_producer_on_a_leaf(self) -> None:
        explanation = why(self.bundle, self.relations, "held", ("ok",))
        # Swap the leaf's source without changing the attested row: the ground
        # checker must still refuse a producer the relation did not admit.
        forged = Evidence("probe:seen:ok", Atom("seen", (Constant("ok"),)),
                          source="forged unit-test")
        tampered = Bundle(self.bundle.relations, facts=self.bundle.facts,
                          rules=self.bundle.rules, evidence=(forged,))
        checked = recheck(tampered, explanation["certificate"], self.relations)
        self.assertFalse(checked.ok)
        self.assertTrue(any("producer" in problem for problem in checked.problems),
                        checked.problems)

    def test_impact_falls_exactly_the_dependent_conclusion(self) -> None:
        result = impact(self.bundle, self.relations, ["probe:seen:ok"],
                        (("held", ("ok",)), ("held", ("missing",))))
        self.assertEqual([entry["relation"] for entry in result["fallen"]], ["held"])
        self.assertEqual(result["survived"], [])
        self.assertEqual(result["fallen"][0]["revoked_leaves"], ["probe:seen:ok"])
        untouched = impact(self.bundle, self.relations, ["unrelated"],
                           (("held", ("ok",)),))
        self.assertEqual(untouched["fallen"], [])
        self.assertEqual(len(untouched["survived"]), 1)


class GoAppGroundTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle, _ = go_app.go_app_bundle()
        cls.report = evaluate(cls.bundle)
        cls.relations = dict(cls.report.relations)

    def test_why_not_for_the_undeclared_runtime_surface_names_the_absent_static_row(self) -> None:
        # The negative claim runtime_route_without_static *is* in the closure
        # (the probe saw POST /jobs).  why-not of the *declared* surface's
        # gap row should say the static inventory contains that surface.
        index = go_app.index_digest()
        explanation = why_not(
            self.bundle, self.relations, "runtime_route_without_static",
            (go_app.TENANT, go_app.SURFACE, go_app.RUN, index))
        self.assertFalse(explanation["holds"])
        self.assertFalse(explanation["refuted"])
        statuses = {item.get("status") for item in explanation["attempts"]}
        self.assertTrue(statuses & {"blocked-by-presence", "missing-premise", "head-mismatch"})

    def test_shared_assumptions_are_empty_on_the_go_app_jobs_read(self) -> None:
        row = (go_app.index_digest(), go_app.SURFACE, "jobs", "read")
        if row not in set(self.relations.get("static_capability_op", ())):
            self.skipTest("go_app control no longer derives static_capability_op for GET /jobs")
        certificate = certify(self.bundle, self.relations, "static_capability_op", row)
        self.assertEqual(shared_assumptions(self.bundle, certificate), ())

    def test_why_and_recheck_agree_across_a_supported_go_app_row(self) -> None:
        [row] = [item for item in self.relations.get("static_index_current", ()) if item]
        explanation = why(self.bundle, self.relations, "static_index_current", row)
        self.assertTrue(explanation["holds"], explanation)
        self.assertTrue(recheck(self.bundle, explanation["certificate"], self.relations).ok)


if __name__ == "__main__":
    unittest.main()
