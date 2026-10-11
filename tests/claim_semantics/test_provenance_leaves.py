from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest

from capcov.claims import (Aggregation, Atom, Bundle, Claim, Column, Constant,
                           Context, DiagnosticRule, Evidence, EvidenceMapping,
                           RelationDecl, Rule, Variable, canonical_json)
from capcov.claims.evaluator import Derivation, ResourceLimits, _Engine, evaluate

from .adapter import load_fixture

ROOT = Path(__file__).parent / "corpus"
EXPECTED_DOCUMENT = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
EXPECTED = EXPECTED_DOCUMENT["cases"]


class CorpusProvenanceTests(unittest.TestCase):
    def test_all_reviewed_claims_have_exact_verdict_status_and_evidence_leaves(self):
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            bundle = load_fixture(path); report = evaluate(bundle)
            evidence_ids = {record.id for record in bundle.evidence}
            actual = {entry.claim.id: entry for entry in report.claims}
            self.assertEqual(set(actual), set(EXPECTED[path.stem]["claims"]), path.stem)
            for claim_id, expected in EXPECTED[path.stem]["claims"].items():
                with self.subTest(case=path.stem, claim=claim_id):
                    result = actual[claim_id].result
                    self.assertEqual(result.semantic.value, expected["semantic_verdict"])
                    self.assertEqual(result.operational.value, expected["operational_status"])
                    self.assertEqual(
                        result.basis.value,
                        EXPECTED_DOCUMENT["evaluation_basis_by_quantifier"][actual[claim_id].claim.quantifier.value])
                    self.assertEqual(result.support, tuple(sorted(expected["support_leaves"])))
                    self.assertEqual(result.refutation, tuple(sorted(expected["refutation_leaves"])))
                    self.assertEqual(
                        tuple(sorted(canonical_json(item) for item in result.missing_premises)),
                        tuple(sorted(canonical_json(item) for item in expected["missing_premises"])))
                    self.assertTrue(set(result.support) | set(result.refutation) <= evidence_ids)
            for _, rows in report.provenance:
                for _, proofs in rows:
                    for proof in proofs:
                        self.assertTrue(set(proof.leaves) <= evidence_ids, (path.stem, proof.leaves))
            self.assertEqual(dict(report.resources)["unattributed_facts"], 0)

    def test_fixture_ten_contains_a_revoked_path_and_an_independent_survivor(self):
        bundle = load_fixture(ROOT / "10-revoked-assumption-alternative.json")
        by_id = {record.id: record for record in bundle.evidence}
        self.assertEqual(by_id["fact-revoked-proof"].depends_on,
                         ("assumption-revoked",))
        self.assertEqual(evaluate(bundle).claims[0].result.support,
                         ("assumption-independent-source", "fact-independent-proof"))

        removed = "fact-independent-proof"
        evidence = tuple(record for record in bundle.evidence if record.id != removed)
        retained_keys = {(record.atom.relation,
                          tuple(term.value for term in record.atom.terms))
                         for record in evidence}
        facts = tuple(fact for fact in bundle.facts
                      if (fact.relation, tuple(term.value for term in fact.terms)) in retained_keys)
        outputs = tuple(output for output in bundle.outputs
                        if removed not in ({output.evidence_id}
                                           | set(output.requires_all_evidence)
                                           | set(output.requires_any_evidence)
                                           | set(output.excludes_evidence)))
        tainted_only = replace(bundle, facts=facts, evidence=evidence, outputs=outputs)
        result = evaluate(tainted_only).claims[0].result
        self.assertEqual(result.semantic.value, "unresolved")
        self.assertEqual(result.operational.value, "stale")

    def test_duplicate_producers_remain_alternatives_and_survive_either_revocation(self):
        observed = RelationDecl("observed", (Column("x", "symbol"),))
        claim_rel = RelationDecl("claim_rel", (Column("x", "symbol"),), modality="claim", primitive=False)
        atom = Atom("observed", (Constant("v"),))
        rule = Rule(Atom("claim_rel", (Variable("x"),)), (Atom("observed", (Variable("x"),)),), "derive")
        evidence = (Evidence("producer-a", atom, source="a"), Evidence("producer-b", atom, source="b"))
        bundle = Bundle((observed, claim_rel), facts=(atom,), evidence=evidence, rules=(rule,),
                        claims=(Claim("claim_rel", (Constant("v"),), id="claim"),))
        report = evaluate(bundle)
        fact_proofs = dict(dict(report.provenance)["observed"])[("v",)]
        self.assertEqual({proof.leaf_id for proof in fact_proofs}, {"producer-a", "producer-b"})
        self.assertEqual(len({proof.signature() for proof in fact_proofs}), 2)
        for retained in evidence:
            changed = replace(bundle, evidence=(retained,))
            result = evaluate(changed).claims[0].result
            self.assertEqual(result.semantic.value, "supported")
            self.assertEqual(result.support, (retained.id,))

    def test_and_children_keep_one_or_choice_per_atom_and_revocation_uses_alternatives(self):
        blocker = RelationDecl("revoked", (Column("side", "symbol"),), modality="assumption")
        left = RelationDecl("left_seen", (Column("x", "symbol"),))
        right = RelationDecl("right_seen", (Column("x", "symbol"),))
        claimed = RelationDecl("joined", (Column("x", "symbol"),),
                               modality="claim", primitive=False)
        left_atom = Atom("left_seen", (Constant("v"),))
        right_atom = Atom("right_seen", (Constant("v"),))
        left_block = Atom("revoked", (Constant("left"),))
        right_block = Atom("revoked", (Constant("right"),))
        evidence = (
            Evidence("block-left", left_block, source="test"),
            Evidence("block-right", right_block, source="test"),
            Evidence("a-left-tainted", left_atom, source="test", depends_on=("block-left",)),
            Evidence("z-left-independent", left_atom, source="test"),
            Evidence("a-right-tainted", right_atom, source="test", depends_on=("block-right",)),
            Evidence("z-right-independent", right_atom, source="test"),
        )
        diagnostics = tuple(
            DiagnosticRule("revoked", "refutation", claim_id="claim",
                           predicate=(("column", "side"), ("operator", "="), ("value", side)))
            for side in ("left", "right")
        )
        rule = Rule(Atom("joined", (Variable("x"),)),
                    (Atom("left_seen", (Variable("x"),)),
                     Atom("right_seen", (Variable("x"),))), "join")
        bundle = Bundle((blocker, left, right, claimed),
                        facts=(left_block, right_block, left_atom, right_atom),
                        evidence=evidence, rules=(rule,),
                        claims=(Claim("joined", (Constant("v"),), id="claim"),),
                        diagnostics=diagnostics)
        report = evaluate(bundle)
        proof = dict(dict(report.provenance)["joined"])[("v",)][0]
        self.assertEqual(len(proof.children), 2)
        self.assertEqual(tuple(child.leaf_id for child in proof.children),
                         ("a-left-tainted", "a-right-tainted"))
        self.assertEqual(proof.alternatives, 2)
        self.assertEqual(report.claims[0].result.support,
                         ("z-left-independent", "z-right-independent"))

    def test_per_row_cap_fails_closed_without_an_unbounded_shadow_set(self):
        relation = RelationDecl("seen", (Column("x", "symbol"),))
        bundle = Bundle((relation,))
        engine = _Engine(bundle, ResourceLimits(max_provenance=1, max_alternatives_per_row=1))
        row = ("v",)
        self.assertTrue(engine.add("seen", row, Derivation("seen", row, leaf_id="z", kind="fact")))
        with self.assertRaisesRegex(RuntimeError, "alternative provenance limit"):
            engine.add("seen", row, Derivation("seen", row, leaf_id="zz", kind="fact"))
        self.assertEqual(engine.provenance_count, 1)
        self.assertEqual(len(engine.proof_path_candidates["seen"][row]), 1)

    def test_distinct_top_level_derivations_report_one_canonical_support_path(self):
        observed_a = RelationDecl("observed_a", (Column("x", "symbol"),))
        observed_b = RelationDecl("observed_b", (Column("x", "symbol"),))
        via_a = RelationDecl("via_a", (Column("x", "symbol"),), primitive=False)
        via_b = RelationDecl("via_b", (Column("x", "symbol"),), primitive=False)
        claimed = RelationDecl("canonical_claim", (Column("x", "symbol"),),
                               modality="claim", primitive=False)
        atom_a = Atom("observed_a", (Constant("v"),))
        atom_b = Atom("observed_b", (Constant("v"),))
        bundle = Bundle(
            (observed_a, observed_b, via_a, via_b, claimed),
            facts=(atom_a, atom_b),
            evidence=(Evidence("leaf-a", atom_a, source="test"),
                      Evidence("leaf-b", atom_b, source="test")),
            rules=(
                Rule(Atom("via_a", (Variable("x"),)),
                     (Atom("observed_a", (Variable("x"),)),), "path-a-1"),
                Rule(Atom("canonical_claim", (Variable("x"),)),
                     (Atom("via_a", (Variable("x"),)),), "path-a-2"),
                Rule(Atom("via_b", (Variable("x"),)),
                     (Atom("observed_b", (Variable("x"),)),), "path-b-1"),
                Rule(Atom("canonical_claim", (Variable("x"),)),
                     (Atom("via_b", (Variable("x"),)),), "path-b-2"),
            ),
            claims=(Claim("canonical_claim", (Constant("v"),), id="claim"),))
        report = evaluate(bundle)
        claim_proofs = dict(dict(report.provenance)["canonical_claim"])[("v",)]
        self.assertEqual({proof.leaves for proof in claim_proofs},
                         {("leaf-a",), ("leaf-b",)})
        self.assertEqual(report.claims[0].result.support, ("leaf-a",))

    def test_shallower_canonical_proof_is_not_replaced_by_late_lexical_path(self):
        one = (Column("x", "symbol"),)
        relations = tuple(RelationDecl(name, one, primitive=name in {"aa_seed_z", "zz_seed_a"})
                          for name in ("aa_seed_z", "zz_seed_a", "zz_hop", "mid", "out"))
        seed_z = Atom("aa_seed_z", (Constant("v"),)); seed_a = Atom("zz_seed_a", (Constant("v"),))
        rules = (
            Rule(Atom("mid", (Variable("x"),)), (Atom("aa_seed_z", (Variable("x"),)),), "z-path"),
            Rule(Atom("mid", (Variable("x"),)), (Atom("zz_hop", (Variable("x"),)),), "a-path"),
            Rule(Atom("out", (Variable("x"),)), (Atom("mid", (Variable("x"),)),), "out"),
            Rule(Atom("zz_hop", (Variable("x"),)), (Atom("zz_seed_a", (Variable("x"),)),), "hop"),
        )
        bundle = Bundle(relations, facts=(seed_z, seed_a), rules=rules,
                        evidence=(Evidence("leaf-z", seed_z, source="test"),
                                  Evidence("leaf-a", seed_a, source="test")),
                        claims=(Claim("out", (Constant("v"),), id="claim"),))
        report = evaluate(bundle, ResourceLimits(max_alternatives_per_row=2, max_provenance=6))
        self.assertEqual(report.status.value, "complete")
        # ``leaf-a`` is lexically first but reaches ``mid`` through an extra
        # hop.  Canonical proof choice is depth-first, then lexical.
        self.assertEqual(report.claims[0].result.support, ("leaf-z",))
        out_proof = dict(dict(report.provenance)["out"])[("v",)][0]
        self.assertEqual(out_proof.leaves, ("leaf-z",))

    def test_many_duplicate_producers_fail_closed_at_the_explanation_bound(self):
        observed = RelationDecl("observed_many", (Column("x", "symbol"),))
        claimed = RelationDecl("claimed_many", (Column("x", "symbol"),), modality="claim", primitive=False)
        atom = Atom("observed_many", (Constant("v"),))
        evidence = tuple(Evidence(f"producer-{i:03d}", atom, source="test") for i in range(70))
        bundle = Bundle((observed, claimed), facts=(atom,), evidence=evidence,
                        rules=(Rule(Atom("claimed_many", (Variable("x"),)),
                                    (Atom("observed_many", (Variable("x"),)),), "derive"),),
                        claims=(Claim("claimed_many", (Constant("v"),), id="claim"),))
        exhausted = evaluate(bundle, ResourceLimits(max_alternatives_per_row=64,
                                                     max_provenance=100))
        self.assertEqual(exhausted.status.value, "resource-exhausted")
        self.assertIn("alternative provenance limit", exhausted.message)
        self.assertEqual(exhausted.claims[0].operational.value, "resource-exhausted")

    def test_refuted_forall_has_only_counterexample_domain_and_closure_leaves(self):
        columns = (Column("tenant", "symbol", True), Column("member", "symbol"))
        domain = RelationDecl("members", columns, finite=True, nonempty=True,
                              context_indices=("tenant",))
        closed = RelationDecl("members_closed", (Column("tenant", "symbol", True),),
                              modality="completeness", completes="members",
                              context_indices=("tenant",))
        negative = RelationDecl("member_failed", columns, modality="claim",
                                polarity="negative", primitive=False,
                                context_indices=("tenant",))
        observed = RelationDecl("failure_observed", columns,
                                context_indices=("tenant",))
        member_a = Atom("members", (Constant("t"), Constant("a")))
        member_b = Atom("members", (Constant("t"), Constant("b")))
        closure = Atom("members_closed", (Constant("t"),))
        counterexample = Atom("failure_observed", (Constant("t"), Constant("a")))
        context = Context.from_mapping({"tenant": "t"})
        evidence = (
            Evidence("domain-a", member_a, context, source="test"),
            Evidence("domain-b", member_b, context, source="test"),
            Evidence("domain-closed", closure, context, source="test"),
            Evidence("negative-a", counterexample, context, source="test"),
        )
        rule = Rule(Atom("member_failed", (Variable("tenant"), Variable("member"))),
                    (Atom("failure_observed", (Variable("tenant"), Variable("member"))),),
                    "derive-negative")
        claim = Claim("member_failed", (Constant("t"), Variable("member")), context,
                      "forall", "members", "all-members")
        result = evaluate(Bundle((domain, closed, negative, observed),
                                 facts=(member_a, member_b, closure, counterexample),
                                 evidence=evidence, rules=(rule,), claims=(claim,))).claims[0].result
        self.assertEqual(result.semantic.value, "refuted")
        self.assertEqual(result.refutation,
                         ("domain-a", "domain-closed", "negative-a"))
        self.assertNotIn("domain-b", result.refutation)

    def test_conflicting_forall_keeps_structured_support_and_refutation(self):
        columns = (Column("tenant", "symbol", True), Column("member", "symbol"))
        domain = RelationDecl("conflict_members", columns, finite=True, nonempty=True,
                              context_indices=("tenant",))
        closed = RelationDecl("conflict_members_closed",
                              (Column("tenant", "symbol", True),),
                              modality="completeness", completes="conflict_members",
                              context_indices=("tenant",))
        claimed = RelationDecl("member_holds", columns, modality="claim",
                               primitive=False, context_indices=("tenant",))
        positive = RelationDecl("member_positive", columns,
                                context_indices=("tenant",))
        negative = RelationDecl("member_negative", columns,
                                context_indices=("tenant",))
        member = Atom("conflict_members", (Constant("t"), Constant("a")))
        closure = Atom("conflict_members_closed", (Constant("t"),))
        positive_atom = Atom("member_positive", (Constant("t"), Constant("a")))
        negative_atom = Atom("member_negative", (Constant("t"), Constant("a")))
        context = Context.from_mapping({"tenant": "t"})
        evidence = (
            Evidence("conflict-domain", member, context, source="test"),
            Evidence("conflict-closed", closure, context, source="test"),
            Evidence("positive-a", positive_atom, context, source="test"),
            Evidence("negative-a", negative_atom, context, source="test"),
        )
        rule = Rule(Atom("member_holds", (Variable("tenant"), Variable("member"))),
                    (Atom("member_positive", (Variable("tenant"), Variable("member"))),),
                    "derive-positive")
        mapping = EvidenceMapping("member_holds", "member_negative", "refutation",
                                  context_indices=("tenant",),
                                  bindings=(("tenant", "tenant"), ("member", "member")),
                                  claim_id="all-members")
        claim = Claim("member_holds", (Constant("t"), Variable("member")), context,
                      "forall", "conflict_members", "all-members")
        result = evaluate(Bundle((domain, closed, claimed, positive, negative),
                                 facts=(member, closure, positive_atom, negative_atom),
                                 evidence=evidence, rules=(rule,), claims=(claim,),
                                 mappings=(mapping,))).claims[0].result
        self.assertEqual(result.semantic.value, "conflicting")
        self.assertEqual(result.support,
                         ("conflict-closed", "conflict-domain", "positive-a"))
        self.assertEqual(result.refutation,
                         ("conflict-closed", "conflict-domain", "negative-a"))

    def test_all_aggregate_provenance_contains_every_domain_member(self):
        source = RelationDecl(
            "aggregate_source",
            (Column("tenant", "symbol", True), Column("member", "symbol"),
             Column("holds", "boolean")), context_indices=("tenant",))
        domain = RelationDecl(
            "aggregate_domain",
            (Column("tenant", "symbol", True), Column("member", "symbol")),
            finite=True, nonempty=True, context_indices=("tenant",))
        closed = RelationDecl(
            "aggregate_domain_closed", (Column("tenant", "symbol", True),),
            modality="completeness", completes="aggregate_domain",
            context_indices=("tenant",))
        claimed = RelationDecl(
            "aggregate_claim", (Column("tenant", "symbol", True),),
            modality="claim", primitive=False, context_indices=("tenant",))
        domain_a = Atom("aggregate_domain", (Constant("t"), Constant("a")))
        domain_b = Atom("aggregate_domain", (Constant("t"), Constant("b")))
        witness = Atom("aggregate_domain_closed", (Constant("t"),))
        source_a = Atom("aggregate_source",
                        (Constant("t"), Constant("a"), Constant(True)))
        source_b = Atom("aggregate_source",
                        (Constant("t"), Constant("b"), Constant(True)))
        context = Context.from_mapping({"tenant": "t"})
        evidence = (
            Evidence("domain-a", domain_a, context, source="review"),
            Evidence("domain-b", domain_b, context, source="review"),
            Evidence("domain-closed", witness, context, source="review"),
            Evidence("source-a", source_a, context, source="producer"),
            Evidence("source-b", source_b, context, source="producer"),
        )
        aggregate = Aggregation("all_members", "aggregate_source", ("tenant",),
                                "holds", "all", "aggregate_domain",
                                "aggregate_domain_closed")
        rule = Rule(
            Atom("aggregate_claim", (Variable("tenant"),)),
            (Atom("aggregate_source", (Variable("tenant"), Variable("member"),
                                       Variable("holds"))),
             Atom("aggregate_domain", (Variable("tenant"), Variable("member"))),
             Atom("aggregate_domain_closed", (Variable("tenant"),))),
            "all-members", aggregate)
        claim = Claim("aggregate_claim", (Constant("t"),), context, id="claim")
        bundle = Bundle((source, domain, closed, claimed),
                        facts=(domain_a, domain_b, witness, source_a, source_b),
                        evidence=evidence, rules=(rule,), claims=(claim,))
        self.assertEqual(evaluate(bundle).claims[0].result.support,
                         ("domain-a", "domain-b", "domain-closed",
                          "source-a", "source-b"))

    def test_evidence_less_bundles_are_rejected_and_legacy_fact_ids_need_the_validation_bypass(self):
        relation = RelationDecl("seen", (Column("x", "symbol"),))
        bundle = Bundle((relation,), facts=(Atom("seen", (Constant("v"),)),),
                        claims=(Claim("seen", (Constant("v"),)),))
        # attribution is unconditional: the public evaluator fails closed
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "invalid-input")
        self.assertIn("fact-without-evidence", report.message)
        self.assertEqual(report.claims[0].result.support, ())
        # the fallback leaf id exists only for an engine run without validation
        engine = _Engine(bundle, ResourceLimits())
        engine.run()
        self.assertEqual(engine.evaluate_claim(0, bundle.claims[0]).result.support, ('fact:seen:["v"]',))
        self.assertEqual(engine.unattributed_facts, 1)

    def test_positive_rule_premises_cannot_be_bypassed_by_support_mappings(self):
        for name in ("01-correlated-positive.json", "14-bounded-no-resend.json"):
            bundle = load_fixture(ROOT / name)
            head = bundle.claims[0].relation
            required = {atom.relation for rule in bundle.rules if rule.head.relation == head for atom in rule.body
                        if isinstance(atom, Atom)}
            for relation in required:
                facts = tuple(fact for fact in bundle.facts if fact.relation != relation)
                removed_ids = {record.id for record in bundle.evidence if record.atom.relation == relation}
                changed = True
                while changed:
                    dependent = {record.id for record in bundle.evidence
                                 if set(record.depends_on) & removed_ids}
                    changed = not dependent.issubset(removed_ids); removed_ids.update(dependent)
                evidence = tuple(record for record in bundle.evidence if record.id not in removed_ids)
                retained_keys = {(record.atom.relation, tuple(term.value for term in record.atom.terms))
                                 for record in evidence}
                facts = tuple(fact for fact in facts
                              if (fact.relation, tuple(term.value for term in fact.terms)) in retained_keys)
                outputs = tuple(output for output in bundle.outputs
                                if not ({output.evidence_id} | set(output.requires_all_evidence)
                                        | set(output.requires_any_evidence)) & removed_ids)
                mutant = replace(bundle, facts=facts, evidence=evidence, outputs=outputs)
                with self.subTest(case=name, premise=relation):
                    self.assertEqual(evaluate(mutant).claims[0].semantic.value, "unresolved")


if __name__ == "__main__": unittest.main()
