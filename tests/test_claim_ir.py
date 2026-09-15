import copy
import json
import unittest

from capcov.claims import (Atom, Bundle, BundleIngestionError, Claim, Column,
    Constant, Context, DiagnosticPolicy, DiagnosticRule, Evidence, Modality,
    OutputTemplate, RelationDecl, Rule, TemplateValue, TypeName, Variable,
    assert_valid, bundle_from_json, canonical_json, digest, validate_bundle)


def rel(name, *cols, modality=Modality.OBSERVATION):
    return RelationDecl(name, tuple(Column(c, TypeName.SYMBOL, context=(c in {"tenant", "run"})) for c in cols), modality=modality)


class ClaimIRTests(unittest.TestCase):
    def assert_named_invalid_input(self, raw):
        for validate in (False, True):
            for source_name, source in (
                    ("mapping", copy.deepcopy(raw)),
                    ("json", json.dumps(raw))):
                with self.subTest(source=source_name, validate=validate):
                    with self.assertRaises(BundleIngestionError) as caught:
                        bundle_from_json(source, validate=validate)
                    self.assertEqual(type(caught.exception),
                                     BundleIngestionError)
                    self.assertEqual(caught.exception.operational_failure,
                                     "invalid-input")

    def test_canonical_json_and_digest_are_stable(self):
        one = Bundle((rel("seen", "tenant", "event"),), facts=(Atom("seen", (Constant("t"), Constant("e"))),))
        two = Bundle((rel("seen", "tenant", "event"),), facts=(Atom("seen", (Constant("t"), Constant("e"))),))
        self.assertEqual(canonical_json(one), canonical_json(two))
        self.assertEqual(digest(one), digest(two))
        self.assertEqual(len(digest(one)), 64)

    def test_bundle_rejects_non_ir_collection_members_before_canonical_replay(self):
        cases = (
            {"relations": (object(),)},
            {"relations": (), "evidence": (object(),)},
            {"relations": (), "outputs": (object(),)},
        )
        for kwargs in cases:
            with self.subTest(field=next(reversed(kwargs))):
                with self.assertRaises(TypeError):
                    Bundle(**kwargs)

    def test_bundle_construction_rejects_malformed_nested_ir(self):
        relation = rel("seen", "value")
        valid_atom = Atom("seen", (Constant("v"),))
        cases = (
            ("evidence-atom", lambda: Bundle(
                (relation,), evidence=(Evidence(
                    "e", object(), source="producer"),))),
            ("atom-terms", lambda: Bundle(
                (relation,), facts=(Atom("seen", (object(),)),))),
            ("rule-head", lambda: Bundle(
                (relation,), rules=(Rule(object(), (valid_atom,)),))),
            ("rule-body", lambda: Bundle(
                (relation,), rules=(Rule(valid_atom, (object(),)),))),
            ("diagnostic-predicate", lambda: Bundle(
                (relation,), diagnostics=(DiagnosticRule(
                    "seen", "observation", predicate=(object(),)),))),
        )
        for name, factory in cases:
            with self.subTest(case=name), self.assertRaises(TypeError):
                factory()

    def test_bundle_rejects_mutated_context_policy_and_template_internals(self):
        relation = rel("seen", "value")
        atom = Atom("seen", (Constant("v"),))

        for owner in ("claim", "evidence"):
            for malformed in (
                    (("value", object()),),
                    (("value", []),),
                    [["value", "v"]],
                    ((1, "v"),)):
                with self.subTest(owner=owner,
                                  malformed=type(malformed).__name__):
                    context = Context.from_mapping({"value": "v"})
                    object.__setattr__(context, "values", malformed)
                    kwargs = ({"claims": (Claim(
                        "seen", (Constant("v"),), context, id="claim"),)}
                        if owner == "claim" else
                        {"facts": (atom,), "evidence": (Evidence(
                            "evidence", atom, context, source="producer"),)})
                    with self.assertRaises(TypeError):
                        Bundle((relation,), **kwargs)

        for field in (
                "missing_premises", "inconsistent_premises", "out_of_scope",
                "forbidden_evidence", "revocation", "completeness"):
            with self.subTest(policy_field=field):
                policy = DiagnosticPolicy()
                object.__setattr__(policy, field, object())
                with self.assertRaises(TypeError):
                    Bundle((), diagnostic_policy=policy)

        template = TemplateValue(value={"nested": [1, 2]},
                                 type="json-metadata-only")
        object.__setattr__(template, "value", [1, 2])
        output = OutputTemplate(
            "missing_premise", "claim", fields=(("payload", template),))
        with self.assertRaises(TypeError):
            Bundle((relation,), outputs=(output,))

    def test_template_values_are_deeply_frozen_and_round_trip(self):
        mutable = {"nested": [1, {"ok": True}]}
        template = TemplateValue(
            value=mutable, type="json-metadata-only")
        mutable["nested"].append("producer-mutation")
        atom = Atom("seen", ())
        output = OutputTemplate(
            "observed", "claim", evidence_id="evidence",
            fields=(("payload", template),))
        bundle = Bundle(
            (rel("seen"),), facts=(atom,),
            evidence=(Evidence("evidence", atom, source="producer"),),
            claims=(Claim("seen", (), id="claim"),), outputs=(output,))
        before = canonical_json(bundle)
        self.assertNotIn("producer-mutation", before)
        reloaded = bundle_from_json(before, validate=True)
        self.assertEqual(canonical_json(reloaded), before)
        self.assertEqual(digest(reloaded), digest(bundle))

    @staticmethod
    def diagnostic_document(**overrides):
        diagnostic = {
            "claim_id": "claim",
            "trigger_relation": "seen",
            "effect": "observation",
            **overrides,
        }
        return {
            "schema_version": 1,
            "relations": [{"name": "seen", "columns": []}],
            "claims": [{"id": "claim", "relation": "seen", "terms": []}],
            "diagnostics": [diagnostic],
        }

    def test_diagnostic_scalars_are_typed_during_ingestion_in_both_modes(self):
        for validate in (False, True):
            valid = self.diagnostic_document(
                when_missing=True, required=False, message="reviewed")
            for source_name, source in (
                    ("mapping", valid), ("json", json.dumps(valid))):
                with self.subTest(validate=validate, case="valid",
                                  source=source_name):
                    bundle = bundle_from_json(source, validate=validate)
                    self.assertTrue(bundle.diagnostics[0].when_missing)
                    self.assertFalse(bundle.diagnostics[0].required)
                    self.assertEqual(bundle.diagnostics[0].message, "reviewed")
        invalid = {
            "when_missing": (0, 1, "false", None, [], {}),
            "required": (0, 1, "true", None, [], {}),
            "message": (0, 1, False, None, [], {}),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, type=type(value).__name__):
                    self.assert_named_invalid_input(
                        self.diagnostic_document(**{field: value}))

    def test_evidence_rejects_dual_canonical_and_wire_atom_forms(self):
        for relation in ("seen", "decoy"):
            raw = {
                "schema_version": 1,
                "relations": [{"name": "seen", "columns": []}],
                "facts": [{"relation": "seen", "terms": []}],
                "evidence": [{
                    "id": "evidence", "source": "producer",
                    "atom": {"relation": "seen", "terms": []},
                    "relation": relation, "terms": [],
                }],
            }
            with self.subTest(relation=relation):
                self.assert_named_invalid_input(raw)

    def test_schema_version_requires_an_exact_non_boolean_integer(self):
        cases = (True, 1.0, "1", None)
        for value in cases:
            with self.subTest(source="ingestion", value=repr(value)):
                self.assert_named_invalid_input({
                    "schema_version": value,
                })
            with self.subTest(source="constructor", value=repr(value)):
                with self.assertRaises(ValueError):
                    Bundle((), schema_version=value)
        self.assert_named_invalid_input({})

    def test_deep_json_and_mapping_recursion_are_named_invalid_input(self):
        deep_json = (
            '{"schema_version":1,"metadata":{"deep":'
            + "[" * 2000 + "null" + "]" * 2000 + "}}")
        nested = None
        for _ in range(2000):
            nested = {"next": nested}
        deep_mapping = {
            "schema_version": 1,
            "relations": [],
            "metadata": {"deep": nested},
        }
        for source in (deep_json, deep_mapping):
            for validate in (False, True):
                with self.subTest(source=type(source).__name__,
                                  validate=validate):
                    with self.assertRaises(BundleIngestionError) as caught:
                        bundle_from_json(source, validate=validate)
                    self.assertEqual(
                        caught.exception.operational_failure, "invalid-input")
                    self.assertIsInstance(caught.exception.__cause__,
                                          RecursionError)

    def test_malformed_raw_members_have_a_named_ingestion_failure(self):
        for field in ("relations", "evidence", "outputs"):
            with self.subTest(field=field):
                raw = {"schema_version": 1, field: [17]}
                with self.assertRaises(BundleIngestionError) as caught:
                    bundle_from_json(raw)
                self.assertEqual(caught.exception.operational_failure,
                                 "invalid-input")

    def test_object_shaped_fields_reject_present_invalid_values(self):
        cases = (
            ("metadata", {"schema_version": 1, "metadata": False}),
            ("diagnostic-policy", {
                "schema_version": 1, "diagnostic_policy": 0}),
            ("claim-context", {
                "schema_version": 1,
                "relations": [{"name": "seen", "columns": []}],
                "claims": [{"id": "claim", "relation": "seen",
                            "terms": [], "context": []}],
            }),
            ("evidence-context", {
                "schema_version": 1,
                "relations": [{"name": "seen", "columns": []}],
                "facts": [{"relation": "seen", "terms": []}],
                "evidence": [{"id": "evidence", "relation": "seen",
                              "terms": [], "context": ""}],
            }),
            ("diagnostic-predicate", {
                **self.diagnostic_document(),
                "diagnostics": [{
                    "claim_id": "claim", "trigger_relation": "seen",
                    "effect": "observation", "predicate": 0,
                }],
            }),
            ("output-fields", {
                "schema_version": 1,
                "relations": [{"name": "seen", "columns": []}],
                "claims": [{"id": "claim", "relation": "seen",
                            "terms": []}],
                "outputs": [{"kind": "missing_premise",
                             "claim_id": "claim", "fields": False}],
            }),
        )
        for name, raw in cases:
            with self.subTest(case=name):
                self.assert_named_invalid_input(raw)

    def test_all_array_fields_reject_wrong_container_kinds(self):
        nested_cases = (
            ("relation.columns", "relations", [{
                "name": "seen", "columns": "",
            }]),
            ("relation.producer_classes", "relations", [{
                "name": "seen", "columns": [], "producer_classes": "ab",
            }]),
            ("relation.context_indices", "relations", [{
                "name": "seen", "columns": [], "context_indices": {},
            }]),
            ("relation.compatibility_targets", "relations", [{
                "name": "seen", "columns": [],
                "compatibility_targets": False,
            }]),
            ("relation.compatibility_context_indices", "relations", [{
                "name": "seen", "columns": [],
                "compatibility_context_indices": 0,
            }]),
            ("atom.terms", "facts", [{
                "relation": "seen", "terms": "ab",
            }]),
            ("rule.body", "rules", [{
                "head": {"relation": "seen", "terms": []}, "body": {},
            }]),
            ("aggregation.group_by", "rules", [{
                "head": {"relation": "seen", "terms": []}, "body": [],
                "aggregation": {"name": "count", "relation": "seen",
                                "group_by": "ab", "value_variable": "v"},
            }]),
            ("claim.terms", "claims", [{
                "id": "claim", "relation": "seen", "terms": {},
            }]),
            ("evidence.terms", "evidence", [{
                "id": "evidence", "relation": "seen", "terms": False,
            }]),
            ("evidence.depends_on", "evidence", [{
                "id": "evidence", "relation": "seen", "terms": [],
                "depends_on": "ab",
            }]),
            ("mapping.context_indices", "mappings", [{
                "claim_relation": "seen", "evidence_relation": "seen",
                "effect": "support", "context_indices": "ab",
            }]),
            ("mapping.bindings", "mappings", [{
                "claim_relation": "seen", "evidence_relation": "seen",
                "effect": "support", "bindings": {},
            }]),
            ("diagnostic.context_indices", "diagnostics", [{
                "claim_id": "claim", "trigger_relation": "seen",
                "effect": "observation", "context_indices": "ab",
            }]),
            ("output.requires_all_evidence", "outputs", [{
                "kind": "missing_premise", "claim_id": "claim",
                "requires_all_evidence": "ab",
            }]),
            ("output.requires_any_evidence", "outputs", [{
                "kind": "missing_premise", "claim_id": "claim",
                "requires_any_evidence": {},
            }]),
            ("output.excludes_evidence", "outputs", [{
                "kind": "missing_premise", "claim_id": "claim",
                "excludes_evidence": False,
            }]),
        )
        for name, field, value in nested_cases:
            raw = {
                "schema_version": 1,
                "relations": [{"name": "seen", "columns": []}],
                "facts": [{"relation": "seen", "terms": []}],
                "claims": [{"id": "claim", "relation": "seen",
                            "terms": []}],
                "evidence": [{"id": "evidence", "relation": "seen",
                              "terms": []}],
            }
            raw[field] = value
            with self.subTest(case=name):
                self.assert_named_invalid_input(raw)

        for field, value in (("relations", ""), ("claims", {}),
                             ("facts", False), ("rules", 0),
                             ("evidence", None), ("mappings", ""),
                             ("diagnostics", {}), ("outputs", False)):
            with self.subTest(case=f"bundle.{field}"):
                self.assert_named_invalid_input({
                    "schema_version": 1, field: value,
                })

    def test_absent_empty_wire_and_canonical_object_forms_are_valid(self):
        absent = bundle_from_json({"schema_version": 1}, validate=True)
        self.assertEqual(absent, Bundle(()))

        wire = {
            "schema_version": 1,
            "relations": [{"name": "seen", "columns": []}],
            "facts": [{"relation": "seen", "terms": []}],
            "claims": [{"id": "claim", "relation": "seen",
                        "terms": [], "context": {}}],
            "evidence": [{"id": "evidence", "relation": "seen",
                          "terms": [], "context": {}, "source": "test"}],
            "metadata": {},
            "diagnostic_policy": {},
        }
        parsed = bundle_from_json(wire, validate=True)
        canonical = json.loads(canonical_json(parsed))
        reloaded = bundle_from_json(canonical, validate=True)
        self.assertEqual(digest(parsed), digest(reloaded))
        self.assertEqual(reloaded.claims[0].context, Context())
        self.assertEqual(reloaded.evidence[0].context, Context())
        self.assertEqual(reloaded.metadata, ())

    def test_nonempty_contexts_have_one_injective_wire_and_canonical_form(self):
        # A context key literally named "values" is legal.  The one reserved
        # shape is the retired schema-v1 wrapper: sole key "values" holding a
        # list of string-keyed pairs (including the empty list).  That shape is
        # rejected by ingestion rather than reinterpreted, so it is covered by
        # BoundaryTotalityRegressionTests instead of asserted here.
        for value in ("producer-context", [1, 2], [["a"]]):
            value_type = ("symbol" if isinstance(value, str)
                          else "json-metadata-only")
            raw = {
                "schema_version": 1,
                "relations": [{
                    "name": "seen",
                    "columns": [{"name": "values",
                                 "type": value_type,
                                 "context": True}],
                    "context_indices": ["values"],
                }],
                "facts": [{
                    "relation": "seen",
                    "terms": [{"value": value,
                               "type": value_type}],
                }],
                "claims": [{
                    "id": "claim", "relation": "seen",
                    "terms": [{"value": value,
                               "type": value_type}],
                    "context": {"values": value},
                }],
                "evidence": [{
                    "id": "evidence", "relation": "seen",
                    "terms": [{"value": value,
                               "type": value_type}],
                    "context": {"values": value}, "source": "producer",
                }],
            }
            validation_modes = ((False, True)
                                if isinstance(value, str) else (False,))
            for validate in validation_modes:
                for source_name, source in (
                        ("mapping", copy.deepcopy(raw)),
                        ("json", json.dumps(raw))):
                    with self.subTest(value=value, validate=validate,
                                      source=source_name):
                        parsed = bundle_from_json(source, validate=validate)
                        expected = Context.from_mapping({"values": value})
                        self.assertEqual(parsed.claims[0].context, expected)
                        self.assertEqual(parsed.evidence[0].context, expected)
                        canonical = json.loads(canonical_json(parsed))
                        self.assertEqual(
                            canonical["claims"][0]["context"],
                            {"values": value})
                        self.assertEqual(
                            canonical["evidence"][0]["context"],
                            {"values": value})
                        reloaded = bundle_from_json(
                            canonical, validate=validate)
                        self.assertEqual(reloaded, parsed)
                        self.assertEqual(digest(reloaded), digest(parsed))

    def test_validation_defensively_reports_mutated_noncanonical_members(self):
        for field, code in (("relations", "relation-type"),
                            ("evidence", "evidence-type"),
                            ("outputs", "output-type")):
            with self.subTest(field=field):
                malformed = Bundle(())
                object.__setattr__(malformed, field, (object(),))
                self.assertIn(code, {issue.code
                                     for issue in validate_bundle(malformed)})

    def test_validation_reports_mutated_nested_ir_without_dereferencing_it(self):
        relation = rel("seen", "value")
        atom = Atom("seen", (Constant("v"),))
        evidence = Evidence("e", atom, source="producer")
        rule = Rule(atom, (atom,))
        claim = Claim("seen", (Constant("v"),), Context(), id="claim")
        diagnostic = DiagnosticRule(
            "seen", "observation", claim_id="claim")

        cases = (
            (evidence, "atom", object(), "atom-type"),
            (atom, "terms", object(), "atom-type"),
            (rule, "head", object(), "atom-type"),
            (rule, "body", object(), "rule-body"),
            (diagnostic, "predicate", (object(),), "diagnostic-predicate"),
        )
        for template, field, malformed, expected in cases:
            with self.subTest(field=f"{type(template).__name__}.{field}"):
                # Rebuild each graph because nested frozen records are shared.
                fact = Atom("seen", (Constant("v"),))
                record = Evidence("e", fact, source="producer")
                candidate_rule = Rule(fact, (fact,))
                candidate_diagnostic = DiagnosticRule(
                    "seen", "observation", claim_id="claim")
                target = {
                    Evidence: record,
                    Atom: fact,
                    Rule: candidate_rule,
                    DiagnosticRule: candidate_diagnostic,
                }[type(template)]
                bundle = Bundle(
                    (relation,), facts=(fact,), evidence=(record,),
                    rules=(candidate_rule,), claims=(claim,),
                    diagnostics=(candidate_diagnostic,))
                object.__setattr__(target, field, malformed)
                self.assertIn(
                    expected,
                    {issue.code for issue in validate_bundle(bundle)},
                )

    def test_validation_defensively_reports_mutated_diagnostic_scalars(self):
        for field, malformed in (("when_missing", "false"),
                                 ("required", 1),
                                 ("message", [])):
            with self.subTest(field=field):
                diagnostic = DiagnosticRule(
                    "seen", "observation", claim_id="claim")
                bundle = Bundle(
                    (rel("seen", "value"),),
                    claims=(Claim("seen", (Constant("v"),), id="claim"),),
                    diagnostics=(diagnostic,))
                object.__setattr__(diagnostic, field, malformed)
                self.assertIn(
                    "diagnostic-type",
                    {issue.code for issue in validate_bundle(bundle)},
                )

    def test_positive_rule_is_valid_and_head_variables_are_safe(self):
        bundle = Bundle((rel("seen", "tenant", "event"), rel("done", "tenant", "event")), rules=(
            Rule(Atom("done", (Variable("t"), Variable("e"))), (Atom("seen", (Variable("t"), Variable("e"))),)),))
        assert_valid(bundle)

    def test_negation_requires_completeness_premise(self):
        without = Bundle((rel("seen", "tenant", "event"), rel("done", "tenant", "event")), rules=(
            Rule(Atom("done", (Variable("t"), Variable("e"))), (Atom("seen", (Variable("t"), Variable("e")), negated=True),)),))
        self.assertIn("missing-completeness", {x.code for x in validate_bundle(without)})
        seen = RelationDecl("seen", (Column("tenant", TypeName.SYMBOL, context=True), Column("event", TypeName.SYMBOL)), context_indices=("tenant",))
        done = RelationDecl("done", (Column("tenant", TypeName.SYMBOL, context=True), Column("event", TypeName.SYMBOL)), context_indices=("tenant",))
        complete = RelationDecl("complete", (Column("tenant", TypeName.SYMBOL, context=True), Column("event", TypeName.SYMBOL)), modality=Modality.COMPLETENESS, completes="seen", context_indices=("tenant",))
        with_ = Bundle((seen, done, complete), rules=(
            Rule(Atom("done", (Variable("t"), Variable("e"))), (Atom("complete", (Variable("t"), Variable("e"))), Atom("seen", (Variable("t"), Variable("e")), negated=True),)),))
        self.assertFalse(validate_bundle(with_))

    def test_unsafe_variable_and_recursive_negation_are_rejected(self):
        unsafe = Bundle((rel("a", "x"), rel("b", "x")), rules=(Rule(Atom("b", (Variable("missing"),)), (Atom("a", (Variable("x"),)),)),))
        self.assertIn("unsafe-variable", {x.code for x in validate_bundle(unsafe)})
        recursive = Bundle((rel("a", "x"), rel("b", "x")), rules=(
            Rule(Atom("a", (Variable("x"),)), (Atom("b", (Variable("x"),)),)),
            Rule(Atom("b", (Variable("x"),)), (Atom("a", (Variable("x"),), negated=True),)),))
        self.assertIn("recursive-negation", {x.code for x in validate_bundle(recursive)})

    def test_aggregation_needs_domain_and_closure(self):
        from capcov.claims import Aggregation
        bundle = Bundle((rel("a", "x"), rel("b", "x")), rules=(Rule(Atom("b", (Variable("x"),)), (Atom("a", (Variable("x"),)),), aggregation=Aggregation("count_a", "a", ("x",), "x")),))
        self.assertTrue({x.code for x in validate_bundle(bundle)} >= {"aggregation-domain", "aggregation-closure"})


