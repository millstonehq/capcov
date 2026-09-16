from __future__ import annotations

import json
from pathlib import Path
import unittest

from capcov.claims import (Aggregation, Atom, Bundle, Claim, Column, Constant,
                           Context, Evidence, EvidenceMapping, Modality,
                           RelationDecl, Rule, Variable, validate_bundle)


def codes(bundle): return {issue.code for issue in validate_bundle(bundle)}


class Section27ValidationTests(unittest.TestCase):
    def test_primitive_relation_rejects_an_unauthorized_producer_class(self):
        relation = RelationDecl("runtime_sql_executed", (Column("run", "symbol", True),),
                                producer_classes=("target-go-runtime-trace-v2",),
                                context_indices=("run",))
        atom = Atom("runtime_sql_executed", (Constant("run-1", "symbol"),))
        # Producer identity lives in Evidence.source (its first token names the
        # producer class); Evidence.kind stays fact/assumption/completeness/
        # compatibility.  A kind-based check briefly existed and collided with
        # this contract; see section 26 (2026-09-15, replay-judge integration).
        unauthorized = Bundle((relation,), facts=(atom,), evidence=(
            Evidence("event", atom, Context.from_mapping({"run": "run-1"}),
                     source="generic-json receipt", kind="fact"),))
        self.assertIn("evidence-producer", codes(unauthorized))
        authorized = Bundle((relation,), facts=(atom,), evidence=(
            Evidence("event", atom, Context.from_mapping({"run": "run-1"}),
                     source="target-go-runtime-trace-v2 receipt", kind="fact"),))
        self.assertNotIn("evidence-producer", codes(authorized))

    def test_evidence_and_facts_correspond_exactly(self):
        relation = RelationDecl("seen", (Column("x", "symbol"),))
        atom = Atom("seen", (Constant("x"),))
        orphan = Bundle((relation,), evidence=(Evidence("e", atom, source="test"),))
        self.assertIn("evidence-without-fact", codes(orphan))
        unattributed = Bundle((relation,), facts=(atom, Atom("seen", (Constant("y"),))),
                              evidence=(Evidence("e", atom, source="test"),))
        self.assertIn("fact-without-evidence", codes(unattributed))

    def test_static_context_free_is_explicit_and_index_context_is_strict(self):
        arbitrary = RelationDecl("tree", (Column("digest", "digest"),), binding="static")
        self.assertIn("static-context", codes(Bundle((arbitrary,))))
        context_free = RelationDecl("source_tree_observed", (Column("tree_digest", "digest"),),
                                    binding="static")
        self.assertFalse(validate_bundle(Bundle((context_free,))))
        near_misses = (
            RelationDecl("source_tree_observed_copy", (Column("tree_digest", "digest"),),
                         binding="static"),
            RelationDecl("source_tree_observed", (Column("digest", "digest"),),
                         binding="static"),
            RelationDecl("source_tree_observed", (Column("tree_digest", "symbol"),),
                         binding="static"),
            RelationDecl("source_tree_observed", (Column("tree_digest", "digest", True),),
                         binding="static", context_indices=("tree_digest",)),
        )
        for declaration in near_misses:
            with self.subTest(near_miss=declaration):
                self.assertIn("static-context", codes(Bundle((declaration,))))
        malformed = RelationDecl("site", (Column("index", "digest", True), Column("path", "symbol")),
                                 binding="static", context_indices=())
        self.assertIn("static-context", codes(Bundle((malformed,))))
        indexed = RelationDecl("site", (Column("index", "digest", True), Column("path", "symbol")),
                               binding="static", context_indices=("index",))
        self.assertFalse(validate_bundle(Bundle((indexed,))))

    def test_arbitrary_indexless_static_relation_cannot_bypass_runtime_join(self):
        static = RelationDecl("arbitrary_static", (Column("x", "symbol"),), binding="static")
        runtime = RelationDecl("runtime_seen", (Column("run", "symbol", True), Column("x", "symbol")),
                               context_indices=("run",))
        out = RelationDecl("joined", (Column("x", "symbol"),), primitive=False)
        rule = Rule(Atom("joined", (Variable("x"),)),
                    (Atom("arbitrary_static", (Variable("x"),)),
                     Atom("runtime_seen", (Variable("run"), Variable("x")))))
        self.assertIn("static-context", codes(Bundle((static, runtime, out), rules=(rule,))))

    def test_only_genuinely_context_free_static_schema_is_allowlisted(self):
        legacy = (
            RelationDecl("static_route_exists",
                         (Column("tenant", "symbol", True),
                          Column("surface", "symbol", True)),
                         binding="static", context_indices=("tenant", "surface")),
            RelationDecl("mail_path_declared",
                         (Column("tenant", "symbol", True),
                          Column("event", "symbol", True),
                          Column("run", "symbol", True)), binding="static",
                         context_indices=("tenant", "event", "run")),
        )
        for declaration in legacy:
            with self.subTest(relation=declaration.name):
                self.assertIn("static-context", codes(Bundle((declaration,))))

        static = RelationDecl("source_tree_observed",
                              (Column("tree_digest", "digest"),), binding="static")
        runtime = RelationDecl("runtime_seen",
                               (Column("run", "symbol", True),
                                Column("tree_digest", "digest")),
                               context_indices=("run",))
        out = RelationDecl("joined", (Column("run", "symbol", True),),
                           primitive=False, context_indices=("run",))
        rule = Rule(Atom("joined", (Variable("run"),)),
                    (Atom("source_tree_observed", (Variable("tree"),)),
                     Atom("runtime_seen", (Variable("run"), Variable("tree")))))
        bundle = Bundle((static, runtime, out), rules=(rule,))
        self.assertNotIn("static-context", codes(bundle))
        self.assertIn("mixed-binding-join", codes(bundle))

    def test_context_free_static_join_is_flagged_only_when_runtime_evidence_is_read(self):
        # Section 29: static_index_current(IX) :- scip_index_tree(IX,T,_,_), source_tree_observed(T).
        # A static-only body may read the context-free claim-time observation; the
        # index/run witness is owed only when the same body reads runtime evidence.
        tree = RelationDecl("scip_index_tree",
                            (Column("index", "digest", True), Column("tree_digest", "digest")),
                            binding="static", context_indices=("index",))
        observed = RelationDecl("source_tree_observed", (Column("tree_digest", "digest"),),
                                binding="static")
        current = RelationDecl("static_index_current", (Column("index", "digest", True),),
                               modality="derived", binding="static", primitive=False,
                               context_indices=("index",))
        static_only = Rule(Atom("static_index_current", (Variable("ix"),)),
                           (Atom("scip_index_tree", (Variable("ix"), Variable("t"))),
                            Atom("source_tree_observed", (Variable("t"),))))
        self.assertEqual(validate_bundle(Bundle((tree, observed, current), rules=(static_only,))), ())
        runtime = RelationDecl("run_tree_seen",
                               (Column("run", "symbol", True), Column("tree_digest", "digest")),
                               context_indices=("run",))
        mixed_out = RelationDecl("current_for_run",
                                 (Column("index", "digest", True), Column("run", "symbol", True)),
                                 modality="derived", primitive=False, context_indices=("index", "run"))
        mixed = Rule(Atom("current_for_run", (Variable("ix"), Variable("run"))),
                     (Atom("scip_index_tree", (Variable("ix"), Variable("t"))),
                      Atom("source_tree_observed", (Variable("t"),)),
                      Atom("run_tree_seen", (Variable("run"), Variable("t")))))
        self.assertIn("mixed-binding-join",
                      codes(Bundle((tree, observed, runtime, mixed_out), rules=(mixed,))))

    def test_mapping_claim_relation_must_match_the_claim_selected_by_id(self):
        actual = RelationDecl("actual_claim",
                              (Column("x", "symbol"), Column("event", "symbol")),
                              modality="claim")
        decoy = RelationDecl("decoy_claim", (Column("x", "symbol"),),
                             modality="claim")
        observed = RelationDecl("observed_event",
                                (Column("x", "symbol"), Column("event", "symbol")))
        mapping = EvidenceMapping("decoy_claim", "observed_event", "support",
                                  bindings=(("x", "x"),), claim_id="actual")
        claim = Claim("actual_claim", (Constant("v"), Constant("event-a")),
                      id="actual")
        self.assertIn("mapping-claim-relation",
                      codes(Bundle((actual, decoy, observed), claims=(claim,),
                                   mappings=(mapping,))))

    def test_support_mapping_cannot_project_away_shared_causal_identities(self):
        for identity in ("event", "notification", "recipient", "attempt"):
            claimed = RelationDecl(f"claimed_{identity}",
                                   (Column("tenant", "symbol", True),
                                    Column(identity, "symbol")),
                                   modality="claim", context_indices=("tenant",))
            observed = RelationDecl(f"observed_{identity}",
                                    (Column("tenant", "symbol", True),
                                     Column(identity, "symbol")),
                                    context_indices=("tenant",))
            mapping = EvidenceMapping(claimed.name, observed.name, "support",
                                      context_indices=("tenant",),
                                      bindings=(("tenant", "tenant"),), claim_id="claim")
            claim = Claim(claimed.name, (Constant("t"), Constant("identity-a")),
                          Context.from_mapping({"tenant": "t"}), id="claim")
            with self.subTest(identity=identity):
                self.assertIn("mapping-coverage",
                              codes(Bundle((claimed, observed), claims=(claim,),
                                           mappings=(mapping,))))

    def test_facts_and_evidence_cannot_seed_derived_or_claim_relations(self):
        derived = RelationDecl("derived", (Column("x", "symbol"),), primitive=False)
        atom = Atom("derived", (Constant("x"),))
        fact_bundle = Bundle((derived,), facts=(atom,))
        evidence_bundle = Bundle((derived,), facts=(atom,), evidence=(Evidence("e", atom, source="test"),))
        self.assertIn("nonprimitive-fact", codes(fact_bundle))
        self.assertIn("nonprimitive-fact", codes(evidence_bundle))

        claimed = RelationDecl("claimed", (Column("x", "symbol"),), modality="claim")
        claim_atom = Atom("claimed", (Constant("x"),))
        producer_bundle = Bundle((claimed,), facts=(claim_atom,),
                                 evidence=(Evidence("producer", claim_atom, source="test"),))
        self.assertIn("producer-authored-claim", codes(producer_bundle))

    def test_unimplemented_mapping_options_are_rejected_not_silently_ignored(self):
        claimed = RelationDecl("mapped_claim", (Column("x", "symbol"),), modality="claim")
        observed = RelationDecl("mapped_observed", (Column("x", "symbol"),))
        claim = Claim("mapped_claim", (Constant("x"),), id="claim")
        for option in ({"required": True}, {"allow_out_of_scope": True}):
            mapping = EvidenceMapping("mapped_claim", "mapped_observed", "support",
                                      bindings=(("x", "x"),), claim_id="claim", **option)
            with self.subTest(option=option):
                self.assertIn("mapping-option",
                              codes(Bundle((claimed, observed), claims=(claim,),
                                           mappings=(mapping,))))

    def test_context_flags_cannot_hide_undeclared_context_columns(self):
        malformed = RelationDecl("hidden", (Column("tenant", "symbol", True), Column("x", "symbol")),
                                 binding="static", context_indices=())
        self.assertIn("context-index", codes(Bundle((malformed,))))

    def test_claim_context_cannot_disagree_with_a_ground_claim_term(self):
        claimed = RelationDecl("scoped_claim", (Column("tenant", "symbol", True),
                                                 Column("x", "symbol")),
                               modality="claim", context_indices=("tenant",))
        claim = Claim("scoped_claim", (Constant("term-tenant"), Constant("x")),
                      Context.from_mapping({"tenant": "context-tenant"}))
        self.assertIn("claim-context", codes(Bundle((claimed,), claims=(claim,))))

    def test_forall_context_column_cannot_be_a_domain_bound_variable(self):
        claimed = RelationDecl(
            "run_member_claim",
            (Column("run", "symbol", True), Column("member", "symbol")),
            modality="claim", context_indices=("run",))
        domain = RelationDecl(
            "run_member_domain",
            (Column("run", "symbol", True), Column("member", "symbol")),
            finite=True, nonempty=True, context_indices=("run",))
        closed = RelationDecl(
            "run_member_domain_closed", (Column("run", "symbol", True),),
            modality="completeness", completes="run_member_domain",
            context_indices=("run",))
        claim = Claim(
            "run_member_claim", (Variable("run"), Variable("member")),
            Context.from_mapping({"run": "run-a"}), "forall",
            "run_member_domain")
        self.assertIn(
            "claim-context",
            codes(Bundle((claimed, domain, closed), claims=(claim,))))

    def test_forall_requires_a_whole_domain_completeness_declaration(self):
        claim_rel = RelationDecl("closed_claim", (Column("tenant", "symbol", True),
                                                   Column("member", "symbol")),
                                  modality="claim", context_indices=("tenant",))
        domain = RelationDecl("closed_domain", (Column("tenant", "symbol", True),
                                                 Column("member", "symbol")),
                              finite=True, nonempty=True, context_indices=("tenant",))
        claim = Claim("closed_claim", (Constant("t"), Variable("member")),
                      Context.from_mapping({"tenant": "t"}), quantifier="forall",
                      domain="closed_domain")
        self.assertIn("forall-closure", codes(Bundle((claim_rel, domain), claims=(claim,))))

        partial = RelationDecl("closed_domain_by_member",
                               (Column("tenant", "symbol", True), Column("member", "symbol")),
                               modality="completeness", completes="closed_domain",
                               context_indices=("tenant",))
        self.assertIn("forall-closure",
                      codes(Bundle((claim_rel, domain, partial), claims=(claim,))))

        complete = RelationDecl("closed_domain_complete", (Column("tenant", "symbol", True),),
                                modality="completeness", completes="closed_domain",
                                context_indices=("tenant",))
        self.assertNotIn("forall-closure",
                         codes(Bundle((claim_rel, domain, complete), claims=(claim,))))

    def test_forall_variables_must_be_present_and_type_compatible_in_domain(self):
        claim_rel = RelationDecl("claim_rel", (Column("member", "symbol"),), modality="claim")
        missing = RelationDecl("missing_domain", (Column("other", "symbol"),), finite=True, nonempty=True)
        wrong = RelationDecl("wrong_domain", (Column("member", "integer"),), finite=True, nonempty=True)
        absent_bundle = Bundle((claim_rel, missing),
                               claims=(Claim("claim_rel", (Variable("member"),), quantifier="forall",
                                             domain="missing_domain"),))
        wrong_bundle = Bundle((claim_rel, wrong),
                              claims=(Claim("claim_rel", (Variable("member"),), quantifier="forall",
                                            domain="wrong_domain"),))
        self.assertIn("forall-binding", codes(absent_bundle))
        self.assertIn("forall-binding", codes(wrong_bundle))

    def test_mixed_binding_join_requires_exact_index_run_witness(self):
        static = RelationDecl("static_site", (Column("index", "digest", True), Column("x", "symbol")),
                              binding="static", context_indices=("index",))
        runtime = RelationDecl("runtime_seen", (Column("run", "symbol", True), Column("x", "symbol")),
                               context_indices=("run",))
        compatible = RelationDecl("index_run", (Column("index", "digest", True), Column("run", "symbol", True)),
                                  modality="compatibility", context_indices=("index", "run"),
                                  compatibility_targets=("static_site", "runtime_seen"),
                                  compatibility_context_indices=("index", "run"))
        out = RelationDecl("out", (Column("x", "symbol"),), primitive=False)
        body = (Atom("static_site", (Variable("i"), Variable("x"))),
                Atom("runtime_seen", (Variable("r"), Variable("x"))))
        self.assertIn("mixed-binding-join", codes(Bundle((static, runtime, compatible, out),
                                                          rules=(Rule(Atom("out", (Variable("x"),)), body),))))
        witnessed = (*body, Atom("index_run", (Variable("i"), Variable("r"))))
        self.assertNotIn("mixed-binding-join", codes(Bundle((static, runtime, compatible, out),
                                                             rules=(Rule(Atom("out", (Variable("x"),)), witnessed),))))
        wrong = (*body, Atom("index_run", (Constant("other", "digest"), Variable("r"))))
        self.assertIn("mixed-binding-join", codes(Bundle((static, runtime, compatible, out),
                                                          rules=(Rule(Atom("out", (Variable("x"),)), wrong),))))
        no_run = RelationDecl("runtime_global", (Column("x", "symbol"),), binding="runtime")
        missing_run_body = (body[0], Atom("runtime_global", (Variable("x"),)))
        self.assertIn("mixed-binding-join", codes(Bundle((static, no_run, compatible, out),
                                                          rules=(Rule(Atom("out", (Variable("x"),)), missing_run_body),))))

        decoy = RelationDecl("index_run_decoy",
                             (Column("index", "digest"), Column("run", "symbol"),
                              Column("basis", "symbol")), modality="compatibility",
                             compatibility_targets=("static_site", "runtime_seen"),
                             compatibility_context_indices=("basis",))
        decoy_body = (*body, Atom("index_run_decoy",
                                  (Variable("i"), Variable("r"), Constant("decoy"))))
        self.assertIn("mixed-binding-join", codes(Bundle((static, runtime, decoy, out),
                                                          rules=(Rule(Atom("out", (Variable("x"),)), decoy_body),))))

    def test_negated_static_relation_cannot_use_runtime_labeled_closure_to_bypass_witness(self):
        static = RelationDecl(
            "static_seen",
            (Column("index", "digest", True), Column("x", "symbol")),
            binding="static", context_indices=("index",))
        mislabeled_closure = RelationDecl(
            "static_seen_closed", (Column("index", "digest", True),),
            modality="completeness", binding="runtime",
            context_indices=("index",), completes="static_seen")
        runtime = RelationDecl(
            "runtime_seen",
            (Column("run", "symbol", True), Column("x", "symbol")),
            context_indices=("run",))
        out = RelationDecl("missing_static", (Column("x", "symbol"),),
                           primitive=False)
        rule = Rule(
            Atom("missing_static", (Variable("x"),)),
            (Atom("runtime_seen", (Variable("run"), Variable("x"))),
             Atom("static_seen_closed", (Variable("index"),)),
             Atom("static_seen", (Variable("index"), Variable("x")),
                  negated=True)))
        result = codes(Bundle((static, mislabeled_closure, runtime, out),
                              rules=(rule,)))
        self.assertIn("completeness-binding", result)
        self.assertIn("mixed-binding-join", result)

    def test_static_assumption_cannot_drop_its_index(self):
        malformed = RelationDecl(
            "authz_symbol__accepted", (Column("symbol", "symbol"),),
            modality="assumption", binding="static")
        self.assertIn("static-context", codes(Bundle((malformed,))))

    def test_forall_must_ground_every_domain_context_position(self):
        claimed = RelationDecl(
            "scoped_member", (Column("tenant", "symbol", True),
                              Column("member", "symbol")),
            modality="claim", context_indices=("tenant",))
        domain = RelationDecl(
            "scoped_domain", (Column("tenant", "symbol", True),
                              Column("run", "symbol", True),
                              Column("member", "symbol")),
            finite=True, nonempty=True, context_indices=("tenant", "run"))
        closed = RelationDecl(
            "scoped_domain_closed", (Column("tenant", "symbol", True),
                                     Column("run", "symbol", True)),
            modality="completeness", completes="scoped_domain",
            context_indices=("tenant", "run"))
        claim = Claim(
            "scoped_member", (Constant("t"), Variable("member")),
            Context.from_mapping({"tenant": "t"}), "forall", "scoped_domain")
        self.assertIn("forall-context",
                      codes(Bundle((claimed, domain, closed), claims=(claim,))))

    def test_rules_cannot_assert_leafless_axioms(self):
        claimed = RelationDecl("axiom", (Column("x", "symbol"),),
                               modality="claim", primitive=False)
        rule = Rule(Atom("axiom", (Constant("v"),)), ())
        self.assertIn("evidence-free-rule",
                      codes(Bundle((claimed,), rules=(rule,))))

    def test_all_aggregate_requires_domain_member_identity_in_source(self):
        source = RelationDecl("source", (Column("tenant", "symbol", True),
                                         Column("holds", "boolean")),
                              context_indices=("tenant",))
        domain = RelationDecl("domain", (Column("tenant", "symbol", True),
                                         Column("member", "symbol")),
                              context_indices=("tenant",), finite=True, nonempty=True)
        closed = RelationDecl("domain_closed", (Column("tenant", "symbol", True),),
                              modality="completeness", context_indices=("tenant",),
                              completes="domain")
        out = RelationDecl("out", (Column("tenant", "symbol", True),),
                           primitive=False, context_indices=("tenant",))
        aggregate = Aggregation("all_holds", "source", ("tenant",), "holds", "all",
                                "domain", "domain_closed")
        rule = Rule(Atom("out", (Variable("tenant"),)),
                    (Atom("source", (Variable("tenant"), Variable("holds"))),
                     Atom("domain", (Variable("tenant"), Variable("member"))),
                     Atom("domain_closed", (Variable("tenant"),))),
                    aggregation=aggregate)
        self.assertIn("aggregation-domain", codes(Bundle((source, domain, closed, out),
                                                           rules=(rule,))))

    def test_path_completeness_witness_cannot_close_another_path(self):
        target = RelationDecl("edge", (Column("index", "digest", True), Column("path", "symbol"), Column("symbol", "symbol")),
                              binding="static", context_indices=("index",))
        closed = RelationDecl("edge_closed", (Column("index", "digest", True), Column("path", "symbol")),
                              modality="completeness", binding="static", context_indices=("index",), completes="edge")
        scope = RelationDecl("scope", target.columns, binding="static", context_indices=("index",))
        out = RelationDecl("missing", target.columns, binding="static", context_indices=("index",), primitive=False)
        rule = Rule(Atom("missing", (Variable("i"), Variable("p"), Variable("s"))),
                    (Atom("scope", (Variable("i"), Variable("p"), Variable("s"))),
                     Atom("edge_closed", (Variable("i"), Constant("other"))),
                     Atom("edge", (Variable("i"), Variable("p"), Variable("s")), negated=True)))
        self.assertIn("missing-completeness", codes(Bundle((target, closed, scope, out), rules=(rule,))))

    def test_frozen_static_schema_contains_the_complete_primitive_contract(self):
        path = Path(__file__).parents[2] / "src/capcov/claims/static/schema_static_v1.json"
        schema = json.loads(path.read_text(encoding="utf-8")); relations = {r["name"]: r for r in schema["relations"]}
        required = {"scip_index", "scip_index_tree", "scip_document", "scip_may_reference",
                    "source_tree_observed", "run_built_from_commit", "scip_documents_closed",
                    "scip_definitions_closed", "scip_references_closed", "static_route_inventory_closed",
                    "static_scope_closed", "static_reachability_closed", "static_scope_leak",
                    "scip_index_comparable", "index_describes_run", "authz_symbol__accepted"}
        self.assertTrue(required <= set(relations))
        self.assertEqual(relations["source_tree_observed"]["context_indices"], [])
        self.assertEqual(relations["run_built_from_commit"]["binding"], "runtime")
        self.assertEqual(relations["authz_symbol__accepted"]["modality"], "assumption")
        self.assertEqual(relations["scip_references_closed"]["completes"], "scip_may_reference")


if __name__ == "__main__": unittest.main()
