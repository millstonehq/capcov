import shutil
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capcov.claims import (Atom, Claim, Column, Constant, Context,
                           DiagnosticRule, Evidence, EvidenceMapping, Modality,
                           OutputTemplate, RelationDecl, Rule, TemplateValue,
                           TypeName, Variable, Aggregation, validate_bundle)
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.evaluator import evaluate as evaluate_python
from capcov.claims.souffle import (MAX_OUTPUT_BYTES, SouffleUnavailable,
                                   run_bundle, translate_bundle)


def R(name, *columns, **kwargs):
    return RelationDecl(name, tuple(Column(*column) for column in columns), **kwargs)


class SouffleBoundaryTests(unittest.TestCase):
    def test_missing_executable_fails_with_named_error(self):
        relation = R("items", ("item", TypeName.SYMBOL))
        with patch("capcov.claims.souffle.shutil.which", return_value=None):
            with self.assertRaises(SouffleUnavailable):
                run_bundle(Bundle((relation,)))

    def test_startup_oserror_fails_with_named_error(self):
        relation = R("items", ("item", TypeName.SYMBOL))
        with patch("capcov.claims.souffle.shutil.which", return_value="/nix/store/fake/bin/souffle"), \
             patch("capcov.claims.souffle.subprocess.Popen", side_effect=PermissionError("denied")):
            with self.assertRaises(SouffleUnavailable):
                run_bundle(Bundle((relation,)))


@unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
class SouffleBackendTests(unittest.TestCase):
    def test_recursive_transitive_closure_and_digests_are_deterministic(self):
        edge = R("edge", ("left", TypeName.SYMBOL), ("right", TypeName.SYMBOL))
        path = R("path", ("left", TypeName.SYMBOL), ("right", TypeName.SYMBOL))
        rules = (
            Rule(Atom("path", (Variable("x"), Variable("y"))),
                 (Atom("edge", (Variable("x"), Variable("y"))),)),
            Rule(Atom("path", (Variable("x"), Variable("z"))),
                 (Atom("path", (Variable("x"), Variable("y"))),
                  Atom("edge", (Variable("y"), Variable("z"))))),
        )
        bundle = Bundle((edge, path), facts=(
            Atom("edge", (Constant("a"), Constant("b"))),
            Atom("edge", (Constant("b"), Constant("c"))),
        ), rules=rules)
        one = run_bundle(bundle)
        two = run_bundle(bundle)
        self.assertEqual(one.relations["path"], (("a", "b"), ("a", "c"), ("b", "c")))
        self.assertEqual(one.program_digest, two.program_digest)
        self.assertEqual(one.output_digest, two.output_digest)

    def test_negation_requires_and_uses_completeness(self):
        observed = R("observed", ("item", TypeName.SYMBOL))
        complete = R("observed_closed", ("item", TypeName.SYMBOL),
                     modality=Modality.COMPLETENESS, completes="observed")
        missing = R("missing", ("item", TypeName.SYMBOL))
        bundle = Bundle((observed, complete, missing),
                        facts=(Atom("observed_closed", (Constant("x"),)),),
                        rules=(Rule(Atom("missing", (Variable("x"),)),
                                    (Atom("observed_closed", (Variable("x"),)),
                                     Atom("observed", (Variable("x"),), negated=True))),))
        self.assertFalse(validate_bundle(bundle))
        self.assertEqual(run_bundle(bundle).relations["missing"], (("x",),))

    def test_count_aggregation_binds_result_and_agrees_for_nonempty_and_zero_groups(self):
        source = R("source", ("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER), context_indices=("tenant",))
        total = R("total", ("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER), context_indices=("tenant",))
        domain = R("tenants", ("tenant", TypeName.SYMBOL, True), finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("tenants_closed", ("tenant", TypeName.SYMBOL, True), modality=Modality.COMPLETENESS, completes="tenants", context_indices=("tenant",))
        aggregation = Aggregation("number", "source", ("tenant",), "n", "count", "tenants", "tenants_closed")
        rule = Rule(Atom("total", (Variable("t"), Variable("n"))),
                    (Atom("source", (Variable("t"), Variable("n"))),
                     Atom("tenants", (Variable("t"),)),
                     Atom("tenants_closed", (Variable("t"),))), aggregation=aggregation)
        fixed = (Atom("tenants", (Constant("t"),)), Atom("tenants_closed", (Constant("t"),)))
        for rows, expected in (((), (("t", 0),)),
                               ((Atom("source", (Constant("t"), Constant(1))),
                                 Atom("source", (Constant("t"), Constant(2)))), (("t", 2),))):
            bundle = Bundle((source, total, domain, closed), facts=(*rows, *fixed), rules=(rule,))
            self.assertFalse(validate_bundle(bundle))
            self.assertEqual(evaluate_python(bundle).relation_rows("total"), expected)
            self.assertEqual(run_bundle(bundle).relations["total"], expected)

    def test_context_columns_are_emitted_in_declared_order(self):
        relation = R("contextual", ("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True), ("value", TypeName.INTEGER), context_indices=("tenant", "run"))
        bundle = Bundle((relation,), facts=(Atom("contextual", (Constant("t"), Constant("r"), Constant(7))),))
        program = translate_bundle(bundle).program
        self.assertIn(".decl contextual(tenant:symbol, run:symbol, value:number)", program)
        self.assertEqual(run_bundle(bundle).relations["contextual"], (("t", "r", 7),))

    def test_output_limit_is_reported(self):
        relation = R("items", ("item", TypeName.SYMBOL))
        bundle = Bundle((relation,), facts=tuple(Atom("items", (Constant(str(i)),)) for i in range(4)))
        with self.assertRaises(OverflowError):
            run_bundle(bundle, max_output_bytes=1)

    def test_timeout_is_reported(self):
        relation = R("items", ("item", TypeName.SYMBOL))
        bundle = Bundle((relation,), facts=(Atom("items", (Constant("x"),)),))
        with self.assertRaises(TimeoutError):
            run_bundle(bundle, timeout=0.0)

    def test_sanitized_column_names_do_not_collide(self):
        relation = R("values", ("a-b", TypeName.SYMBOL), ("a_b", TypeName.SYMBOL))
        bundle = Bundle((relation,), facts=(Atom("values", (Constant("x"), Constant("y"))),))
        program = translate_bundle(bundle).program
        self.assertIn("a_b:symbol, a_b_2:symbol", program)
        self.assertEqual(run_bundle(bundle).relations["values"], (("x", "y"),))

    def test_nonprimitive_facts_are_rejected(self):
        derived = R("derived", ("x", TypeName.SYMBOL), primitive=False)
        with self.assertRaises(ValueError):
            translate_bundle(Bundle((derived,), facts=(Atom("derived", (Constant("x"),)),)))

    def test_producer_authored_claim_facts_are_rejected(self):
        claimed = R("producer_claim", ("x", TypeName.SYMBOL), modality="claim")
        atom = Atom("producer_claim", (Constant("x"),))
        issues = validate_bundle(Bundle((claimed,), facts=(atom,),
                                        evidence=(Evidence("producer", atom, source="test"),)))
        self.assertIn("producer-authored-claim", {issue.code for issue in issues})
        with self.assertRaises(ValueError):
            translate_bundle(Bundle((claimed,), facts=(atom,)))

    def test_json_metadata_round_trips_without_hashing_mapping(self):
        relation = R("metadata", ("payload", TypeName.JSON_METADATA_ONLY))
        bundle = Bundle((relation,), facts=(Atom("metadata", (Constant({"x": [1, 2]}, TypeName.JSON_METADATA_ONLY),)),))
        self.assertEqual(run_bundle(bundle).relations["metadata"], (({"x": [1, 2]},),))

    def test_empty_forall_is_inconsistent_with_bounded_basis(self):
        domain = R("empty_domain", ("member", TypeName.SYMBOL), finite=True, nonempty=True)
        closed = R("empty_domain_closed", modality=Modality.COMPLETENESS,
                   completes="empty_domain")
        covered = R("empty_covered", ("member", TypeName.SYMBOL))
        claim = Claim("empty_covered", (Variable("member"),), quantifier="forall", domain="empty_domain")
        result = run_bundle(Bundle((domain, closed, covered),
                                   facts=(Atom("empty_domain_closed", ()),),
                                   claims=(claim,))).claims[0]
        self.assertEqual(result.semantic.value, "unresolved")
        self.assertEqual(result.operational.value, "inconsistent-premises")
        self.assertEqual(result.basis.value, "bounded-history-model")
        self.assertEqual(result.missing_premises, ("domain:empty_domain",))

    def test_forall_binds_by_domain_column_name_when_columns_are_reordered(self):
        domain = R("ordered_domain", ("other", TypeName.SYMBOL), ("member", TypeName.SYMBOL),
                   finite=True, nonempty=True)
        closed = R("ordered_domain_closed", modality=Modality.COMPLETENESS,
                   completes="ordered_domain")
        covered = R("ordered_covered", ("member", TypeName.SYMBOL))
        claim = Claim("ordered_covered", (Variable("member"),), quantifier="forall", domain="ordered_domain")
        facts = (Atom("ordered_domain", (Constant("z"), Constant("a"))),
                 Atom("ordered_domain_closed", ()),
                 Atom("ordered_covered", (Constant("a"),)))
        result = run_bundle(Bundle((domain, closed, covered), facts=facts, claims=(claim,))).claims[0]
        self.assertEqual(result.semantic.value, "supported")

    def test_forall_requires_every_finite_domain_member(self):
        domain = R("domain", ("x", TypeName.SYMBOL), finite=True, nonempty=True)
        closed = R("domain_closed", modality=Modality.COMPLETENESS, completes="domain")
        covered = R("covered", ("value", TypeName.SYMBOL))
        claims = (Claim("covered", (Variable("x"),), quantifier="forall", domain="domain"),)
        bundle = Bundle((domain, closed, covered), facts=(Atom("domain", (Constant("a"),)), Atom("domain", (Constant("b"),)), Atom("domain_closed", ()), Atom("covered", (Constant("a"),))), claims=claims)
        result = run_bundle(bundle)
        self.assertEqual(result.claims[0].semantic.value, "unresolved")

    def test_any_and_all_compile_to_boolean_rows(self):
        source = R("source", ("tenant", TypeName.SYMBOL, True), ("value", TypeName.BOOLEAN), context_indices=("tenant",))
        out = R("out", ("tenant", TypeName.SYMBOL, True), ("value", TypeName.BOOLEAN), context_indices=("tenant",))
        domain = R("domain", ("tenant", TypeName.SYMBOL, True), finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("closed", ("tenant", TypeName.SYMBOL, True), modality=Modality.COMPLETENESS, completes="domain", context_indices=("tenant",))
        aggregation = Aggregation("any", "source", ("tenant",), "value", "any", "domain", "closed")
        rule = Rule(Atom("out", (Variable("tenant"), Variable("value"))), (Atom("source", (Variable("tenant"), Variable("value"))), Atom("domain", (Variable("tenant"),)), Atom("closed", (Variable("tenant"),))), aggregation=aggregation)
        bundle = Bundle((source, out, domain, closed), facts=(Atom("source", (Constant("t"), Constant(True))), Atom("domain", (Constant("t"),)), Atom("closed", (Constant("t"),))), rules=(rule,))
        self.assertFalse(validate_bundle(bundle))
        self.assertEqual(run_bundle(bundle).relations["out"], (("t", True),))

    def test_boolean_aggregates_count_truth_values_not_rows(self):
        def evaluate(operator, values):
            source = R("source", ("tenant", TypeName.SYMBOL, True), ("value", TypeName.BOOLEAN), context_indices=("tenant",))
            out = R("out", ("tenant", TypeName.SYMBOL, True), ("value", TypeName.BOOLEAN), context_indices=("tenant",))
            domain = R("domain", ("tenant", TypeName.SYMBOL, True), finite=True, nonempty=True, context_indices=("tenant",))
            closed = R("closed", ("tenant", TypeName.SYMBOL, True), modality=Modality.COMPLETENESS, completes="domain", context_indices=("tenant",))
            aggregate = Aggregation(operator, "source", ("tenant",), "value", operator, "domain", "closed")
            rule = Rule(Atom("out", (Variable("tenant"), Variable("value"))), (Atom("source", (Variable("tenant"), Variable("value"))), Atom("domain", (Variable("tenant"),)), Atom("closed", (Variable("tenant"),))), aggregation=aggregate)
            facts = tuple(Atom("source", (Constant("t"), Constant(value))) for value in values)
            facts += (Atom("domain", (Constant("t"),)), Atom("closed", (Constant("t"),)))
            return run_bundle(Bundle((source, out, domain, closed), facts=facts, rules=(rule,))).relations["out"]
        self.assertEqual(evaluate("any", (False,)), (("t", False),))
        self.assertEqual(evaluate("any", (False, True)), (("t", True),))
        self.assertEqual(evaluate("all", (True, True)), (("t", True),))
        self.assertEqual(evaluate("all", (True, False)), (("t", False),))
        self.assertEqual(evaluate("all", ()), (("t", False),))

    def test_boolean_all_requires_closed_domain_coverage(self):
        source = R("coverage_source", ("tenant", TypeName.SYMBOL, True),
                   ("member", TypeName.SYMBOL), ("holds", TypeName.BOOLEAN), context_indices=("tenant",))
        out = R("coverage_out", ("tenant", TypeName.SYMBOL, True), context_indices=("tenant",), primitive=False)
        domain = R("coverage_domain", ("tenant", TypeName.SYMBOL, True), ("member", TypeName.SYMBOL),
                   finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("coverage_closed", ("tenant", TypeName.SYMBOL, True), modality=Modality.COMPLETENESS,
                   completes="coverage_domain", context_indices=("tenant",))
        aggregate = Aggregation("coverage", "coverage_source", ("tenant",), "holds", "all",
                                "coverage_domain", "coverage_closed")
        rule = Rule(Atom("coverage_out", (Variable("tenant"),)),
                    (Atom("coverage_source", (Variable("tenant"), Variable("member"), Variable("holds"))),
                     Atom("coverage_domain", (Variable("tenant"), Variable("member"))),
                     Atom("coverage_closed", (Variable("tenant"),))), aggregation=aggregate)
        facts = (Atom("coverage_domain", (Constant("t"), Constant("a"))),
                 Atom("coverage_domain", (Constant("t"), Constant("b"))),
                 Atom("coverage_closed", (Constant("t"),)),
                 Atom("coverage_source", (Constant("t"), Constant("a"), Constant(True))))
        bundle = Bundle((source, out, domain, closed), facts=facts, rules=(rule,))
        self.assertEqual(evaluate_python(bundle).relation_rows("coverage_out"), ())
        self.assertEqual(run_bundle(bundle).relations["coverage_out"], ())

    def test_boolean_all_compares_exact_domain_identity_and_projects_source_dimensions(self):
        source = R("source_keys", ("tenant", TypeName.SYMBOL, True),
                   ("member", TypeName.SYMBOL), ("channel", TypeName.SYMBOL),
                   ("holds", TypeName.BOOLEAN), context_indices=("tenant",))
        out = R("all_keys", ("tenant", TypeName.SYMBOL, True), ("holds", TypeName.BOOLEAN),
                primitive=False, context_indices=("tenant",))
        domain = R("domain_keys", ("tenant", TypeName.SYMBOL, True), ("member", TypeName.SYMBOL),
                   finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("domain_keys_closed", ("tenant", TypeName.SYMBOL, True),
                   modality=Modality.COMPLETENESS, completes="domain_keys",
                   context_indices=("tenant",))
        aggregate = Aggregation("all_keys", "source_keys", ("tenant",), "holds", "all",
                                "domain_keys", "domain_keys_closed")
        rule = Rule(Atom("all_keys", (Variable("tenant"), Variable("holds"))),
                    (Atom("source_keys", (Variable("tenant"), Variable("member"),
                                          Variable("channel"), Variable("holds"))),
                     Atom("domain_keys", (Variable("tenant"), Variable("member"))),
                     Atom("domain_keys_closed", (Variable("tenant"),))), aggregation=aggregate)
        common = (Atom("domain_keys", (Constant("t"), Constant("a"))),
                  Atom("domain_keys", (Constant("t"), Constant("b"))),
                  Atom("domain_keys_closed", (Constant("t"),)))
        wrong_members = Bundle((source, out, domain, closed), facts=(*common,
            Atom("source_keys", (Constant("t"), Constant("a"), Constant("one"), Constant(True))),
            Atom("source_keys", (Constant("t"), Constant("c"), Constant("one"), Constant(True)))),
            rules=(rule,))
        projected_duplicates = Bundle((source, out, domain, closed), facts=(*common,
            Atom("source_keys", (Constant("t"), Constant("a"), Constant("one"), Constant(True))),
            Atom("source_keys", (Constant("t"), Constant("a"), Constant("two"), Constant(True))),
            Atom("source_keys", (Constant("t"), Constant("b"), Constant("one"), Constant(True)))),
            rules=(rule,))
        for bundle, expected in ((wrong_members, (("t", False),)),
                                 (projected_duplicates, (("t", True),))):
            with self.subTest(expected=expected):
                self.assertEqual(evaluate_python(bundle).relation_rows("all_keys"), expected)
                self.assertEqual(run_bundle(bundle).relations["all_keys"], expected)

    def test_boolean_all_false_is_a_guard_when_value_is_absent_from_head(self):
        source = R("guard_source", ("tenant", TypeName.SYMBOL, True), ("holds", TypeName.BOOLEAN), context_indices=("tenant",))
        out = R("guard_out", ("tenant", TypeName.SYMBOL, True), context_indices=("tenant",), primitive=False)
        domain = R("guard_domain", ("tenant", TypeName.SYMBOL, True), finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("guard_closed", ("tenant", TypeName.SYMBOL, True), modality=Modality.COMPLETENESS, completes="guard_domain", context_indices=("tenant",))
        aggregate = Aggregation("guard_all", "guard_source", ("tenant",), "holds", "all", "guard_domain", "guard_closed")
        rule = Rule(Atom("guard_out", (Variable("tenant"),)),
                    (Atom("guard_source", (Variable("tenant"), Variable("holds"))),
                     Atom("guard_domain", (Variable("tenant"),)), Atom("guard_closed", (Variable("tenant"),))),
                    aggregation=aggregate)
        base = (Atom("guard_domain", (Constant("t"),)), Atom("guard_closed", (Constant("t"),)))
        false_bundle = Bundle((source, out, domain, closed), facts=(*base, Atom("guard_source", (Constant("t"), Constant(True))), Atom("guard_source", (Constant("t"), Constant(False)))), rules=(rule,))
        true_bundle = Bundle((source, out, domain, closed), facts=(*base, Atom("guard_source", (Constant("t"), Constant(True)))), rules=(rule,))
        self.assertEqual(evaluate_python(false_bundle).relation_rows("guard_out"), ())
        self.assertEqual(run_bundle(false_bundle).relations["guard_out"], ())
        self.assertEqual(evaluate_python(true_bundle).relation_rows("guard_out"), (("t",),))
        self.assertEqual(run_bundle(true_bundle).relations["guard_out"], (("t",),))

    def test_output_subset_keeps_omitted_derived_claim_semantics(self):
        seed = R("seed", ("x", TypeName.SYMBOL))
        derived_claim = R("derived_claim", ("x", TypeName.SYMBOL),
                          modality="claim", primitive=False)
        audit = R("audit", ("x", TypeName.SYMBOL))
        bundle = Bundle((seed, derived_claim, audit),
                        facts=(Atom("seed", (Constant("v"),)),),
                        rules=(Rule(Atom("derived_claim", (Variable("x"),)),
                                    (Atom("seed", (Variable("x"),)),)),),
                        claims=(Claim("derived_claim", (Constant("v"),), id="claim"),))
        result = run_bundle(bundle, outputs=("audit",))
        self.assertEqual(result.claims[0].semantic.value, "supported")
        self.assertEqual(result.relations["derived_claim"], (("v",),))

    def test_output_subset_does_not_echo_unrequested_primitive_relations(self):
        left = R("left_input", ("x", TypeName.SYMBOL))
        right = R("right_input", ("x", TypeName.SYMBOL))
        bundle = Bundle((left, right), facts=(Atom("left_input", (Constant("a"),)), Atom("right_input", (Constant("b"),))))
        program = translate_bundle(bundle, outputs=("left_input",)).program
        self.assertIn(".output left_input", program)
        self.assertNotIn(".output right_input", program)
        result = run_bundle(bundle, outputs=("left_input",))
        self.assertEqual(result.relations["right_input"], (("b",),))

    def test_boolean_aggregates_support_global_zero_group(self):
        def bundle(operator, values):
            source = R("source", ("value", TypeName.BOOLEAN))
            out = R("out", ("value", TypeName.BOOLEAN))
            domain = R("domain", ("scope", TypeName.SYMBOL), finite=True, nonempty=True)
            closed = R("closed", ("scope", TypeName.SYMBOL), modality=Modality.COMPLETENESS, completes="domain")
            aggregate = Aggregation(operator, "source", (), "value", operator, "domain", "closed")
            rule = Rule(Atom("out", (Variable("value"),)), (Atom("source", (Variable("value"),)), Atom("domain", (Constant("global"),)), Atom("closed", (Constant("global"),))), aggregation=aggregate)
            facts = tuple(Atom("source", (Constant(value),)) for value in values)
            facts += (Atom("domain", (Constant("global"),)), Atom("closed", (Constant("global"),)))
            return Bundle((source, out, domain, closed), facts=facts, rules=(rule,))
        # ``all`` without a shared domain member identity is deliberately
        # invalid; ``any`` still exercises the nullary helper ABI.
        for operator in ("any",):
            program = translate_bundle(bundle(operator, (True,))).program
            self.assertIn(".decl __capcov_agg_", program)
            self.assertNotIn("(, n_true", program)

    def test_nullary_input_and_derived_predicates_round_trip(self):
        seed = R("seed")
        derived = R("derived")
        bundle = Bundle((seed, derived), facts=(Atom("seed", ()),), rules=(Rule(Atom("derived", ()), (Atom("seed", ()),)),))
        program = translate_bundle(bundle).program
        self.assertIn(".decl seed()", program)
        self.assertIn(".decl derived()", program)
        self.assertIn("derived() :- seed().", program)
        result = run_bundle(bundle)
        self.assertEqual(result.relations["seed"], ((),))
        self.assertEqual(result.relations["derived"], ((),))

    def test_forall_domain_is_scoped_by_shared_noncontext_constants(self):
        domain = R("capability_domain", ("tenant", TypeName.SYMBOL, True),
                   ("capability", TypeName.SYMBOL), ("member", TypeName.SYMBOL),
                   finite=True, nonempty=True, context_indices=("tenant",))
        closed = R("capability_domain_closed", ("tenant", TypeName.SYMBOL, True),
                   modality=Modality.COMPLETENESS, completes="capability_domain",
                   context_indices=("tenant",))
        source = R("capability_observed", ("tenant", TypeName.SYMBOL, True),
                   ("capability", TypeName.SYMBOL), ("member", TypeName.SYMBOL),
                   context_indices=("tenant",))
        covered = R("capability_covered", ("tenant", TypeName.SYMBOL, True),
                    ("capability", TypeName.SYMBOL), ("member", TypeName.SYMBOL),
                    modality="claim", primitive=False, context_indices=("tenant",))
        context = Context.from_mapping({"tenant": "t"})
        claim = Claim("capability_covered",
                      (Constant("t"), Constant("a"), Variable("member")), context,
                      quantifier="forall", domain="capability_domain", id="claim")
        facts = (Atom("capability_domain", (Constant("t"), Constant("a"), Constant("one"))),
                 Atom("capability_domain", (Constant("t"), Constant("b"), Constant("other"))),
                 Atom("capability_domain_closed", (Constant("t"),)),
                 Atom("capability_observed", (Constant("t"), Constant("a"), Constant("one"))))
        rule = Rule(Atom("capability_covered", (Variable("t"), Variable("c"), Variable("m"))),
                    (Atom("capability_observed", (Variable("t"), Variable("c"), Variable("m"))),))
        bundle = Bundle((domain, closed, source, covered), facts=facts, rules=(rule,), claims=(claim,))
        self.assertEqual(evaluate_python(bundle).claims[0].semantic.value, "supported")
        self.assertEqual(run_bundle(bundle).claims[0].semantic.value, "supported")

    def test_negative_rule_evidence_and_positive_mapping_conflict(self):
        observed_denial = R("denial_observed", ("x", TypeName.SYMBOL))
        denied = R("denied", ("x", TypeName.SYMBOL), modality="claim",
                   polarity="negative", primitive=False)
        allowed = R("allowed", ("x", TypeName.SYMBOL))
        mapping = EvidenceMapping("denied", "allowed", "support",
                                  bindings=(("x", "x"),), claim_id="claim")
        rule = Rule(Atom("denied", (Variable("x"),)),
                    (Atom("denial_observed", (Variable("x"),)),))
        bundle = Bundle((observed_denial, denied, allowed),
                        facts=(Atom("denial_observed", (Constant("v"),)),
                               Atom("allowed", (Constant("v"),))),
                        rules=(rule,), claims=(Claim("denied", (Constant("v"),), id="claim"),),
                        mappings=(mapping,))
        self.assertEqual(run_bundle(bundle).claims[0].semantic.value, "conflicting")

    def test_claim_context_filters_rule_derived_negative_evidence(self):
        observed = R("rejection_observed", ("tenant", TypeName.SYMBOL, True),
                     ("actor", TypeName.SYMBOL), context_indices=("tenant",))
        rejected = R("rejected", ("tenant", TypeName.SYMBOL, True),
                     ("actor", TypeName.SYMBOL), modality="claim", polarity="negative",
                     primitive=False, context_indices=("tenant",))
        rule = Rule(Atom("rejected", (Variable("tenant"), Variable("actor"))),
                    (Atom("rejection_observed", (Variable("tenant"), Variable("actor"))),))
        claim = Claim("rejected", (Variable("tenant"), Constant("actor")),
                      Context.from_mapping({"tenant": "t1"}))
        facts = (Atom("rejection_observed", (Constant("t1"), Constant("actor"))),
                 Atom("rejection_observed", (Constant("t2"), Constant("actor"))))
        bundle = Bundle((observed, rejected), facts=facts, rules=(rule,), claims=(claim,))
        result = run_bundle(bundle)
        self.assertEqual(result.claims[0].semantic.value, "refuted")
        wrong_context = Claim("rejected", (Variable("tenant"), Constant("actor")),
                              Context.from_mapping({"tenant": "missing"}))
        result = run_bundle(Bundle((observed, rejected), facts=facts, rules=(rule,),
                                   claims=(wrong_context,)))
        self.assertEqual(result.claims[0].semantic.value, "unresolved")

    def test_forall_support_requires_attributed_domain_closure(self):
        domain = R("reviewed_domain", ("member", TypeName.SYMBOL), finite=True, nonempty=True)
        closed = R("reviewed_domain_closed", modality=Modality.COMPLETENESS,
                   completes="reviewed_domain")
        observed = R("member_observed", ("member", TypeName.SYMBOL))
        covered = R("member_covered", ("member", TypeName.SYMBOL),
                    modality="claim", primitive=False)
        member = Atom("reviewed_domain", (Constant("a"),))
        witness = Atom("reviewed_domain_closed", ())
        seen = Atom("member_observed", (Constant("a"),))
        rule = Rule(Atom("member_covered", (Variable("member"),)),
                    (Atom("member_observed", (Variable("member"),)),))
        claim = Claim("member_covered", (Variable("member"),), quantifier="forall",
                      domain="reviewed_domain", id="claim")
        without_witness = Bundle((domain, closed, observed, covered), facts=(member, seen),
                                 rules=(rule,), claims=(claim,))
        for result in (evaluate_python(without_witness).claims[0].result,
                       run_bundle(without_witness).claims[0]):
            self.assertEqual(result.semantic.value, "unresolved")
            self.assertEqual(result.missing_premises, ("closure:reviewed_domain",))

        evidence = (Evidence("domain-member", member, source="review"),
                    Evidence("domain-closure", witness, source="review",
                             depends_on=("domain-member",)),
                    Evidence("member-observation", seen, source="producer"))
        reviewed = Bundle((domain, closed, observed, covered),
                          facts=(member, witness, seen), evidence=evidence,
                          rules=(rule,), claims=(claim,))
        self.assertFalse(validate_bundle(reviewed))
        python_result = evaluate_python(reviewed).claims[0].result
        self.assertEqual(python_result.semantic.value, "supported")
        self.assertEqual(python_result.support,
                         ("domain-closure", "domain-member", "member-observation"))
        self.assertEqual(run_bundle(reviewed).claims[0].semantic.value, "supported")

    def test_irrelevant_rule_does_not_suppress_another_claims_mapping(self):
        source = R("mapped_observation", ("x", TypeName.SYMBOL))
        claimed = R("shared_claim", ("x", TypeName.SYMBOL),
                    modality="claim", primitive=False)
        rule = Rule(Atom("shared_claim", (Constant("rule-only"),)),
                    (Atom("mapped_observation", (Constant("rule-premise"),)),))
        mapping = EvidenceMapping("shared_claim", "mapped_observation", "support",
                                  bindings=(("x", "x"),), claim_id="mapped")
        bundle = Bundle((source, claimed),
                        facts=(Atom("mapped_observation", (Constant("mapped"),)),),
                        rules=(rule,), claims=(Claim("shared_claim", (Constant("mapped"),),
                                                     id="mapped"),), mappings=(mapping,))
        self.assertEqual(evaluate_python(bundle).claims[0].semantic.value, "supported")
        self.assertEqual(run_bundle(bundle).claims[0].semantic.value, "supported")

    def test_supported_claim_does_not_mask_runtime_stale_status(self):
        source = R("fresh_support", ("x", TypeName.SYMBOL))
        stale = R("stale_observation", ("x", TypeName.SYMBOL))
        claimed = R("stale_claim", ("x", TypeName.SYMBOL),
                    modality="claim", primitive=False)
        rule = Rule(Atom("stale_claim", (Variable("x"),)),
                    (Atom("fresh_support", (Variable("x"),)),))
        diagnostic = DiagnosticRule("stale_observation", "observation",
                                    operational_status="stale", claim_id="claim")
        bundle = Bundle((source, stale, claimed),
                        facts=(Atom("fresh_support", (Constant("v"),)),
                               Atom("stale_observation", (Constant("v"),))),
                        rules=(rule,), claims=(Claim("stale_claim", (Constant("v"),),
                                                     id="claim"),), diagnostics=(diagnostic,))
        for result in (evaluate_python(bundle).claims[0].result,
                       run_bundle(bundle).claims[0]):
            self.assertEqual(result.semantic.value, "supported")
            self.assertEqual(result.operational.value, "stale")

    def test_missing_premise_templates_use_triggers_and_unrelated_outputs_keep_fallback(self):
        observed = R("trigger_observed", ("x", TypeName.SYMBOL))
        claimed = R("trigger_claimed", ("x", TypeName.SYMBOL), modality="claim")
        match = Atom("trigger_observed", (Constant("v"),))
        other = Atom("trigger_observed", (Constant("other"),))
        evidence = (Evidence("match", match, source="test"),
                    Evidence("other", other, source="test"))
        claim = Claim("trigger_claimed", (Constant("v"),), id="claim")
        mapping = EvidenceMapping("trigger_claimed", "trigger_observed", "observation",
                                  bindings=(("x", "x"),), claim_id="claim")
        unrelated = OutputTemplate("observed", "claim", evidence_id="match")
        base = Bundle((observed, claimed), facts=(match, other), evidence=evidence,
                      claims=(claim,), mappings=(mapping,), outputs=(unrelated,))
        fallback = ('claim:trigger_claimed:{}',)
        self.assertEqual(run_bundle(base).claims[0].missing_premises, fallback)

        fields = (("reason", TemplateValue(value="reviewed")),)
        active = OutputTemplate("missing_premise", "claim",
                                relation="trigger_observed", fields=fields,
                                requires_all_evidence=("match",),
                                excludes_evidence=("other",),
                                when_claim="unresolved")
        inactive = OutputTemplate("missing_premise", "claim",
                                  relation="trigger_observed", fields=fields,
                                  requires_any_evidence=("other",),
                                  when_claim="refuted")
        for output, expected in ((active, ({"relation": "trigger_observed",
                                           "reason": "reviewed"},)),
                                 (inactive, fallback)):
            bundle = Bundle(base.relations, base.facts, evidence=base.evidence,
                            claims=base.claims, mappings=base.mappings,
                            outputs=(output,))
            with self.subTest(output=output):
                self.assertEqual(run_bundle(bundle).claims[0].missing_premises,
                                 expected)

    def test_forall_revocation_cannot_shrink_the_quantified_domain(self):
        domain = R("revoked_domain", ("member", TypeName.SYMBOL),
                   finite=True, nonempty=True)
        closed = R("revoked_domain_closed", modality=Modality.COMPLETENESS,
                   completes="revoked_domain")
        blocker = R("revoked_assumption", ("name", TypeName.SYMBOL),
                    modality="assumption")
        observed = R("revoked_observed", ("member", TypeName.SYMBOL))
        covered = R("revoked_covered", ("member", TypeName.SYMBOL),
                    modality="claim", primitive=False)
        blocked = Atom("revoked_assumption", (Constant("bad"),))
        member_a = Atom("revoked_domain", (Constant("a"),))
        member_b = Atom("revoked_domain", (Constant("b"),))
        witness = Atom("revoked_domain_closed", ())
        seen_a = Atom("revoked_observed", (Constant("a"),))
        seen_b = Atom("revoked_observed", (Constant("b"),))
        evidence = (
            Evidence("blocked", blocked, source="test"),
            Evidence("domain-a", member_a, source="test"),
            Evidence("domain-b-tainted", member_b, source="test",
                     depends_on=("blocked",)),
            Evidence("domain-closed", witness, source="test"),
            Evidence("seen-a", seen_a, source="test"),
            Evidence("seen-b", seen_b, source="test"),
        )
        claim = Claim("revoked_covered", (Variable("member"),),
                      quantifier="forall", domain="revoked_domain", id="claim")
        rule = Rule(Atom("revoked_covered", (Variable("member"),)),
                    (Atom("revoked_observed", (Variable("member"),)),))
        diagnostic = DiagnosticRule("revoked_assumption", "forbidden",
                                    claim_id="claim")
        bundle = Bundle((domain, closed, blocker, observed, covered),
                        facts=(blocked, member_a, member_b, witness, seen_a, seen_b),
                        evidence=evidence, rules=(rule,), claims=(claim,),
                        diagnostics=(diagnostic,))
        python = evaluate_python(bundle).claims[0].result
        souffle = run_bundle(bundle).claims[0]
        self.assertEqual(python.semantic.value, "unresolved")
        self.assertEqual(souffle.semantic, python.semantic)
        self.assertEqual(souffle.operational, python.operational)
        self.assertEqual(souffle.missing_premises, python.missing_premises)
        self.assertEqual(souffle.missing_premises,
                         ('domain-evidence:revoked_domain:["b"]',))

    def test_claim_local_reruns_share_one_process_budget(self):
        rejected = R("budget_rejected", ("x", TypeName.SYMBOL), modality="assumption")
        observed = R("budget_observed", ("x", TypeName.SYMBOL))
        claimed = R("budget_claimed", ("x", TypeName.SYMBOL),
                    modality="claim", primitive=False)
        trigger = Atom("budget_rejected", (Constant("v"),))
        seen = Atom("budget_observed", (Constant("v"),))
        evidence = (Evidence("blocked", trigger, source="test"),
                    Evidence("tainted", seen, source="test", depends_on=("blocked",)))
        rule = Rule(Atom("budget_claimed", (Variable("x"),)),
                    (Atom("budget_observed", (Variable("x"),)),))
        claims = (Claim("budget_claimed", (Constant("v"),), id="one"),
                  Claim("budget_claimed", (Constant("v"),), id="two"))
        diagnostics = (DiagnosticRule("budget_rejected", "forbidden", claim_id="one"),
                       DiagnosticRule("budget_rejected", "forbidden", claim_id="two"))
        bundle = Bundle((rejected, observed, claimed), facts=(trigger, seen),
                        evidence=evidence, rules=(rule,), claims=claims,
                        diagnostics=diagnostics)
        with self.assertRaisesRegex(OverflowError, "process"):
            run_bundle(bundle, max_processes=1)


if __name__ == "__main__":
    unittest.main()