if __name__ == "__main__": unittest.main()


class BoundaryTotalityRegressionTests(unittest.TestCase):
    """Round-3 reviewer findings on kernel-closure-finalize (2026-09-15)."""

    def test_mutable_constant_value_is_rejected_by_bundle_construction(self):
        from capcov.claims.ir import Atom, Bundle, Column, Constant, RelationDecl
        constant = Constant(("a",))
        object.__setattr__(constant, "value", ["a"])  # bypass freezing
        decl = RelationDecl("r", (Column("x", "json-metadata-only"),))
        with self.assertRaises(TypeError):
            Bundle((decl,), (Atom("r", (constant,)),))

    def test_mutable_diagnostic_predicate_value_is_rejected(self):
        from capcov.claims.ir import Bundle, Column, DiagnosticRule, RelationDecl
        rule = DiagnosticRule("r", "forbidden", predicate=(("column", "x"), ("operator", "in"), ("value", ("a",))))
        object.__setattr__(rule, "predicate", (("column", "x"), ("operator", "in"), ("value", ["a"])))
        decl = RelationDecl("r", (Column("x", "symbol"),))
        with self.assertRaises(TypeError):
            Bundle((decl,), diagnostics=(rule,))

    def test_legacy_context_wrapper_bytes_are_rejected_not_reinterpreted(self):
        import json
        from capcov.claims.ir import BundleIngestionError, bundle_from_json
        legacy = {"schema_version": 1,
                  "relations": [{"name": "r", "columns": [{"name": "tenant", "type": "symbol", "context": True}],
                                 "context_indices": ["tenant"], "modality": "claim"}],
                  "claims": [{"id": "c", "relation": "r", "terms": [{"value": "t1"}],
                              "context": {"values": [["tenant", "t1"]]}}]}
        for validate in (False, True):
            with self.subTest(validate=validate):
                with self.assertRaises(BundleIngestionError) as caught:
                    bundle_from_json(json.dumps(legacy), validate=validate)
                self.assertIn("retired schema-v1 context wrapper", str(caught.exception))

    def test_genuine_values_context_key_round_trips(self):
        import json
        from capcov.claims.ir import bundle_from_json, canonical_json
        raw = {"schema_version": 1,
               "relations": [{"name": "r", "columns": [{"name": "values", "type": "symbol", "context": True}],
                              "context_indices": ["values"], "modality": "claim"}],
               "claims": [{"id": "c", "relation": "r", "terms": [{"value": "x"}], "context": {"values": "x"}}]}
        bundle = bundle_from_json(json.dumps(raw), validate=True)
        self.assertEqual(bundle.claims[0].context.as_dict(), {"values": "x"})
        again = bundle_from_json(canonical_json(bundle), validate=True)
        self.assertEqual(canonical_json(again), canonical_json(bundle))
