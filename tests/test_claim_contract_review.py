import json
import math
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capcov.claims import (Aggregation, Atom, Claim, Column, Comparison,
    Constant, Context, Modality, RelationDecl, Rule, TypeName, Variable,
    bundle_from_json, canonical_json, validate_bundle)
from tests.claim_fixtures import Bundle  # attributed fixture bundles


def R(name, cols, **kw):
    return RelationDecl(name, tuple(Column(n, t, context=c) for n, t, c in cols), **kw)


class ContractReviewTests(unittest.TestCase):
    def test_values_are_frozen_and_unsupported_values_rejected(self):
        value = {"x": [1, {"y": 2}]}
        c = Constant(value, TypeName.JSON_METADATA_ONLY)
        value["x"].append(3)
        self.assertEqual(canonical_json(c), '{"type":"json-metadata-only","value":{"x":[1,{"y":2}]}}')
        with self.assertRaises(TypeError): Constant({1: "bad"})
        with self.assertRaises(ValueError): Constant(math.nan)
        with self.assertRaises(TypeError): canonical_json({"bad": {1: 2}})

    def test_fact_rule_claim_and_comparison_types_are_checked(self):
        rels = (R("a", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False))),
                R("b", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False))))
        bad_fact = Atom("a", (Constant("t"), Constant("not-int")))
        bad_rule = Rule(Atom("b", (Variable("t"), Variable("x"))),
                        (Atom("a", (Variable("t"), Variable("x"))),
                         Comparison(Variable("x"), "=", Constant("wrong"))))
        bad_claim = Claim("a", (Constant(4), Constant(3)), Context.from_mapping({"tenant": "t"}))
        codes = {i.code for i in validate_bundle(Bundle(rels, facts=(bad_fact,), rules=(bad_rule,), claims=(bad_claim,)))}
        self.assertIn("type-mismatch", codes)

    def test_completeness_witness_names_target_and_matches_scope(self):
        obs = R("obs", (("tenant", TypeName.SYMBOL, True), ("event", TypeName.SYMBOL, False)), context_indices=("tenant",))
        done = R("done", (("tenant", TypeName.SYMBOL, True), ("event", TypeName.SYMBOL, False)), context_indices=("tenant",))
        complete = R("complete", (("tenant", TypeName.SYMBOL, True), ("event", TypeName.SYMBOL, False)), modality=Modality.COMPLETENESS, completes="obs", context_indices=("tenant",))
        rule = Rule(Atom("done", (Variable("t"), Variable("e"))), (Atom("complete", (Variable("t"), Variable("e"))), Atom("obs", (Variable("u"), Variable("e")), negated=True)))
        self.assertIn("missing-completeness", {i.code for i in validate_bundle(Bundle((obs, done, complete), rules=(rule,)))})

    def test_completeness_rejects_extra_context_dimension(self):
        obs = R("obs", (("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True)), context_indices=("tenant",))
        done = R("done", (("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True)), context_indices=("tenant",))
        complete = R("complete", (("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True)), modality=Modality.COMPLETENESS, completes="obs", context_indices=("tenant", "run"))
        rule = Rule(Atom("done", (Variable("t"), Variable("r"))), (Atom("complete", (Variable("t"), Variable("r"))), Atom("obs", (Variable("t"), Variable("r")), negated=True)))
        self.assertIn("missing-completeness", {i.code for i in validate_bundle(Bundle((obs, done, complete), rules=(rule,)))})

    def test_negated_variables_cannot_introduce_bindings(self):
        a = R("a", (("x", TypeName.SYMBOL, False),))
        b = R("b", (("x", TypeName.SYMBOL, False),))
        rule = Rule(Atom("b", (Variable("x"),)), (Atom("a", (Variable("y"),), negated=True),))
        self.assertIn("unsafe-negation", {i.code for i in validate_bundle(Bundle((a, b), rules=(rule,)))})

    def test_aggregation_domain_closure_source_and_recursion(self):
        source = R("src", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",))
        head = R("total", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",))
        domain = R("tenants", (("tenant", TypeName.SYMBOL, True),), modality=Modality.ASSUMPTION, finite=True, nonempty=True, context_indices=("tenant",))
        closure = R("tenants_closed", (("tenant", TypeName.SYMBOL, True),), modality=Modality.COMPLETENESS, completes="tenants", context_indices=("tenant",))
        agg = Aggregation("sum", "src", ("tenant",), "n", operator="sum", domain="tenants", closure_witness="tenants_closed")
        valid = Bundle((source, head, domain, closure), rules=(Rule(Atom("total", (Variable("t"), Variable("n"))), (Atom("src", (Variable("t"), Variable("n"))), Atom("tenants", (Variable("t"),)), Atom("tenants_closed", (Variable("t"),))), aggregation=agg),))
        self.assertNotIn("aggregation-domain", {i.code for i in validate_bundle(valid)})
        bad = Bundle((source, head, domain, closure), rules=(Rule(Atom("total", (Variable("t"), Variable("n"))), (Atom("src", (Variable("t"), Variable("n"))),), aggregation=Aggregation("x", "missing", (), "n", domain="tenants", closure_witness="wrong")),))
        self.assertTrue({i.code for i in validate_bundle(bad)} & {"aggregation-source", "aggregation-closure"})

    def test_forall_requires_finite_explicitly_nonempty_domain(self):
        claim_rel = R("ok", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        empty = R("tenants", (("tenant", TypeName.SYMBOL, True),), finite=True, nonempty=False, context_indices=("tenant",))
        claim = Claim("ok", (Constant("t"),), Context.from_mapping({"tenant": "t"}), quantifier="forall", domain="tenants")
        self.assertIn("forall-domain", {i.code for i in validate_bundle(Bundle((claim_rel, empty), claims=(claim,)))})

    def test_cross_context_join_needs_compatibility_witness(self):
        a = R("a", (("tenant", TypeName.SYMBOL, True), ("x", TypeName.SYMBOL, False)), context_indices=("tenant",))
        b = R("b", (("tenant", TypeName.SYMBOL, True), ("x", TypeName.SYMBOL, False)), context_indices=("tenant",))
        out = R("out", (("tenant", TypeName.SYMBOL, True), ("x", TypeName.SYMBOL, False)), context_indices=("tenant",))
        rule = Rule(Atom("out", (Variable("t"), Variable("x"))), (Atom("a", (Variable("t"), Variable("x"))), Atom("b", (Variable("u"), Variable("x")))))
        self.assertIn("missing-compatibility", {i.code for i in validate_bundle(Bundle((a, b, out), rules=(rule,)))})

    def test_compatibility_payload_must_use_declared_context_positions(self):
        a = R("a", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        b = R("b", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        out = R("out", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        compat = RelationDecl("compat", (Column("left", TypeName.SYMBOL, context=True), Column("right", TypeName.SYMBOL, context=True), Column("payload", TypeName.SYMBOL)), modality=Modality.COMPATIBILITY, context_indices=("left", "right"), compatibility_targets=("a", "b"), compatibility_context_indices=("left", "right"))
        rule = Rule(Atom("out", (Variable("t"),)), (Atom("a", (Variable("u"),)), Atom("b", (Variable("v"),)), Atom("compat", (Variable("x"), Variable("y"), Variable("u")))))
        self.assertIn("missing-compatibility", {i.code for i in validate_bundle(Bundle((a, b, out, compat), rules=(rule,)))})

    def test_compatibility_witness_matches_constant_context_terms(self):
        a = R("a", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",)); b = R("b", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",)); out = R("out", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        compat = RelationDecl("compat", (Column("left", TypeName.SYMBOL, context=True), Column("right", TypeName.SYMBOL, context=True)), modality=Modality.COMPATIBILITY, context_indices=("left", "right"), compatibility_targets=("a", "b"), compatibility_context_indices=("left", "right"))
        rule = Rule(Atom("out", (Constant("left"),)), (Atom("a", (Constant("left"),)), Atom("b", (Constant("right"),)), Atom("compat", (Constant("wrong"), Constant("also-wrong")))))
        self.assertIn("missing-compatibility", {i.code for i in validate_bundle(Bundle((a, b, out, compat), rules=(rule,)))})

    def test_aggregation_requires_domain_and_closure_body_bindings(self):
        src = R("src", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",)); out = R("out", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",)); dom = R("dom", (("tenant", TypeName.SYMBOL, True),), finite=True, nonempty=True, context_indices=("tenant",)); clo = R("closed", (("tenant", TypeName.SYMBOL, True),), modality=Modality.COMPLETENESS, completes="dom", context_indices=("tenant",))
        agg = Aggregation("s", "src", ("tenant",), "n", "sum", "dom", "closed")
        rule = Rule(Atom("out", (Variable("t"), Variable("n"))), (Atom("src", (Variable("t"), Variable("n"))), Atom("dom", (Variable("wrong"),)), Atom("closed", (Variable("other"),))), aggregation=agg)
        codes = {i.code for i in validate_bundle(Bundle((src, out, dom, clo), rules=(rule,)))}
        self.assertTrue({"aggregation-domain", "aggregation-closure"}.issubset(codes))

    def test_indirect_aggregation_cycle_is_rejected(self):
        a = R("a", (("x", TypeName.SYMBOL, False),)); b = R("b", (("x", TypeName.SYMBOL, False),)); c = R("c", (("x", TypeName.SYMBOL, False),))
        dom = R("dom", (("x", TypeName.SYMBOL, False),), finite=True, nonempty=True); clo = R("closed", (("x", TypeName.SYMBOL, False),), modality=Modality.COMPLETENESS, completes="dom")
        agg = Aggregation("a_sum", "c", ("x",), "x", "count", "dom", "closed")
        rules = (Rule(Atom("a", (Variable("x"),)), (Atom("b", (Variable("x"),)),), aggregation=agg), Rule(Atom("b", (Variable("x"),)), (Atom("c", (Variable("x"),)),)), Rule(Atom("c", (Variable("x"),)), (Atom("a", (Variable("x"),)),)))
        self.assertIn("recursive-aggregation", {i.code for i in validate_bundle(Bundle((a, b, c, dom, clo), rules=rules))})

    def test_json_ingestion_rejects_unknown_fields_and_bad_version(self):
        base = {"schema_version": 1, "relations": [], "facts": [], "rules": [], "claims": [], "metadata": {}}
        self.assertEqual(bundle_from_json(json.dumps(base)).schema_version, 1)
        bad = dict(base); bad["extra"] = True
        with self.assertRaises(ValueError): bundle_from_json(bad)
        with self.assertRaises(ValueError): bundle_from_json('{"schema_version":1,"relations":[],"facts":[],"rules":[],"claims":[],"metadata":{},"x":NaN}')
        bad = dict(base); bad["schema_version"] = 2
        with self.assertRaises(ValueError): bundle_from_json(bad)

    def test_validation_is_total_for_malformed_rule_parts(self):
        rel = R("a", (("x", TypeName.SYMBOL, False),))
        atom = Atom("a", (Variable("x"),))
        malformed = Rule(atom, (atom,))
        bundle = Bundle((rel,), rules=(malformed,))
        object.__setattr__(malformed, "head", object())
        object.__setattr__(malformed, "body", (object(),))
        object.__setattr__(malformed, "aggregation", object())
        codes = {i.code for i in validate_bundle(bundle)}
        self.assertTrue({"atom-type", "aggregation-type"}.issubset(codes))

    def test_declared_constant_type_is_checked_against_value(self):
        rel = R("a", (("x", TypeName.INTEGER, False),))
        issues = validate_bundle(Bundle((rel,), facts=(Atom("a", (Constant("1", TypeName.INTEGER),)),)))
        self.assertIn("type-mismatch", {i.code for i in issues})

    def test_frozen_json_is_recognized_as_json_type(self):
        rel = R("a", (("payload", TypeName.JSON_METADATA_ONLY, False),))
        self.assertFalse(validate_bundle(Bundle((rel,), facts=(Atom("a", (Constant({"x": [1]}, TypeName.JSON_METADATA_ONLY),)),))))

    def test_context_and_metadata_duplicates_are_rejected(self):
        with self.assertRaises(ValueError): Context((('tenant', 'a'), ('tenant', 'b')))
        with self.assertRaises(ValueError): Bundle((), metadata=(('x', 1), ('x', 2)))
        rel = R("a", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        claim = Claim("a", (Constant("t"),), Context.from_mapping({"tenant": 4}))
        self.assertIn("type-mismatch", {i.code for i in validate_bundle(Bundle((rel,), claims=(claim,)))})

    def test_aggregation_requires_body_source_and_head_dataflow(self):
        src = R("src", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",))
        out = R("out", (("tenant", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant",))
        dom = R("dom", (("tenant", TypeName.SYMBOL, True),), finite=True, nonempty=True, context_indices=("tenant",))
        clo = R("closed", (("tenant", TypeName.SYMBOL, True),), modality=Modality.COMPLETENESS, completes="dom", context_indices=("tenant",))
        agg = Aggregation("s", "src", ("tenant",), "n", "sum", "dom", "closed")
        rule = Rule(Atom("out", (Variable("t"), Variable("z"))), (), aggregation=agg)
        self.assertIn("aggregation-source", {i.code for i in validate_bundle(Bundle((src, out, dom, clo), rules=(rule,)))})

    def test_aggregation_preserves_every_source_context_dimension(self):
        src = R("src", (("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant", "run"))
        out = R("out", (("tenant", TypeName.SYMBOL, True), ("run", TypeName.SYMBOL, True), ("n", TypeName.INTEGER, False)), context_indices=("tenant", "run"))
        dom = R("dom", (("tenant", TypeName.SYMBOL, True),), finite=True, nonempty=True, context_indices=("tenant",))
        clo = R("closed", (("tenant", TypeName.SYMBOL, True),), modality=Modality.COMPLETENESS, completes="dom", context_indices=("tenant",))
        agg = Aggregation("s", "src", ("tenant",), "n", "sum", "dom", "closed")
        rule = Rule(Atom("out", (Variable("t"), Variable("wrong_run"), Variable("n"))), (Atom("src", (Variable("t"), Variable("run"), Variable("n"))),), aggregation=agg)
        self.assertIn("aggregation-head", {i.code for i in validate_bundle(Bundle((src, out, dom, clo), rules=(rule,)))})

    def test_all_aggregation_requires_nonempty_domain(self):
        src = R("src", (("x", TypeName.SYMBOL, False),)); out = R("out", (("x", TypeName.SYMBOL, False),))
        dom = R("dom", (("x", TypeName.SYMBOL, False),), finite=True, nonempty=False); clo = R("closed", (("x", TypeName.SYMBOL, False),), modality=Modality.COMPLETENESS, completes="dom")
        agg = Aggregation("a", "src", ("x",), "x", "all", "dom", "closed")
        rule = Rule(Atom("out", (Variable("x"),)), (Atom("src", (Variable("x"),)),), aggregation=agg)
        self.assertIn("aggregation-domain", {i.code for i in validate_bundle(Bundle((src, out, dom, clo), rules=(rule,)))})

    def test_unsigned_accepts_nonnegative_integers_and_rejects_negative(self):
        rel = R("u", (("n", TypeName.UNSIGNED, False),))
        self.assertNotIn("type-mismatch", {i.code for i in validate_bundle(Bundle((rel,), facts=(Atom("u", (Constant(0),)),)))})
        self.assertNotIn("type-mismatch", {i.code for i in validate_bundle(Bundle((rel,), facts=(Atom("u", (Constant(4, TypeName.UNSIGNED),)),)))})
        self.assertIn("type-mismatch", {i.code for i in validate_bundle(Bundle((rel,), facts=(Atom("u", (Constant(-1, TypeName.UNSIGNED),)),)))})

    def test_compatibility_targets_and_positions_are_explicit(self):
        a = R("a", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        b = R("b", (("tenant", TypeName.SYMBOL, True),), context_indices=("tenant",))
        c = R("compat", (("left", TypeName.SYMBOL, True), ("right", TypeName.SYMBOL, True)), modality=Modality.COMPATIBILITY, context_indices=("left", "right"), compatibility_targets=("a", "b"), compatibility_context_indices=("left", "right"))
        self.assertFalse({i.code for i in validate_bundle(Bundle((a, b, c)))} & {"compatibility-target", "compatibility-context"})
        bad = RelationDecl("bad", c.columns, modality=Modality.COMPATIBILITY, context_indices=("left", "right"), compatibility_targets=("a", "missing"), compatibility_context_indices=("wrong",))
        self.assertTrue({"compatibility-target", "compatibility-context"}.issubset({i.code for i in validate_bundle(Bundle((a, b, bad)))}))

    def test_structural_sorting_and_default_json_validation(self):
        r = R("a", (("x", TypeName.SYMBOL, False),))
        one = Bundle((r,), facts=(Atom("a", (Constant("b"),)), Atom("a", (Constant("a"),))))
        two = Bundle((r,), facts=(Atom("a", (Constant("a"),)), Atom("a", (Constant("b"),))))
        self.assertEqual(canonical_json(one), canonical_json(two))
        payload = {"schema_version": 1, "relations": [{"name": "a", "columns": [{"name": "x", "type": "symbol", "context": False}]}], "facts": [{"relation": "a", "terms": [{"value": 3}]}], "rules": [], "claims": [], "metadata": {}}
        with self.assertRaises(Exception): bundle_from_json(payload)


if __name__ == "__main__": unittest.main()
