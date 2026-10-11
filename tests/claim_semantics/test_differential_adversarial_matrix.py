from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
import shutil
import tempfile
import unittest

from capcov.claims import (
    Atom,
    Claim,
    Constant,
    Context,
    DiagnosticPolicy,
    DiagnosticRule,
    Evidence,
    EvidenceMapping,
    OutputTemplate,
    Rule,
    TemplateValue,
    TypeName,
    Variable,
    bundle_from_json,
    digest,
)
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.differential import (
    COMPARABLE_CLAIM_FIELDS,
    DifferentialMismatch,
    compare,
    run_python,
    run_souffle,
)
from capcov.claims.evaluator import ResourceLimits
from capcov.claims.shrinker import _difference_shape

from . import test_differential_kernels as differential_cases
from . import test_kernel_closure as kernel_cases


@unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
class AdversarialDifferentialMatrixTests(unittest.TestCase):
    """Central registry for every section-27 kernel adversary."""

    @staticmethod
    def typed_bundle(kind):
        factory = kernel_cases.KernelClosureTests._mixed_literal_bundle
        if kind == "valid":
            return factory(
                TypeName.DIGEST,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
            )
        if kind == "wrong-index-type":
            return factory(
                TypeName.SYMBOL,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.SYMBOL),
                Constant("run-a", TypeName.SYMBOL),
            )
        if kind == "wrong-run-type":
            return factory(
                TypeName.DIGEST,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.DIGEST),
                witness_run_type=TypeName.DIGEST,
            )
        if kind == "untyped-witness":
            return factory(
                TypeName.DIGEST,
                Constant("index-a"), Constant("run-a"),
                Constant("index-a"), Constant("run-a"),
            )
        raise AssertionError(kind)

    @staticmethod
    def runtime_compatibility_bundle(*, typed, swapped=False):
        """Exercise the generic same-binding context compatibility path."""
        relation = kernel_cases.relation
        target_type = TypeName.DIGEST
        witness_type = target_type if typed else TypeName.SYMBOL

        def constant(value, type_name):
            return Constant(value, type_name) if typed else Constant(value)

        left = relation(
            "runtime_left", ("scope", target_type, True),
            ("value", TypeName.SYMBOL, False), context_indices=("scope",))
        right = relation(
            "runtime_right", ("scope", target_type, True),
            ("value", TypeName.SYMBOL, False), context_indices=("scope",))
        compatible = relation(
            "runtime_scopes_compatible",
            ("left_scope", witness_type, True),
            ("right_scope", witness_type, True),
            modality="compatibility",
            context_indices=("left_scope", "right_scope"),
            compatibility_targets=("runtime_left", "runtime_right"),
            compatibility_context_indices=("left_scope", "right_scope"),
        )
        joined = relation(
            "runtime_joined", ("value", TypeName.SYMBOL, False),
            modality="claim", primitive=False)
        left_scope = constant("scope-a", target_type)
        right_scope = constant("scope-b", target_type)
        witness_left = constant(
            "scope-b" if swapped else "scope-a", witness_type)
        witness_right = constant(
            "scope-a" if swapped else "scope-b", witness_type)
        value = Constant("v", TypeName.SYMBOL)
        left_atom = Atom("runtime_left", (left_scope, value))
        right_atom = Atom("runtime_right", (right_scope, value))
        witness_atom = Atom(
            "runtime_scopes_compatible", (witness_left, witness_right))
        return Bundle(
            (left, right, compatible, joined),
            facts=(left_atom, right_atom, witness_atom),
            evidence=(
                Evidence("runtime-left", left_atom,
                         Context.from_mapping({"scope": "scope-a"}),
                         source="reviewed"),
                Evidence("runtime-right", right_atom,
                         Context.from_mapping({"scope": "scope-b"}),
                         source="reviewed"),
                Evidence("runtime-compatibility", witness_atom,
                         Context.from_mapping({
                             "left_scope": witness_left.value,
                             "right_scope": witness_right.value}),
                         source="reviewed"),
            ),
            rules=(Rule(
                Atom("runtime_joined", (Variable("value"),)),
                (Atom("runtime_left", (left_scope, Variable("value"))),
                 Atom("runtime_right", (right_scope, Variable("value"))),
                 Atom("runtime_scopes_compatible",
                      (witness_left, witness_right))),
                "runtime-compatible"),),
            claims=(Claim("runtime_joined", (value,), id="runtime-claim"),),
        )

    @staticmethod
    def multidimensional_runtime_bundle(witness_values):
        """A schema-v1 generic witness cannot identify two dimensions."""
        relation = kernel_cases.relation
        left = relation(
            "multi_left", ("tenant", TypeName.SYMBOL, True),
            ("run", TypeName.SYMBOL, True),
            ("value", TypeName.SYMBOL, False),
            context_indices=("tenant", "run"))
        right = relation(
            "multi_right", ("tenant", TypeName.SYMBOL, True),
            ("run", TypeName.SYMBOL, True),
            ("value", TypeName.SYMBOL, False),
            context_indices=("tenant", "run"))
        compatible = relation(
            "multi_compatible",
            ("left_value", TypeName.SYMBOL, True),
            ("right_value", TypeName.SYMBOL, True),
            modality="compatibility",
            context_indices=("left_value", "right_value"),
            compatibility_targets=("multi_left", "multi_right"),
            compatibility_context_indices=("left_value", "right_value"))
        joined = relation(
            "multi_joined", ("value", TypeName.SYMBOL, False),
            modality="claim", primitive=False)
        a = Constant("a", TypeName.SYMBOL)
        b = Constant("b", TypeName.SYMBOL)
        value = Constant("v", TypeName.SYMBOL)
        left_atom = Atom("multi_left", (a, a, value))
        right_atom = Atom("multi_right", (b, b, value))
        witness_atom = Atom(
            "multi_compatible",
            tuple(Constant(item, TypeName.SYMBOL) for item in witness_values))
        return Bundle(
            (left, right, compatible, joined),
            facts=(left_atom, right_atom, witness_atom),
            evidence=(
                Evidence("multi-left", left_atom,
                         Context.from_mapping({"tenant": "a", "run": "a"}),
                         source="reviewed"),
                Evidence("multi-right", right_atom,
                         Context.from_mapping({"tenant": "b", "run": "b"}),
                         source="reviewed"),
                Evidence("multi-witness", witness_atom,
                         Context.from_mapping({
                             "left_value": witness_values[0],
                             "right_value": witness_values[1]}),
                         source="reviewed"),
            ),
            rules=(Rule(
                Atom("multi_joined", (Variable("value"),)),
                (Atom("multi_left", (a, a, Variable("value"))),
                 Atom("multi_right", (b, b, Variable("value"))),
                 Atom("multi_compatible", witness_atom.terms)),
                "ambiguous-multidimensional-join"),),
            claims=(Claim("multi_joined", (value,), id="multi-claim"),),
        )

    @staticmethod
    def malformed_bundle(kind):
        """Canonical semantic-invalid inputs that can be replayed byte-for-byte."""
        relation = kernel_cases.relation
        if kind == "relation":
            return Bundle((relation(
                "9invalid_relation", ("value", TypeName.SYMBOL, False)),))

        observed = relation(
            "malformed_observed", ("value", TypeName.SYMBOL, False))
        atom = Atom(
            "malformed_observed", (Constant("v", TypeName.SYMBOL),))
        source = "" if kind == "evidence" else "reviewed"
        evidence = Evidence("malformed-evidence", atom, source=source)
        if kind == "evidence":
            return Bundle((observed,), facts=(atom,), evidence=(evidence,))
        if kind == "output":
            output = OutputTemplate(
                "observed", "malformed-claim",
                evidence_id="malformed-evidence",
                fields=(("bad", TemplateValue(source="not-a-source")),))
            return Bundle(
                (observed,), facts=(atom,), evidence=(evidence,),
                claims=(Claim(
                    "malformed_observed", (Constant("v", TypeName.SYMBOL),),
                    id="malformed-claim"),), outputs=(output,))
        raise AssertionError(kind)

    @staticmethod
    def deep_rule_bundle(depth=300):
        """A valid acyclic graph beyond the validation dependency bound."""
        relation = kernel_cases.relation
        declarations = tuple(
            relation(
                f"deep_{index:03d}", ("value", TypeName.SYMBOL, False),
                modality="claim" if index == 0 else "derived",
                primitive=index == depth - 1,
            )
            for index in range(depth)
        )
        rules = tuple(
            Rule(
                Atom(f"deep_{index:03d}", (Variable("value"),)),
                (Atom(f"deep_{index + 1:03d}", (Variable("value"),)),),
                f"deep-rule-{index:03d}",
            )
            for index in range(depth - 1)
        )
        leaf = Atom(
            f"deep_{depth - 1:03d}",
            (Constant("v", TypeName.SYMBOL),))
        return Bundle(
            declarations, facts=(leaf,),
            evidence=(Evidence("deep-leaf", leaf, source="reviewed"),),
            rules=rules,
            claims=(Claim(
                "deep_000", (Constant("v", TypeName.SYMBOL),),
                id="deep-claim"),),
        )

    @staticmethod
    def malformed_nested_bundle(kind):
        """Attempt to build a noncanonical recursive IR graph."""
        relation = kernel_cases.relation
        observed = relation(
            "nested_observed", ("value", TypeName.SYMBOL, False))
        valid_atom = Atom(
            "nested_observed", (Constant("v", TypeName.SYMBOL),))
        if kind == "evidence-atom":
            return Bundle(
                (observed,), evidence=(Evidence(
                    "nested-evidence", object(), source="producer"),))
        if kind == "atom-terms":
            return Bundle(
                (observed,), facts=(Atom("nested_observed", (object(),)),))
        if kind == "rule-head":
            return Bundle((observed,), rules=(Rule(
                object(), (valid_atom,), "malformed-head"),))
        if kind == "rule-body":
            return Bundle((observed,), rules=(Rule(
                valid_atom, (object(),), "malformed-body"),))
        if kind == "diagnostic-predicate":
            return Bundle((observed,), diagnostics=(DiagnosticRule(
                "nested_observed", "observation",
                predicate=(object(),)),))
        if kind in {"claim-context", "evidence-context"}:
            context = Context.from_mapping({"tenant": "t"})
            object.__setattr__(context, "values", (("tenant", []),))
            if kind == "claim-context":
                return Bundle((observed,), claims=(Claim(
                    "nested_observed", (Constant("v"),), context,
                    id="nested-claim"),))
            return Bundle(
                (observed,), facts=(valid_atom,), evidence=(Evidence(
                    "nested-evidence", valid_atom, context,
                    source="producer"),))
        if kind.startswith("diagnostic-policy-"):
            field = kind.removeprefix("diagnostic-policy-")
            policy = DiagnosticPolicy()
            object.__setattr__(policy, field, object())
            return Bundle((observed,), diagnostic_policy=policy)
        if kind == "template-value":
            template = TemplateValue(value={"nested": [1]})
            object.__setattr__(template, "value", [1])
            return Bundle((observed,), outputs=(OutputTemplate(
                "missing_premise", "nested-claim",
                fields=(("payload", template),)),))
        raise AssertionError(kind)

    @staticmethod
    def legacy_indexless_static_bundle():
        claimed = differential_cases.R(
            "legacy_claim", ("tenant", "symbol"), modality="claim")
        static = differential_cases.R(
            "static_route_exists", ("tenant", "symbol", True),
            ("surface", "symbol", True), binding="static",
            context_indices=("tenant", "surface"))
        return Bundle(
            (claimed, static),
            claims=(Claim(
                "legacy_claim", (Constant("t"),), id="legacy"),))

    @staticmethod
    def mapping_relation_decoy_bundle():
        actual = differential_cases.R(
            "actual_claim", ("x", "symbol"), ("event", "symbol"),
            modality="claim")
        decoy = differential_cases.R(
            "decoy_claim", ("x", "symbol"), modality="claim")
        observed = differential_cases.R(
            "observed_event", ("x", "symbol"), ("event", "symbol"))
        mapping = EvidenceMapping(
            "decoy_claim", "observed_event", "support",
            bindings=(("x", "x"),), claim_id="actual")
        return Bundle(
            (actual, decoy, observed),
            facts=(Atom("observed_event", (
                Constant("v"), Constant("event-b"))),),
            claims=(Claim(
                "actual_claim", (Constant("v"), Constant("event-a")),
                id="actual"),), mappings=(mapping,))

    def assert_complete_agreement(self, name, bundle):
        result = compare(bundle, shrink=False)
        self.assertTrue(result.matched, name)
        self.assertIsNone(result.python.operational_failure, name)
        self.assertIsNone(result.souffle.operational_failure, name)
        self.assertEqual(result.python.relations, result.souffle.relations, name)
        self.assertEqual(len(result.python.claims), len(result.souffle.claims), name)
        for left, right in zip(result.python.claims, result.souffle.claims):
            self.assertEqual((left.key, left.index), (right.key, right.index), name)
            for field in COMPARABLE_CLAIM_FIELDS:
                self.assertEqual(getattr(left, field), getattr(right, field),
                                 (name, field))
        return result

    def assert_blocking_boundary(self, name, bundle, expected_pair,
                                 python_runner=run_python,
                                 souffle_runner=run_souffle):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DifferentialMismatch, msg=name) as caught:
                compare(bundle, python_runner=python_runner,
                        souffle_runner=souffle_runner,
                        replay_root=directory, max_steps=1)
            result = caught.exception.result
            actual_pair = (result.python.operational_failure,
                           result.souffle.operational_failure)
            self.assertEqual(actual_pair, expected_pair, name)
            self.assertEqual(
                _difference_shape(result.python, result.souffle),
                ("operational", *expected_pair), name)
            self.assertFalse(result.matched, name)
            self.assertTrue(result.replay_reproduced, name)
            replay = Path(result.replay_path)
            self.assertTrue(replay.is_file(), name)
            reloaded = bundle_from_json(
                replay.read_text(encoding="utf-8"), validate=False)
            self.assertEqual(digest(reloaded), digest(bundle), name)
            return result

    def test_valid_adversarial_bundle_matrix(self):
        kernel = kernel_cases.KernelClosureTests
        outputs = kernel_cases.VariableOutputRelevanceTests
        typed_without_witness = self.typed_bundle("valid")
        typed_without_witness = replace(
            typed_without_witness,
            facts=tuple(atom for atom in typed_without_witness.facts
                        if atom.relation != "index_describes_run"),
            evidence=tuple(record for record in typed_without_witness.evidence
                           if record.id != "compatibility-witness"),
        )
        cases = {
            "base-plus-self-cycle": kernel.self_cycle_bundle(),
            "late-shorter-proof": kernel.late_shorter_bundle(),
            "variable-output-exists": outputs.bundle("exists"),
            "variable-output-forall": outputs.bundle("forall"),
            "repeated-variable-and-scope": outputs.repeated_and_scoped_bundle(),
            "joint-trigger-incompatible": outputs.joint_trigger_bundle("b"),
            "joint-trigger-compatible": outputs.joint_trigger_bundle("a"),
            "failed-mapping-diagnostic-relevance":
                outputs.failed_mapping_relevance_bundle("b", "diagnostic"),
            "same-mapping-diagnostic-relevance":
                outputs.failed_mapping_relevance_bundle("a", "diagnostic"),
            "failed-mapping-proof-relevance":
                outputs.failed_mapping_relevance_bundle("b", "proof"),
            "same-mapping-proof-relevance":
                outputs.failed_mapping_relevance_bundle("a", "proof"),
            "json-canonical-proof": kernel.json_proof_bundle(reverse=True),
            "incompatible-exclusion-exists":
                outputs.exclusion_bundle("b", "exists"),
            "same-binding-exclusion-exists":
                outputs.exclusion_bundle("a", "exists"),
            "incompatible-exclusion-forall":
                outputs.exclusion_bundle("b", "forall"),
            "same-binding-exclusion-forall":
                outputs.exclusion_bundle("a", "forall"),
            "typed-compatibility-positive": self.typed_bundle("valid"),
            "typed-compatibility-witness-absent": typed_without_witness,
            "typed-runtime-context-compatibility":
                self.runtime_compatibility_bundle(typed=True),
        }
        self.assertEqual(set(cases), {
            "base-plus-self-cycle", "late-shorter-proof",
            "variable-output-exists", "variable-output-forall",
            "repeated-variable-and-scope", "joint-trigger-incompatible",
            "joint-trigger-compatible",
            "failed-mapping-diagnostic-relevance",
            "same-mapping-diagnostic-relevance",
            "failed-mapping-proof-relevance",
            "same-mapping-proof-relevance", "json-canonical-proof",
            "incompatible-exclusion-exists", "same-binding-exclusion-exists",
            "incompatible-exclusion-forall", "same-binding-exclusion-forall",
            "typed-compatibility-positive", "typed-compatibility-witness-absent",
            "typed-runtime-context-compatibility",
        })
        for name, bundle in cases.items():
            with self.subTest(case=name):
                result = self.assert_complete_agreement(name, bundle)
                if name == "typed-compatibility-positive":
                    self.assertEqual(dict(result.python.relations)["joined"],
                                     (("v",),))
                    self.assertEqual(
                        tuple(getattr(result.python.claims[0], field)
                              for field in COMPARABLE_CLAIM_FIELDS),
                        ("supported", "complete", "derivational", ()))
                elif name == "typed-compatibility-witness-absent":
                    self.assertEqual(dict(result.python.relations)["joined"], ())
                    self.assertEqual(result.python.claims[0].semantic,
                                     "unresolved")
                elif name == "typed-runtime-context-compatibility":
                    self.assertEqual(
                        dict(result.python.relations)["runtime_joined"],
                        (("v",),))
                    self.assertEqual(result.python.claims[0].semantic,
                                     "supported")
                elif name == "failed-mapping-diagnostic-relevance":
                    self.assertEqual(
                        result.python.claims[0].missing_premises,
                        ('"claim:mapped_pair_claimed:{}"',))
                elif name == "same-mapping-diagnostic-relevance":
                    self.assertEqual(
                        result.python.claims[0].missing_premises,
                        ('{"reason":"mapped instance","relation":"mapped_pair_claimed"}',))

    def test_invalid_and_exhausted_adversarial_bundle_matrix(self):
        mixed_binding_invalid = {
            "mixed-binding-declaration-only":
                differential_cases.mixed_mapping_bundle(
                    include_witness=False),
            "mixed-binding-wrong-targets":
                differential_cases.mixed_mapping_bundle(
                    witness_targets=("static_seen", "decoy_seen")),
            "mixed-binding-wrong-witness-payload":
                differential_cases.mixed_mapping_bundle(
                    witness_payload=("index",)),
            "mixed-binding-static-index-unbound":
                differential_cases.mixed_mapping_bundle(
                    static_bindings=(("value", "value"),)),
            "mixed-binding-runtime-run-unbound":
                differential_cases.mixed_mapping_bundle(
                    runtime_bindings=(("value", "value"),)),
            "mixed-binding-witness-index-unbound":
                differential_cases.mixed_mapping_bundle(
                    witness_bindings=(("run", "run"),)),
            "mixed-binding-witness-run-unbound":
                differential_cases.mixed_mapping_bundle(
                    witness_bindings=(("index", "index"),)),
            "mixed-binding-swapped-witness-bindings":
                differential_cases.mixed_mapping_bundle(
                    witness_bindings=(
                        ("index", "run"), ("run", "index"))),
            "mixed-binding-observation-only-witness":
                differential_cases.mixed_mapping_bundle(
                    witness_effect="observation"),
        }
        invalid = {
            "forall-context-alias":
                kernel_cases.KernelClosureTests.forall_context_alias_bundle(),
            "typed-witness-wrong-index": self.typed_bundle("wrong-index-type"),
            "typed-witness-wrong-run": self.typed_bundle("wrong-run-type"),
            "typed-witness-untyped": self.typed_bundle("untyped-witness"),
            "runtime-context-witness-wrong-types":
                self.runtime_compatibility_bundle(typed=False),
            "runtime-context-witness-swapped":
                self.runtime_compatibility_bundle(typed=True, swapped=True),
            "multidimensional-tenant-only-witness":
                self.multidimensional_runtime_bundle(("a", "b")),
            "multidimensional-swapped-witness":
                self.multidimensional_runtime_bundle(("b", "a")),
            "multidimensional-duplicated-witness":
                self.multidimensional_runtime_bundle(("a", "a")),
            "malformed-relation-declaration": self.malformed_bundle("relation"),
            "malformed-evidence-record": self.malformed_bundle("evidence"),
            "malformed-output-field": self.malformed_bundle("output"),
            "legacy-indexless-static":
                self.legacy_indexless_static_bundle(),
            "mapping-relation-decoy":
                self.mapping_relation_decoy_bundle(),
            **mixed_binding_invalid,
        }
        expected_invalid = {
            "forall-context-alias", "typed-witness-wrong-index",
            "typed-witness-wrong-run", "typed-witness-untyped",
            "runtime-context-witness-wrong-types",
            "runtime-context-witness-swapped",
            "multidimensional-tenant-only-witness",
            "multidimensional-swapped-witness",
            "multidimensional-duplicated-witness",
            "malformed-relation-declaration", "malformed-evidence-record",
            "malformed-output-field",
            "legacy-indexless-static", "mapping-relation-decoy",
            *mixed_binding_invalid,
        }
        self.assertEqual(set(invalid), expected_invalid)
        for name, bundle in invalid.items():
            with self.subTest(case=name):
                self.assert_blocking_boundary(
                    name, bundle, ("invalid-input", "invalid-input"))

        def raises_recursion(_):
            raise RecursionError("injected runner recursion")

        bounded = (
            (
                "python-iteration-exhaustion",
                kernel_cases.KernelClosureTests.self_cycle_bundle(),
                ("resource-exhausted", None),
                partial(run_python, limits=ResourceLimits(max_iterations=0)),
                run_souffle,
            ),
            (
                "souffle-row-exhaustion",
                kernel_cases.KernelClosureTests.self_cycle_bundle(),
                (None, "resource-exhausted"),
                run_python,
                partial(run_souffle, max_rows=0),
            ),
            (
                "alternative-provenance-exhaustion",
                differential_cases.alternative_overflow_bundle(),
                ("resource-exhausted", None),
                run_python,
                run_souffle,
            ),
            (
                "validation-depth-exhaustion",
                self.deep_rule_bundle(),
                ("resource-exhausted", "resource-exhausted"),
                run_python,
                run_souffle,
            ),
            (
                "injected-runner-recursion-exhaustion",
                kernel_cases.KernelClosureTests.self_cycle_bundle(),
                ("resource-exhausted", "resource-exhausted"),
                raises_recursion,
                raises_recursion,
            ),
        )
        self.assertEqual({case[0] for case in bounded}, {
            "python-iteration-exhaustion", "souffle-row-exhaustion",
            "alternative-provenance-exhaustion", "validation-depth-exhaustion",
            "injected-runner-recursion-exhaustion",
        })
        for name, bundle, pair, python_runner, souffle_runner in bounded:
            with self.subTest(case=name):
                self.assert_blocking_boundary(
                    name, bundle, pair, python_runner, souffle_runner)

    def test_malformed_nested_ir_is_rejected_at_bundle_construction(self):
        # Structurally noncanonical graphs never become Bundles, so every actual
        # Bundle in this module remains in the valid or blocking compare matrix.
        names = {
            "evidence-atom", "atom-terms", "rule-head", "rule-body",
            "diagnostic-predicate", "claim-context", "evidence-context",
            "diagnostic-policy-missing_premises",
            "diagnostic-policy-inconsistent_premises",
            "diagnostic-policy-out_of_scope",
            "diagnostic-policy-forbidden_evidence",
            "diagnostic-policy-revocation",
            "diagnostic-policy-completeness", "template-value",
        }
        for name in names:
            with self.subTest(case=name), self.assertRaises(TypeError):
                self.malformed_nested_bundle(name)

    def test_actual_identical_invalid_failures_still_block_with_asymmetric_payloads(self):
        bundle = kernel_cases.KernelClosureTests.forall_context_alias_bundle()
        left, right = run_python(bundle), run_souffle(bundle)
        self.assertEqual((left.operational_failure, right.operational_failure),
                         ("invalid-input", "invalid-input"))
        self.assertEqual(len(left.claims), 1)
        self.assertEqual(right.claims, ())
        self.assertNotEqual(left.relations, right.relations)
        result = self.assert_blocking_boundary(
            "identical-invalid-failures", bundle,
            ("invalid-input", "invalid-input"))
        self.assertTrue(result.replay_reproduced)


if __name__ == "__main__":
    unittest.main()
