from __future__ import annotations

from dataclasses import replace
import shutil
import unittest

from capcov.claims import (
    Atom, Claim, Column, Constant, Context, DiagnosticRule, Evidence,
    EvidenceMapping, OutputTemplate, RelationDecl, Rule, TemplateValue,
    TypeName, Variable, bundle_from_json, canonical_json, validate_bundle,
)
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.differential import compare, run_python, run_souffle
from capcov.claims.evaluator import ResourceLimits, evaluate
from capcov.claims.output import VerifiedProofEvidence, render_outputs


def relation(name, *columns, **kwargs):
    return RelationDecl(
        name, tuple(Column(column, type_name, context) for column, type_name, context in columns),
        **kwargs,
    )


def issue_codes(bundle):
    return {issue.code for issue in validate_bundle(bundle)}


class KernelClosureTests(unittest.TestCase):
    @staticmethod
    def self_cycle_bundle():
        seed = relation("seed", ("value", TypeName.SYMBOL, False))
        reachable = relation(
            "reachable", ("value", TypeName.SYMBOL, False),
            modality="claim", primitive=False,
        )
        seed_atom = Atom("seed", (Constant("v", TypeName.SYMBOL),))
        return Bundle(
            (seed, reachable),
            facts=(seed_atom,),
            evidence=(Evidence("base-leaf", seed_atom, source="reviewed"),),
            # Canonical rule order puts the self-cycle before the seed rule.
            rules=(
                Rule(Atom("reachable", (Variable("value"),)),
                     (Atom("reachable", (Variable("value"),)),), "a-self-cycle"),
                Rule(Atom("reachable", (Variable("value"),)),
                     (Atom("seed", (Variable("value"),)),), "z-base"),
            ),
            claims=(Claim("reachable", (Constant("v", TypeName.SYMBOL),),
                          id="claim"),),
        )

    def test_positive_self_cycle_keeps_the_shallow_base_proof(self):
        bundle = self.self_cycle_bundle()
        first = evaluate(bundle)
        second = evaluate(bundle)
        self.assertEqual(first.status.value, "complete")
        self.assertEqual(first.as_dict(), second.as_dict())
        proofs = dict(dict(first.provenance)["reachable"])[("v",)]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0].rule, "z-base")
        self.assertEqual(proofs[0].depth, 1)
        self.assertEqual(first.claims[0].result.support, ("base-leaf",))
        self.assertEqual(dict(first.resources)["provenance_nodes"], 2)

    @staticmethod
    def late_shorter_bundle():
        one = (("value", TypeName.SYMBOL, False),)
        declarations = tuple(
            relation(name, *one, primitive=name in {"seed_deep", "seed_short"})
            for name in ("a_long", "b_longer", "m_mid", "n_out",
                         "seed_deep", "seed_short", "z_short")
        )
        deep = Atom("seed_deep", (Constant("v", TypeName.SYMBOL),))
        short = Atom("seed_short", (Constant("v", TypeName.SYMBOL),))
        return Bundle(
            declarations,
            facts=(deep, short),
            evidence=(Evidence("leaf-deep", deep, source="reviewed"),
                      Evidence("leaf-short", short, source="reviewed")),
            # Head names force the long route and its downstream snapshot to
            # arrive before z_short.  The next pass must improve n_out's same
            # ground path when m_mid acquires the shallower route.
            rules=(
                Rule(Atom("a_long", (Variable("x"),)),
                     (Atom("seed_deep", (Variable("x"),)),), "long-1"),
                Rule(Atom("b_longer", (Variable("x"),)),
                     (Atom("a_long", (Variable("x"),)),), "long-2"),
                Rule(Atom("m_mid", (Variable("x"),)),
                     (Atom("b_longer", (Variable("x"),)),), "mid-deep"),
                Rule(Atom("m_mid", (Variable("x"),)),
                     (Atom("z_short", (Variable("x"),)),), "mid-short"),
                Rule(Atom("n_out", (Variable("x"),)),
                     (Atom("m_mid", (Variable("x"),)),), "downstream"),
                Rule(Atom("z_short", (Variable("x"),)),
                     (Atom("seed_short", (Variable("x"),)),), "short"),
            ),
            claims=(Claim("n_out", (Constant("v", TypeName.SYMBOL),),
                          id="claim"),),
        )

    def test_late_shorter_child_proof_propagates_to_same_downstream_path(self):
        report = evaluate(self.late_shorter_bundle())
        self.assertEqual(report.status.value, "complete")
        self.assertEqual(report.claims[0].result.support, ("leaf-short",))
        mid_proofs = dict(dict(report.provenance)["m_mid"])[("v",)]
        self.assertEqual(mid_proofs[0].rule, "mid-short")
        out_proof = dict(dict(report.provenance)["n_out"])[("v",)][0]
        self.assertEqual(out_proof.leaves, ("leaf-short",))
        self.assertEqual(out_proof.depth, 3)

    @staticmethod
    def json_proof_bundle(reverse=False):
        metadata = relation(
            "json_source", ("payload", TypeName.JSON_METADATA_ONLY, False))
        chosen = relation(
            "json_chosen", ("value", TypeName.SYMBOL, False),
            modality="claim", primitive=False)
        payloads = ({"a": [1, {"stable": True}]},
                    {"z": [1, {"stable": True}]})
        if reverse:
            payloads = tuple(reversed(payloads))
        atoms = tuple(Atom(
            "json_source", (Constant(payload, TypeName.JSON_METADATA_ONLY),))
            for payload in payloads)
        evidence = tuple(Evidence(
            "leaf-a" if "a" in payload else "leaf-z", atom,
            source="reviewed") for payload, atom in zip(payloads, atoms))
        rule = Rule(
            Atom("json_chosen", (Constant("v", TypeName.SYMBOL),)),
            (Atom("json_source", (Variable("payload"),)),),
            "same-json-rule")
        return Bundle(
            (metadata, chosen), facts=atoms, evidence=evidence,
            rules=(rule,), claims=(Claim(
                "json_chosen", (Constant("v", TypeName.SYMBOL),),
                id="json-claim"),))

    def test_json_proof_choice_is_canonical_across_construction_and_reload(self):
        constructed = self.json_proof_bundle(reverse=True)
        reloaded = bundle_from_json(canonical_json(constructed), validate=True)
        for bundle in (constructed, reloaded, self.json_proof_bundle()):
            with self.subTest(bundle=canonical_json(bundle)):
                report = evaluate(bundle)
                proofs = dict(dict(report.provenance)["json_chosen"])[("v",)]
                self.assertEqual(proofs[0].leaves, ("leaf-a",))
                self.assertEqual(report.claims[0].result.support, ("leaf-a",))

    @staticmethod
    def forall_context_alias_bundle():
        claimed = relation(
            "run_member_claim", ("run", TypeName.SYMBOL, True),
            ("member", TypeName.SYMBOL, False), modality="claim",
            context_indices=("run",))
        domain = relation(
            "run_member_domain", ("run", TypeName.SYMBOL, True),
            ("member", TypeName.SYMBOL, False), finite=True, nonempty=True,
            context_indices=("run",))
        closed = relation(
            "run_member_domain_closed", ("run", TypeName.SYMBOL, True),
            modality="completeness", completes="run_member_domain",
            context_indices=("run",))
        member = Atom(
            "run_member_domain",
            (Constant("run-b", TypeName.SYMBOL),
             Constant("member-b", TypeName.SYMBOL)))
        closure = Atom(
            "run_member_domain_closed",
            (Constant("run-b", TypeName.SYMBOL),))
        context = Context.from_mapping({"run": "run-b"})
        claim = Claim(
            "run_member_claim", (Variable("run"), Variable("member")),
            Context.from_mapping({"run": "run-a"}), "forall",
            "run_member_domain", id="claim")
        return Bundle(
            (claimed, domain, closed), facts=(member, closure),
            evidence=(Evidence("member-b", member, context, source="reviewed"),
                      Evidence("domain-run-b-closed", closure, context,
                               source="reviewed")),
            claims=(claim,))

    def test_forall_context_alias_is_rejected_by_both_kernel_boundaries(self):
        bundle = self.forall_context_alias_bundle()
        self.assertIn("claim-context", issue_codes(bundle))
        self.assertEqual(run_python(bundle).operational_failure, "invalid-input")
        self.assertEqual(run_souffle(bundle).operational_failure, "invalid-input")

    def test_cycle_bound_is_a_named_operational_result(self):
        seed = relation("seed", ("value", TypeName.SYMBOL, False))
        reachable = relation("reachable", ("value", TypeName.SYMBOL, False),
                             primitive=False)
        bundle = Bundle(
            (seed, reachable),
            facts=(Atom("seed", (Constant("v", TypeName.SYMBOL),)),),
            rules=(Rule(Atom("reachable", (Variable("value"),)),
                        (Atom("seed", (Variable("value"),)),), "base"),),
            claims=(Claim("reachable", (Constant("v", TypeName.SYMBOL),)),),
        )
        report = evaluate(bundle, ResourceLimits(max_iterations=0))
        self.assertEqual(report.status.value, "resource-exhausted")
        self.assertEqual(report.claims[0].operational.value, "resource-exhausted")
        self.assertIn("iteration limit", report.message)

    def test_mixed_witness_columns_must_match_both_target_types(self):
        mutants = (
            self._mixed_literal_bundle(
                TypeName.SYMBOL,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.SYMBOL),
                Constant("run-a", TypeName.SYMBOL),
            ),
            self._mixed_literal_bundle(
                TypeName.DIGEST,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.DIGEST),
                witness_run_type=TypeName.DIGEST,
            ),
        )
        for bundle in mutants:
            with self.subTest(bundle=bundle):
                self.assertIn("mixed-binding-join", issue_codes(bundle))

    def test_untyped_literals_cannot_forge_a_mixed_witness(self):
        untyped = self._mixed_literal_bundle(
            TypeName.DIGEST,
            Constant("index-a"), Constant("run-a"),
            Constant("index-a"), Constant("run-a"),
        )
        self.assertIn("mixed-binding-join", issue_codes(untyped))

        typed = self._mixed_literal_bundle(
            TypeName.DIGEST,
            Constant("index-a", TypeName.DIGEST),
            Constant("run-a", TypeName.SYMBOL),
            Constant("index-a", TypeName.DIGEST),
            Constant("run-a", TypeName.SYMBOL),
        )
        self.assertNotIn("mixed-binding-join", issue_codes(typed))

    @staticmethod
    def _mixed_literal_bundle(witness_index_type, static_index, runtime_run,
                              witness_index, witness_run,
                              witness_run_type=TypeName.SYMBOL):
        static = relation(
            "static_item", ("index", TypeName.DIGEST, True),
            ("value", TypeName.SYMBOL, False), binding="static",
            context_indices=("index",),
        )
        runtime = relation(
            "runtime_item", ("run", TypeName.SYMBOL, True),
            ("value", TypeName.SYMBOL, False), context_indices=("run",),
        )
        compatible = relation(
            "index_describes_run", ("index", witness_index_type, True),
            ("run", witness_run_type, True), modality="compatibility",
            context_indices=("index", "run"),
            compatibility_targets=("static_item", "runtime_item"),
            compatibility_context_indices=("index", "run"),
        )
        joined = relation("joined", ("value", TypeName.SYMBOL, False),
                          modality="claim", primitive=False)
        rule = Rule(
            Atom("joined", (Variable("value"),)),
            (Atom("static_item", (static_index, Variable("value"))),
             Atom("runtime_item", (runtime_run, Variable("value"))),
             Atom("index_describes_run", (witness_index, witness_run))),
            "mixed",
        )
        value = Constant("v", TypeName.SYMBOL)
        static_atom = Atom("static_item", (static_index, value))
        runtime_atom = Atom("runtime_item", (runtime_run, value))
        witness_atom = Atom(
            "index_describes_run", (witness_index, witness_run))
        facts = (static_atom, runtime_atom, witness_atom)
        evidence = (
            Evidence("static-item", static_atom,
                     Context.from_mapping({"index": static_index.value}),
                     source="reviewed"),
            Evidence("runtime-item", runtime_atom,
                     Context.from_mapping({"run": runtime_run.value}),
                     source="reviewed"),
            Evidence("compatibility-witness", witness_atom,
                     Context.from_mapping({"index": witness_index.value,
                                           "run": witness_run.value}),
                     source="reviewed"),
        )
        return Bundle(
            (static, runtime, compatible, joined), facts=facts,
            evidence=evidence, rules=(rule,),
            claims=(Claim("joined", (value,), id="joined-claim"),))


