from __future__ import annotations

import unittest

from capcov.claims.evaluator import ResourceLimits, evaluate
from capcov.claims.ir import (Aggregation, Atom, Claim, Column, Comparison,
                              Constant, Evidence, EvidenceMapping, OutputTemplate,
                              RelationDecl, Rule, TemplateValue, Variable)
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.verdicts import OperationalStatus, SemanticVerdict


def rel(name: str, *columns: tuple[str, str], **kwargs: object) -> RelationDecl:
    return RelationDecl(name, tuple(Column(n, t) for n, t in columns), **kwargs)


def fact(name: str, *values: object) -> Atom:
    return Atom(name, tuple(Constant(v) for v in values))


class PythonEvaluatorTests(unittest.TestCase):
    def test_transitive_closure_and_provenance_are_deterministic(self) -> None:
        bundle = Bundle(
            relations=(rel("edge", ("src", "symbol"), ("dst", "symbol")),
                       rel("reachable", ("src", "symbol"), ("dst", "symbol")),),
            facts=(fact("edge", "a", "b"), fact("edge", "b", "c")),
            rules=(
                Rule(Atom("reachable", (Variable("x"), Variable("y"))),
                     (Atom("edge", (Variable("x"), Variable("y"))),), "edge-to-reachable"),
                Rule(Atom("reachable", (Variable("x"), Variable("z"))),
                     (Atom("reachable", (Variable("x"), Variable("y"))),
                      Atom("edge", (Variable("y"), Variable("z")))), "reachable-step"),
            ),
            claims=(Claim("reachable", (Constant("a"), Constant("c"))),),
        )
        report = evaluate(bundle)
        self.assertEqual(report.relation_rows("reachable"), (("a", "b"), ("a", "c"), ("b", "c")))
        self.assertEqual(report.claims[0].semantic, SemanticVerdict.SUPPORTED)
        self.assertIn("fact:edge", report.claims[0].result.support[0])
        self.assertEqual(report.as_dict(), evaluate(bundle).as_dict())

    def test_independent_alternative_derivations_report_one_canonical_support_path(self) -> None:
        bundle = Bundle(
            relations=(rel("a", ("x", "symbol")), rel("b", ("x", "symbol")), rel("c", ("x", "symbol"))),
            facts=(fact("a", "v"), fact("b", "v")),
            rules=(Rule(Atom("c", (Variable("x"),)), (Atom("a", (Variable("x"),)),), "from-a"),
                   Rule(Atom("c", (Variable("x"),)), (Atom("b", (Variable("x"),)),), "from-b")),
            claims=(Claim("c", (Constant("v"),)),),
        )
        result = evaluate(bundle).claims[0].result
        self.assertEqual(result.support, ("fact:a:[\"v\"]",))

    def test_provenance_cap_is_complete_at_boundary_and_exhausted_one_over(self) -> None:
        bundle = Bundle(
            relations=(rel("a", ("x", "symbol")), rel("b", ("x", "symbol")), rel("c", ("x", "symbol"))),
            facts=(fact("a", "v"), fact("b", "v")),
            rules=(Rule(Atom("c", (Variable("x"),)), (Atom("a", (Variable("x"),)),), "from-a"),
                   Rule(Atom("c", (Variable("x"),)), (Atom("b", (Variable("x"),)),), "from-b")),
            claims=(Claim("c", (Constant("v"),)),),
        )
        # Two fact proofs plus two distinct c proofs exactly fill the cap.
        complete = evaluate(bundle, ResourceLimits(max_provenance=4))
        self.assertEqual(complete.status, OperationalStatus.COMPLETE)
        exhausted = evaluate(bundle, ResourceLimits(max_provenance=3))
        self.assertEqual(exhausted.status, OperationalStatus.RESOURCE_EXHAUSTED)
        self.assertEqual(exhausted.claims[0].operational, OperationalStatus.RESOURCE_EXHAUSTED)

    def test_comparison_and_negation_require_no_false_open_world_success(self) -> None:
        relations = (
            rel("item", ("name", "symbol"), ("n", "integer")),
            rel("complete_item", ("scope", "symbol"), modality="completeness"),
            rel("small", ("name", "symbol")),
        )
        # The completeness witness is in the same rule body as the negation;
        # its presence permits closed-world reasoning for item only there.
        rules = (
            Rule(Atom("small", (Variable("name"),)),
                 (Atom("item", (Variable("name"), Variable("n"))),
                  Comparison(Variable("n"), "<", Constant(10))), "small"),
        )
        # This rule is intentionally invalid: the completeness relation has
        # the wrong arity for the witness scope and must not be evaluated.
        invalid = Bundle(relations=relations, facts=(fact("item", "a", 3),), rules=rules,
                         claims=(Claim("small", (Constant("a"),)),))
        report = evaluate(invalid)
        self.assertEqual(report.status, OperationalStatus.INVALID_INPUT)

    def test_resource_exhaustion_is_a_status(self) -> None:
        bundle = Bundle(
            relations=(rel("edge", ("src", "symbol"), ("dst", "symbol")),
                       rel("reachable", ("src", "symbol"), ("dst", "symbol")),),
            facts=(fact("edge", "a", "b"), fact("edge", "b", "c")),
            rules=(Rule(Atom("reachable", (Variable("x"), Variable("y"))),
                         (Atom("edge", (Variable("x"), Variable("y"))),), "seed"),),
            claims=(Claim("reachable", (Constant("a"), Constant("b"))),),
        )
        report = evaluate(bundle, ResourceLimits(max_derived_rows=1))
        self.assertEqual(report.status, OperationalStatus.RESOURCE_EXHAUSTED)
        self.assertEqual(report.claims[0].operational, OperationalStatus.RESOURCE_EXHAUSTED)

    def test_stratified_negation_uses_explicit_completeness(self) -> None:
        relations = (
            rel("item", ("name", "symbol")),
            rel("blocked", ("name", "symbol")),
            rel("all_items", ("scope", "symbol"), modality="completeness", completes="item"),
            rel("all_blocked", ("scope", "symbol"), modality="completeness", completes="blocked"),
            rel("allowed", ("name", "symbol")),
        )
        rule = Rule(Atom("allowed", (Variable("name"),)),
                    (Atom("item", (Variable("name"),)),
                     Atom("all_items", (Constant("global"),)),
                     Atom("all_blocked", (Constant("global"),)),
                     Atom("blocked", (Variable("name"),), negated=True)), "allowed-if-unblocked")
        bundle = Bundle(relations=relations,
                        facts=(fact("item", "a"), fact("all_items", "global"), fact("all_blocked", "global")),
                        rules=(rule,), claims=(Claim("allowed", (Constant("a"),)),))
        report = evaluate(bundle)
        self.assertEqual(report.claims[0].semantic, SemanticVerdict.SUPPORTED)
        blocked_bundle = Bundle(relations=relations,
                                facts=(*bundle.facts, fact("blocked", "a")),
                                rules=(rule,), claims=bundle.claims)
        self.assertEqual(evaluate(blocked_bundle).claims[0].semantic, SemanticVerdict.UNRESOLVED)

    def test_finite_sum_aggregation_is_grouped_and_deterministic(self) -> None:
        relations = (
            rel("item", ("name", "symbol"), ("n", "integer")),
            rel("group", ("name", "symbol"), finite=True, nonempty=True),
            rel("group_closed", ("name", "symbol"), modality="completeness", completes="group"),
            rel("total", ("name", "symbol"), ("n", "integer")),
        )
        rule = Rule(Atom("total", (Variable("name"), Variable("n"))),
                    (Atom("item", (Variable("name"), Variable("n"))),
                     Atom("group", (Variable("name"),)),
                     Atom("group_closed", (Variable("name"),))),
                    "sum-items", aggregation=Aggregation("n", "item", ("name",), "n", "sum", "group", "group_closed"))
        bundle = Bundle(relations=relations,
                        facts=(fact("item", "a", 2), fact("item", "a", 3), fact("group", "a"), fact("group_closed", "a")),
                        rules=(rule,), claims=(Claim("total", (Constant("a"), Constant(5))),))
        report = evaluate(bundle)
        self.assertEqual(report.relation_rows("total"), (("a", 5),))
        self.assertEqual(report.claims[0].semantic, SemanticVerdict.SUPPORTED)

    def test_forall_is_a_typed_conjunction_over_the_domain(self) -> None:
        bundle = Bundle(
            relations=(rel("domain", ("name", "symbol"), finite=True, nonempty=True),
                       rel("domain_closed", modality="completeness", completes="domain"),
                       rel("item", ("name", "symbol"), ("ok", "boolean"))),
            facts=(fact("domain", "a"), fact("domain", "b"), fact("domain_closed"),
                   fact("item", "a", True), fact("item", "b", False)),
            claims=(Claim("item", (Variable("name"), Constant(True)), quantifier="forall", domain="domain"),),
        )
        result = evaluate(bundle).claims[0]
        # The second typed substitution has no matching positive fact; in an
        # open-world relation that is unresolved, not a refutation.
        self.assertEqual(result.semantic, SemanticVerdict.UNRESOLVED)
        self.assertEqual(result.operational, OperationalStatus.COMPLETE)

    def test_any_and_all_aggregations_have_boolean_results(self) -> None:
        relations = (
            rel("item", ("name", "symbol"), ("ok", "boolean")),
            rel("group", ("name", "symbol"), finite=True, nonempty=True),
            rel("group_closed", ("name", "symbol"), modality="completeness", completes="group"),
            rel("any_ok", ("name", "symbol"), ("ok", "boolean")),
            rel("all_ok", ("name", "symbol"), ("ok", "boolean")),
        )
        any_rule = Rule(Atom("any_ok", (Variable("name"), Variable("ok"))),
                        (Atom("item", (Variable("name"), Variable("ok"))), Atom("group", (Variable("name"),)),
                         Atom("group_closed", (Variable("name"),))), "any-ok",
                        Aggregation("ok", "item", ("name",), "ok", "any", "group", "group_closed"))
        all_rule = Rule(Atom("all_ok", (Variable("name"), Variable("ok"))),
                        (Atom("item", (Variable("name"), Variable("ok"))), Atom("group", (Variable("name"),)),
                         Atom("group_closed", (Variable("name"),))), "all-ok",
                        Aggregation("ok", "item", ("name",), "ok", "all", "group", "group_closed"))
        bundle = Bundle(relations=relations,
                        facts=(fact("item", "a", True), fact("item", "a", False), fact("group", "a"),
                               fact("group", "b"), fact("group_closed", "a"), fact("group_closed", "b")),
                        rules=(any_rule, all_rule), claims=(Claim("any_ok", (Constant("a"), Constant(True))),))
        report = evaluate(bundle)
        self.assertEqual(report.relation_rows("any_ok"), (("a", True), ("b", False)))
        self.assertEqual(report.relation_rows("all_ok"), (("a", False), ("b", False)))
        self.assertEqual(report.claims[0].semantic, SemanticVerdict.SUPPORTED)

    def test_missing_premise_outputs_honor_every_trigger_and_keep_fallback(self) -> None:
        observed = rel("observed", ("x", "symbol"))
        claimed = rel("claimed", ("x", "symbol"), modality="claim")
        match = fact("observed", "v")
        other = fact("observed", "other")
        evidence = (Evidence("match", match, source="test"),
                    Evidence("other", other, source="test"))
        claim = Claim("claimed", (Constant("v"),), id="claim")
        mapping = EvidenceMapping("claimed", "observed", "observation",
                                  bindings=(("x", "x"),), claim_id="claim")
        base = Bundle((observed, claimed), facts=(match, other),
                      evidence=evidence, claims=(claim,), mappings=(mapping,))
        fallback = ('claim:claimed:{}',)

        unrelated = OutputTemplate("observed", "claim", evidence_id="match")
        self.assertEqual(evaluate(Bundle(base.relations, base.facts,
                                         claims=base.claims, evidence=base.evidence,
                                         mappings=base.mappings,
                                         outputs=(unrelated,))).claims[0].result.missing_premises,
                         fallback)

        reason = (("reason", TemplateValue(value="reviewed missing premise")),)
        cases = (
            ({"requires_all_evidence": ("match",)}, True),
            ({"requires_all_evidence": ("other",)}, False),
            ({"requires_any_evidence": ("other", "match")}, True),
            ({"requires_any_evidence": ("other",)}, False),
            ({"excludes_evidence": ("match",)}, False),
            ({"excludes_evidence": ("other",)}, True),
            ({"when_claim": "refuted"}, False),
            ({"when_claim": "unresolved"}, True),
            ({"when_claim": "derived"}, False),
            ({"when_claim": "underived"}, True),
        )
        expected = ({"relation": "observed",
                     "reason": "reviewed missing premise"},)
        for conditions, active in cases:
            output = OutputTemplate("missing_premise", "claim",
                                    relation="observed", fields=reason,
                                    **conditions)
            bundle = Bundle(base.relations, base.facts, claims=base.claims,
                            evidence=base.evidence, mappings=base.mappings,
                            outputs=(output,))
            with self.subTest(conditions=conditions):
                actual = evaluate(bundle).claims[0].result.missing_premises
                self.assertEqual(actual, expected if active else fallback)

    def test_multiple_recursive_body_atoms_each_use_delta_pivots(self) -> None:
        relations = (
            rel("edge", ("src", "symbol"), ("dst", "symbol")),
            rel("paired", ("src", "symbol"), ("left", "symbol"), ("right", "symbol")),
        )
        seed = Rule(Atom("paired", (Variable("x"), Variable("y"), Variable("z"))),
                    (Atom("edge", (Variable("x"), Variable("y"))),
                     Atom("edge", (Variable("x"), Variable("z")))), "seed-pairs")
        # Both body atoms depend on the same recursive relation.  The result
        # requires using each newly-added tuple as a possible pivot.
        step = Rule(Atom("paired", (Variable("x"), Variable("y"), Variable("w"))),
                    (Atom("paired", (Variable("x"), Variable("y"), Variable("z"))),
                     Atom("paired", (Variable("x"), Variable("z"), Variable("w")))), "extend-pairs")
        bundle = Bundle(relations=relations,
                        facts=(fact("edge", "a", "b"), fact("edge", "a", "c"), fact("edge", "a", "e")),
                        rules=(seed, step), claims=(Claim("paired", (Constant("a"), Constant("b"), Constant("e"))),))
        report = evaluate(bundle)
        self.assertIn(("a", "b", "e"), report.relation_rows("paired"))
        self.assertLessEqual(dict(report.resources)["provenance_nodes"], 200)


if __name__ == "__main__":
    unittest.main()