class VariableOutputRelevanceTests(unittest.TestCase):
    @staticmethod
    def bundle(quantifier="exists"):
        observed = relation("member_observed", ("member", TypeName.SYMBOL, False))
        claimed = relation("member_claimed", ("member", TypeName.SYMBOL, False),
                           modality="claim")
        domain = relation("member_domain", ("member", TypeName.SYMBOL, False),
                          finite=True, nonempty=True)
        closed = relation("member_domain_closed", modality="completeness",
                          completes="member_domain")
        observed_atom = Atom(
            "member_observed", (Constant("a", TypeName.SYMBOL),))
        domain_atom = Atom("member_domain", (Constant("a", TypeName.SYMBOL),))
        closure_atom = Atom("member_domain_closed", ())
        evidence = (
            Evidence("observed-a", observed_atom, source="producer"),
            Evidence("domain-a", domain_atom, source="review"),
            Evidence("domain-closed", closure_atom, source="review"),
        )
        claim = Claim(
            "member_claimed", (Variable("member"),), quantifier=quantifier,
            domain="member_domain" if quantifier == "forall" else None,
            id="claim",
        )
        mapping = EvidenceMapping(
            "member_claimed", "member_observed", "observation",
            bindings=(("member", "member"),), claim_id="claim",
        )
        output = OutputTemplate(
            "missing_premise", "claim", relation="member_observed",
            fields=(
                ("member", TemplateValue(
                    source="evidence", column="member", type=TypeName.SYMBOL,
                    evidence_id="observed-a")),
                ("reason", TemplateValue(
                    value="reviewed variable-valued premise")),
            ),
            requires_all_evidence=("observed-a",),
            when_claim="underived",
        )
        return Bundle(
            (observed, claimed, domain, closed),
            facts=(observed_atom, domain_atom, closure_atom), evidence=evidence,
            claims=(claim,), mappings=(mapping,), outputs=(output,),
        )

    @staticmethod
    def joint_trigger_bundle(right_value="b"):
        left = relation("left_observed", ("value", TypeName.SYMBOL, False))
        right = relation("right_observed", ("value", TypeName.SYMBOL, False))
        claimed = relation(
            "same_pair_claimed", ("left", TypeName.SYMBOL, False),
            ("right", TypeName.SYMBOL, False), modality="claim")
        left_atom = Atom(
            "left_observed", (Constant("a", TypeName.SYMBOL),))
        right_atom = Atom(
            "right_observed", (Constant(right_value, TypeName.SYMBOL),))
        right_id = f"right-{right_value}"
        claim = Claim(
            "same_pair_claimed", (Variable("same"), Variable("same")),
            id="joint-claim")
        mappings = (
            EvidenceMapping(
                "same_pair_claimed", "left_observed", "observation",
                bindings=(("left", "value"),), claim_id="joint-claim"),
            EvidenceMapping(
                "same_pair_claimed", "right_observed", "observation",
                bindings=(("right", "value"),), claim_id="joint-claim"),
        )
        output = OutputTemplate(
            "missing_premise", "joint-claim", relation="same_pair_claimed",
            fields=(("reason", TemplateValue(
                value="incompatible rows must not trigger")),),
            requires_all_evidence=("left-a", right_id),
            when_claim="underived")
        return Bundle(
            (left, right, claimed), facts=(left_atom, right_atom),
            evidence=(Evidence("left-a", left_atom, source="reviewed"),
                      Evidence(right_id, right_atom, source="reviewed")),
            claims=(claim,), mappings=mappings, outputs=(output,))

    @staticmethod
    def failed_mapping_relevance_bundle(value="b", relevance="diagnostic"):
        observed = relation(
            "mapped_pair_observed", ("left", TypeName.SYMBOL, False),
            ("right", TypeName.SYMBOL, False))
        claimed = relation(
            "mapped_pair_claimed", ("left", TypeName.SYMBOL, False),
            ("right", TypeName.SYMBOL, False), modality="claim",
            primitive=False)
        atom = Atom(
            "mapped_pair_observed",
            (Constant("a", TypeName.SYMBOL),
             Constant(value, TypeName.SYMBOL)))
        evidence_id = f"mapped-{value}"
        claim = Claim(
            "mapped_pair_claimed", (Variable("same"), Variable("same")),
            id="mapped-claim")
        mapping = EvidenceMapping(
            "mapped_pair_claimed", "mapped_pair_observed", "observation",
            bindings=(("left", "left"), ("right", "right")),
            claim_id="mapped-claim")
        diagnostic = (DiagnosticRule(
            "mapped_pair_observed", "observation",
            claim_id="mapped-claim"),) if relevance == "diagnostic" else ()
        rules = (Rule(
            Atom("mapped_pair_claimed",
                 (Variable("left"), Variable("right"))),
            (Atom("mapped_pair_observed",
                  (Variable("left"), Variable("right"))),),
            "proof-relevance"),) if relevance == "proof" else ()
        output = OutputTemplate(
            "missing_premise", "mapped-claim",
            relation="mapped_pair_claimed",
            fields=(("reason", TemplateValue(value="mapped instance")),),
            requires_all_evidence=(evidence_id,), when_claim="underived")
        return Bundle(
            (observed, claimed), facts=(atom,),
            evidence=(Evidence(evidence_id, atom, source="reviewed"),),
            rules=rules, claims=(claim,), mappings=(mapping,),
            diagnostics=diagnostic, outputs=(output,))

    def test_incompatible_joint_output_trigger_keeps_semantic_fallback(self):
        result = evaluate(self.joint_trigger_bundle()).claims[0].result
        self.assertEqual(result.semantic.value, "unresolved")
        self.assertEqual(result.missing_premises,
                         ("claim:same_pair_claimed:{}",))

    def test_failed_mapping_is_not_discarded_for_diagnostic_relevance(self):
        failed = evaluate(self.failed_mapping_relevance_bundle(
            "b", "diagnostic")).claims[0].result
        matched = evaluate(self.failed_mapping_relevance_bundle(
            "a", "diagnostic")).claims[0].result
        self.assertEqual(failed.missing_premises,
                         ("claim:mapped_pair_claimed:{}",))
        self.assertEqual(
            matched.missing_premises,
            ({"relation": "mapped_pair_claimed",
              "reason": "mapped instance"},))

    def test_failed_mapping_is_not_discarded_for_proof_relevance(self):
        expected = ({
            "kind": "missing_premise",
            "claim_id": "mapped-claim",
            "relation": "mapped_pair_claimed",
            "fields": {"reason": "mapped instance"},
        },)
        failed = self.failed_mapping_relevance_bundle("b", "proof")
        matched = self.failed_mapping_relevance_bundle("a", "proof")
        failed_proof = VerifiedProofEvidence.from_bundle(
            failed, "mapped-claim", {"mapped-b"})
        matched_proof = VerifiedProofEvidence.from_bundle(
            matched, "mapped-claim", {"mapped-a"})
        self.assertEqual(
            render_outputs(
                failed, "mapped-claim", {"mapped-b"}, failed_proof,
                "underived", {"mapped_pair_claimed"}),
            ())
        self.assertEqual(
            render_outputs(
                matched, "mapped-claim", {"mapped-a"}, matched_proof,
                "underived", {"mapped_pair_claimed"}),
            expected)

    def test_python_missing_premise_uses_mapped_variable_evidence(self):
        expected = ({
            "relation": "member_observed",
            "member": "a",
            "reason": "reviewed variable-valued premise",
        },)
        for quantifier in ("exists", "forall"):
            with self.subTest(quantifier=quantifier):
                result = evaluate(self.bundle(quantifier)).claims[0].result
                self.assertEqual(result.semantic.value, "unresolved")
                self.assertEqual(result.missing_premises, expected)

    @staticmethod
    def exclusion_bundle(excluded_value="b", quantifier="exists"):
        observed = relation(
            "excluded_observed", ("member", TypeName.SYMBOL, False))
        claimed = relation(
            "excluded_claimed", ("member", TypeName.SYMBOL, False),
            modality="claim")
        domain = relation(
            "excluded_domain", ("member", TypeName.SYMBOL, False),
            finite=True, nonempty=True)
        closed = relation(
            "excluded_domain_closed", modality="completeness",
            completes="excluded_domain")
        required_atom = Atom(
            "excluded_observed", (Constant("a", TypeName.SYMBOL),))
        excluded_atom = Atom(
            "excluded_observed",
            (Constant(excluded_value, TypeName.SYMBOL),))
        member = Atom(
            "excluded_domain", (Constant("a", TypeName.SYMBOL),))
        closure = Atom("excluded_domain_closed", ())
        facts = tuple(dict.fromkeys((required_atom, excluded_atom, member, closure)))
        evidence = (
            Evidence("required-a", required_atom, source="reviewed"),
            Evidence(f"excluded-{excluded_value}", excluded_atom,
                     source="reviewed"),
            Evidence("excluded-domain-a", member, source="reviewed"),
            Evidence("excluded-domain-closed", closure, source="reviewed"),
        )
        claim = Claim(
            "excluded_claimed", (Variable("member"),),
            quantifier=quantifier,
            domain="excluded_domain" if quantifier == "forall" else None,
            id="excluded-claim")
        mapping = EvidenceMapping(
            "excluded_claimed", "excluded_observed", "observation",
            bindings=(("member", "member"),), claim_id="excluded-claim")
        output = OutputTemplate(
            "missing_premise", "excluded-claim",
            relation="excluded_observed",
            fields=(("reason", TemplateValue(value="anti-join matched")),),
            requires_all_evidence=("required-a",),
            excludes_evidence=(f"excluded-{excluded_value}",),
            when_claim="underived")
        return Bundle(
            (observed, claimed, domain, closed), facts=facts,
            evidence=evidence, claims=(claim,), mappings=(mapping,),
            outputs=(output,))

    def test_exclusion_is_scoped_to_the_required_claim_environment(self):
        expected = ({"relation": "excluded_observed",
                     "reason": "anti-join matched"},)
        for quantifier in ("exists", "forall"):
            with self.subTest(quantifier=quantifier, excluded="b"):
                result = evaluate(
                    self.exclusion_bundle("b", quantifier)).claims[0].result
                self.assertEqual(result.missing_premises, expected)
            with self.subTest(quantifier=quantifier, excluded="a"):
                result = evaluate(
                    self.exclusion_bundle("a", quantifier)).claims[0].result
                self.assertEqual(
                    result.missing_premises,
                    ("claim:excluded_claimed:{}",))

    @staticmethod
    def repeated_and_scoped_bundle():
        observed = relation(
            "pair_observed", ("run", TypeName.SYMBOL, True),
            ("target", TypeName.SYMBOL, False),
            ("left", TypeName.SYMBOL, False),
            ("right", TypeName.SYMBOL, False), context_indices=("run",),
        )
        claimed = relation(
            "pair_claimed", ("run", TypeName.SYMBOL, True),
            ("target", TypeName.SYMBOL, False),
            ("left", TypeName.SYMBOL, False),
            ("right", TypeName.SYMBOL, False), modality="claim",
            context_indices=("run",),
        )
        rows = (
            ("good", "run-a", "target", "a", "a"),
            ("unequal", "run-a", "target", "a", "b"),
            ("wrong-value", "run-a", "other", "a", "a"),
            ("wrong-scope", "run-b", "target", "a", "a"),
        )
        atoms = tuple(
            Atom("pair_observed", tuple(Constant(value, TypeName.SYMBOL)
                                         for value in values))
            for _, *values in rows
        )
        evidence = tuple(
            Evidence(name, atom, Context.from_mapping({"run": values[0]}),
                     source="reviewed")
            for (name, *values), atom in zip(rows, atoms)
        )
        claim = Claim(
            "pair_claimed",
            (Constant("run-a", TypeName.SYMBOL),
             Constant("target", TypeName.SYMBOL),
             Variable("same"), Variable("same")),
            Context.from_mapping({"run": "run-a"}), id="claim",
        )
        mapping = EvidenceMapping(
            "pair_claimed", "pair_observed", "observation",
            context_indices=("run",),
            bindings=(("run", "run"), ("target", "target"),
                      ("left", "left"), ("right", "right")),
            claim_id="claim",
        )
        outputs = tuple(
            OutputTemplate(
                "missing_premise", "claim", relation="pair_observed",
                fields=(("reason", TemplateValue(value=name)),),
                requires_all_evidence=(name,), when_claim="underived",
            )
            for name, *_ in rows
        )
        return Bundle((observed, claimed), facts=atoms, evidence=evidence,
                      claims=(claim,), mappings=(mapping,), outputs=outputs)

    def test_repeated_variables_wrong_values_and_scope_filter_output_evidence(self):
        result = evaluate(self.repeated_and_scoped_bundle()).claims[0].result
        self.assertEqual(result.missing_premises,
                         ({"relation": "pair_observed", "reason": "good"},))

    def test_forall_member_does_not_reuse_another_members_output_trigger(self):
        base = self.bundle("forall")
        observed = next(atom for atom in base.facts
                        if atom.relation == "member_observed")
        closed = next(atom for atom in base.facts
                      if atom.relation == "member_domain_closed")
        observed_evidence = next(record for record in base.evidence
                                 if record.id == "observed-a")
        closure_evidence = next(record for record in base.evidence
                                if record.id == "domain-closed")
        member_b = Atom("member_domain", (Constant("b", TypeName.SYMBOL),))
        changed = replace(
            base, facts=(observed, member_b, closed),
            evidence=(observed_evidence,
                      Evidence("domain-b", member_b, source="review"),
                      closure_evidence),
        )
        result = evaluate(changed).claims[0].result
        self.assertEqual(result.semantic.value, "unresolved")
        self.assertEqual(result.missing_premises,
                         ('claim:member_claimed:{}',))

    def test_variable_relevance_applies_any_and_exclusion_triggers(self):
        base = self.bundle()
        output = base.outputs[0]
        cases = (
            (replace(output, requires_all_evidence=(),
                     requires_any_evidence=("observed-a",)), True),
            (replace(output, requires_all_evidence=(),
                     excludes_evidence=("observed-a",)), False),
        )
        for changed, active in cases:
            with self.subTest(output=changed):
                result = evaluate(replace(base, outputs=(changed,))).claims[0].result
                self.assertEqual(bool(result.missing_premises
                                      and isinstance(result.missing_premises[0], dict)),
                                 active)


@unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
class SouffleVariableOutputRelevanceTests(unittest.TestCase):
    def test_cyclic_closure_and_every_claim_field_agree(self):
        result = compare(KernelClosureTests.self_cycle_bundle(), shrink=False)
        self.assertEqual(result.python.relations, result.souffle.relations)
        self.assertEqual(dict(result.python.relations)["reachable"], (("v",),))
        self.assertEqual(result.python.claims, result.souffle.claims)
        claim = result.python.claims[0]
        self.assertEqual((claim.semantic, claim.operational, claim.basis,
                          claim.missing_premises),
                         ("supported", "complete", "derivational", ()))

    def test_exact_late_shorter_bundle_agrees_on_closure_and_claim_fields(self):
        result = compare(KernelClosureTests.late_shorter_bundle(), shrink=False)
        self.assertTrue(result.matched)
        self.assertEqual(result.python.relations, result.souffle.relations)
        self.assertEqual(result.python.claims, result.souffle.claims)
        self.assertEqual(dict(result.python.relations)["n_out"], (("v",),))
        claim = result.python.claims[0]
        self.assertEqual((claim.semantic, claim.operational, claim.basis,
                          claim.missing_premises),
                         ("supported", "complete", "derivational", ()))

    def test_typed_compatibility_controls_cross_both_kernel_boundaries(self):
        invalid = (
            KernelClosureTests._mixed_literal_bundle(
                TypeName.DIGEST,
                Constant("index-a"), Constant("run-a"),
                Constant("index-a"), Constant("run-a")),
            KernelClosureTests._mixed_literal_bundle(
                TypeName.SYMBOL,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.SYMBOL),
                Constant("run-a", TypeName.SYMBOL)),
            KernelClosureTests._mixed_literal_bundle(
                TypeName.DIGEST,
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.SYMBOL),
                Constant("index-a", TypeName.DIGEST),
                Constant("run-a", TypeName.DIGEST),
                witness_run_type=TypeName.DIGEST),
        )
        for bundle in invalid:
            with self.subTest(bundle=bundle):
                self.assertEqual(run_python(bundle).operational_failure,
                                 "invalid-input")
                self.assertEqual(run_souffle(bundle).operational_failure,
                                 "invalid-input")
        valid = KernelClosureTests._mixed_literal_bundle(
            TypeName.DIGEST,
            Constant("index-a", TypeName.DIGEST),
            Constant("run-a", TypeName.SYMBOL),
            Constant("index-a", TypeName.DIGEST),
            Constant("run-a", TypeName.SYMBOL),
        )
        result = compare(valid, shrink=False)
        self.assertTrue(result.matched)
        self.assertEqual(dict(result.python.relations)["joined"], (("v",),))
        self.assertEqual(result.python.claims, result.souffle.claims)
        claim = result.python.claims[0]
        self.assertEqual((claim.semantic, claim.operational, claim.basis,
                          claim.missing_premises),
                         ("supported", "complete", "derivational", ()))

        without_witness = replace(
            valid,
            facts=tuple(atom for atom in valid.facts
                        if atom.relation != "index_describes_run"),
            evidence=tuple(record for record in valid.evidence
                           if record.id != "compatibility-witness"),
        )
        missing = compare(without_witness, shrink=False)
        self.assertEqual(dict(missing.python.relations)["joined"], ())
        self.assertEqual(missing.python.claims, missing.souffle.claims)
        self.assertEqual(missing.python.claims[0].semantic, "unresolved")

    def test_repeated_wrong_and_scoped_values_match_python(self):
        result = compare(
            VariableOutputRelevanceTests.repeated_and_scoped_bundle(),
            shrink=False,
        )
        expected = ('{"reason":"good","relation":"pair_observed"}',)
        self.assertEqual(result.python.claims[0].missing_premises, expected)
        self.assertEqual(result.python.claims, result.souffle.claims)

    def test_forall_member_does_not_reuse_another_members_output_trigger(self):
        base = VariableOutputRelevanceTests.bundle("forall")
        observed = next(atom for atom in base.facts
                        if atom.relation == "member_observed")
        closed = next(atom for atom in base.facts
                      if atom.relation == "member_domain_closed")
        observed_evidence = next(record for record in base.evidence
                                 if record.id == "observed-a")
        closure_evidence = next(record for record in base.evidence
                                if record.id == "domain-closed")
        member_b = Atom("member_domain", (Constant("b", TypeName.SYMBOL),))
        changed = replace(
            base, facts=(observed, member_b, closed),
            evidence=(observed_evidence,
                      Evidence("domain-b", member_b, source="review"),
                      closure_evidence),
        )
        result = compare(changed, shrink=False)
        expected = ('"claim:member_claimed:{}"',)
        self.assertEqual(result.python.claims[0].missing_premises, expected)
        self.assertEqual(result.python.claims, result.souffle.claims)

    def test_incompatible_joint_output_trigger_matches_with_fallback(self):
        result = compare(
            VariableOutputRelevanceTests.joint_trigger_bundle(),
            shrink=False)
        fallback = ('"claim:same_pair_claimed:{}"',)
        self.assertTrue(result.matched)
        self.assertEqual(result.python.relations, result.souffle.relations)
        self.assertEqual(result.python.claims, result.souffle.claims)
        self.assertEqual(result.python.claims[0].missing_premises, fallback)

        compatible = compare(
            VariableOutputRelevanceTests.joint_trigger_bundle("a"),
            shrink=False)
        self.assertTrue(compatible.matched)
        self.assertEqual(compatible.python.claims, compatible.souffle.claims)
        self.assertEqual(
            compatible.python.claims[0].missing_premises,
            ('{"reason":"incompatible rows must not trigger",'
             '"relation":"same_pair_claimed"}',))

    def test_wrong_scope_output_payload_evidence_keeps_fallback(self):
        base = VariableOutputRelevanceTests.bundle()
        output = replace(base.outputs[0], evidence_id="domain-a")
        result = compare(replace(base, outputs=(output,)), shrink=False)
        fallback = ('"claim:member_claimed:{}"',)
        self.assertTrue(result.matched)
        self.assertEqual(result.python.claims, result.souffle.claims)
        self.assertEqual(result.python.claims[0].missing_premises, fallback)

    def test_missing_premise_uses_mapped_variable_evidence(self):
        expected = ('{"member":"a","reason":"reviewed variable-valued premise",'
                    '"relation":"member_observed"}',)
        for quantifier in ("exists", "forall"):
            with self.subTest(quantifier=quantifier):
                result = compare(
                    VariableOutputRelevanceTests.bundle(quantifier),
                    shrink=False)
                self.assertTrue(result.matched)
                self.assertEqual(result.python.claims, result.souffle.claims)
                self.assertEqual(result.python.claims[0].semantic, "unresolved")
                self.assertEqual(result.python.claims[0].missing_premises,
                                 expected)

    def test_exclusions_are_anti_joined_with_the_required_environment(self):
        rendered = ('{"reason":"anti-join matched",'
                    '"relation":"excluded_observed"}',)
        fallback = ('"claim:excluded_claimed:{}"',)
        for quantifier in ("exists", "forall"):
            with self.subTest(quantifier=quantifier, excluded="b"):
                result = compare(
                    VariableOutputRelevanceTests.exclusion_bundle(
                        "b", quantifier), shrink=False)
                self.assertEqual(result.python.claims, result.souffle.claims)
                self.assertEqual(
                    result.python.claims[0].missing_premises, rendered)
            with self.subTest(quantifier=quantifier, excluded="a"):
                result = compare(
                    VariableOutputRelevanceTests.exclusion_bundle(
                        "a", quantifier), shrink=False)
                self.assertEqual(result.python.claims, result.souffle.claims)
                self.assertEqual(
                    result.python.claims[0].missing_premises, fallback)

    def test_any_and_exclusion_triggers_share_variable_unification(self):
        base = VariableOutputRelevanceTests.bundle()
        output = base.outputs[0]
        cases = (
            (replace(output, requires_all_evidence=(),
                     requires_any_evidence=("observed-a",)), True),
            (replace(output, requires_all_evidence=(),
                     excludes_evidence=("observed-a",)), False),
        )
        for changed, active in cases:
            with self.subTest(output=changed):
                result = compare(replace(base, outputs=(changed,)), shrink=False)
                self.assertTrue(result.matched)
                self.assertEqual(result.python.claims, result.souffle.claims)
                missing = result.python.claims[0].missing_premises
                self.assertEqual(bool(missing and missing[0].startswith("{")),
                                 active)


if __name__ == "__main__":
    unittest.main()
