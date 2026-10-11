"""Evaluator-independent proof that corpus semantics compile into policy IR."""
import json
import unittest
from pathlib import Path

from .adapter import ROOT, bundle_payload, load_fixture
from capcov.claims import (Atom, Bundle, Claim, Column, Comparison, Constant, Context, Evidence, EvidenceEffect,
                           RelationDecl, Rule, Variable, assert_valid, bundle_from_json,
                           canonical_json, relevant_evidence_ids, schema_digest, render_outputs,
                           VerifiedProofEvidence)


class EvidencePolicyCompilationTests(unittest.TestCase):
    def test_every_fixture_has_authoritative_evidence_and_real_rules(self):
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            fixture = json.loads(path.read_text())
            bundle = load_fixture(path)
            with self.subTest(case=fixture["id"]):
                self.assertTrue(bundle.evidence)
                self.assertTrue(bundle.rules or bundle.diagnostics or bundle.mappings)
                self.assertTrue(bundle.mappings)
                self.assertEqual({e.id for e in bundle.evidence},
                                 {x["id"] for x in fixture["facts"] + fixture["assumptions"]})
                self.assertNotIn('"expected"', canonical_json(bundle))

    def test_mapping_has_explicit_support_and_refutation_effects(self):
        effects = set()
        for path in ROOT.glob("[0-9][0-9]-*.json"):
            effects.update(mapping.effect for mapping in load_fixture(path).mappings)
        self.assertIn(EvidenceEffect.SUPPORT, effects)
        self.assertIn(EvidenceEffect.REFUTATION, effects)

    def test_provenance_context_and_dependencies_are_not_metadata_only(self):
        fixture = json.loads((ROOT / "07-shared-mistaken-assumption.json").read_text())
        bundle = load_fixture(ROOT / "07-shared-mistaken-assumption.json")
        records = {e.id: e for e in bundle.evidence}
        self.assertIn("assumption-false", records["fact-static"].depends_on)
        self.assertIn("assumption-false", records["fact-runtime"].depends_on)
        self.assertEqual(records["fact-static"].source, "static")
        self.assertEqual(records["fact-static"].context.as_dict(),
                         {"index": "index-07"})
        self.assertEqual(records["fact-runtime"].context.as_dict()["tenant"],
                         fixture["context"]["tenant"])

    def test_round_trip_is_canonical_and_expected_table_is_not_ingested(self):
        path = ROOT / "09-support-and-refutation.json"
        payload = bundle_payload(json.loads(path.read_text()))
        first = bundle_from_json(payload, validate=True)
        second = bundle_from_json(json.loads(canonical_json(first)), validate=True)
        self.assertEqual(schema_digest(first), schema_digest(second))
        self.assertNotIn("expected", payload)

    def test_domain_rules_require_conjunction_and_preserve_scope(self):
        positive = load_fixture(ROOT / "01-correlated-positive.json")
        rule = next(r for r in positive.rules if r.head.relation == "notification_delivery_terminal")
        self.assertEqual({a.relation for a in rule.body}, {"http_save_succeeded", "issue_changed_observed", "smtp_accepted", "sql_terminal_state"})
        scoped = load_fixture(ROOT / "11-compatible-history-sets.json")
        self.assertTrue(all(any(a.relation == "compatible_history" for a in rule.body) for rule in scoped.rules if rule.head.relation == "capability_holds_in_all_compatible_histories"))
        out_of_scope = load_fixture(ROOT / "12-unexpected-runtime-surface.json")
        self.assertFalse(any(rule.head.relation == "model_complete" for rule in out_of_scope.rules))
        self.assertTrue(any(mapping.effect == EvidenceEffect.OBSERVATION for mapping in out_of_scope.mappings))
        history = json.loads((ROOT / "11-compatible-history-sets.json").read_text())
        compatible = next(x for x in history["facts"] if x["relation"] == "compatible_history")
        self.assertEqual(["tenant", "run", "history", "capability", "outcome", "holds"], compatible["arg_order"])
        self.assertIsInstance(compatible["arguments"]["holds"], bool)
        aggregate = load_fixture(ROOT / "11-compatible-history-sets.json")
        self.assertTrue(any(rule.aggregation and rule.aggregation.operator == "all" for rule in aggregate.rules))
        self.assertIn("history_holds", {relation.name for relation in aggregate.relations})
        sql = load_fixture(ROOT / "10-revoked-assumption-alternative.json")
        self.assertTrue(any(rule.name == "row_requires_independent_current_sql" for rule in sql.rules))
        self.assertTrue(all(p.get("scope") != "global" for rule in json.loads((ROOT / "10-revoked-assumption-alternative.json").read_text())["rules"] for p in rule["premises"]))
        terminal = load_fixture(ROOT / "13-acceptance-sql-ack-failure.json")
        self.assertTrue(any(rule.head.relation == "notification_delivery_terminal" for rule in terminal.rules))
        self.assertTrue(any(d.claim_id == "claim-terminal" and d.trigger_relation == "sql_ack_failed" for d in terminal.diagnostics))

    def test_history_aggregation_requires_explicit_closed_domain_witness(self):
        bundle = load_fixture(ROOT / "11-compatible-history-sets.json")
        aggregate = [rule for rule in bundle.rules if rule.aggregation]
        self.assertEqual({rule.aggregation.closure_witness for rule in aggregate}, {"compatible_history_domain_closed"})
        self.assertTrue(all(any(atom.relation == "compatible_history_domain_closed" for atom in rule.body) for rule in aggregate))
        payload = bundle_payload(json.loads((ROOT / "11-compatible-history-sets.json").read_text()))
        payload["facts"] = [fact for fact in payload["facts"] if fact["relation"] != "compatible_history_domain_closed"]
        payload["evidence"] = [fact for fact in payload["evidence"] if fact["relation"] != "compatible_history_domain_closed"]
        missing = bundle_from_json(payload, validate=True)
        self.assertTrue(all(rule.aggregation.closure_witness == "compatible_history_domain_closed" for rule in missing.rules if rule.aggregation))

    def test_all_fixture_bundles_canonically_reingest(self):
        for path in ROOT.glob("[0-9][0-9]-*.json"):
            with self.subTest(case=path.stem):
                first = load_fixture(path)
                second = bundle_from_json(json.loads(canonical_json(first)), validate=True)
                self.assertEqual(schema_digest(first), schema_digest(second))

    def test_declared_outputs_cover_expected_diagnostic_payloads(self):
        expected = json.loads((ROOT / "expected.json").read_text())["cases"]
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            bundle = load_fixture(path)
            by_claim = {}
            for output in bundle.outputs:
                by_claim.setdefault(output.claim_id, []).append(output)
            for claim_id, table in expected[path.stem]["claims"].items():
                declared = by_claim.get(claim_id, [])
                observed = {o.evidence_id for o in declared if o.kind.value == "observed"}
                forbidden = {o.evidence_id for o in declared if o.kind.value == "forbidden"}
                self.assertTrue(set(table["observed_leaves"]).issubset(observed), path.stem)
                self.assertTrue(set(table["forbidden_leaves"]).issubset(forbidden), path.stem)
                discrepancies = [dict(o.fields).get("kind").value for o in declared if o.kind.value == "discrepancy" and "kind" in dict(o.fields)]
                self.assertTrue({d["kind"] for d in table["discrepancies"]}.issubset(discrepancies), path.stem)
                missing = {(dict(o.fields).get("reason").value, o.relation) for o in declared if o.kind.value == "missing_premise" and "reason" in dict(o.fields)}
                self.assertTrue({(d["reason"], d["relation"]) for d in table["missing_premises"]}.issubset(missing), path.stem)

    def test_malformed_output_templates_are_rejected_structurally(self):
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["outputs"] = [{"kind": "observed", "claim_id": "claim-terminal-delivery", "evidence_id": "missing"}]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)

    def test_output_triggers_are_causal_and_arrays_are_exact(self):
        fixture = json.loads((ROOT / "02-surface-mismatch.json").read_text())
        bundle = load_fixture(ROOT / "02-surface-mismatch.json")
        discrepancy = next(o for o in bundle.outputs if o.kind.value == "discrepancy")
        self.assertEqual(
            dict(discrepancy.fields)["surfaces"].value,
            ("route-a", "route-b"),
        )
        self.assertEqual(set(discrepancy.requires_all_evidence), {"fact-route-a-static", "fact-route-b-runtime"})
        mutated = bundle_payload(fixture)
        mutated["outputs"][2]["requires_all_evidence"] = ["unrelated-evidence"]
        with self.assertRaises(ValueError): bundle_from_json(mutated, validate=True)
        mutated = bundle_payload(fixture)
        mutated["outputs"][2]["requires_all_evidence"] = ["fact-route-a-static"]
        # A weakened trigger is still syntactically valid, but no longer
        # claims the two-evidence discrepancy; the renderer must not emit it.
        weakened = bundle_from_json(mutated, validate=True)
        self.assertNotEqual(set(weakened.outputs[2].requires_all_evidence), set(discrepancy.requires_all_evidence))
        active = {"fact-route-a-static", "fact-route-b-runtime"}
        self.assertTrue(any(item.get("fields", {}).get("surfaces") == ["route-a", "route-b"] for item in render_outputs(bundle, "claim-effect", active, VerifiedProofEvidence.from_bundle(bundle, "claim-effect", active), "unresolved", {"same_surface"})))
        self.assertFalse(any(item.get("fields", {}).get("surfaces") == ["route-a", "route-b"] for item in render_outputs(bundle, "claim-effect", {"fact-route-a-static"}, VerifiedProofEvidence.from_bundle(bundle, "claim-effect", {"fact-route-a-static"}), "unresolved", {"same_surface"})))
        self.assertFalse(any(item.get("fields", {}).get("surfaces") == ["route-a", "route-b"] for item in render_outputs(bundle, "claim-effect", active, VerifiedProofEvidence.from_bundle(bundle, "claim-effect", active), "supported", {"same_surface"})))
        wrong_context = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        wrong_context["evidence"][0]["context"]["tenant"] = "other-tenant"
        wrong = bundle_from_json(wrong_context, validate=False)
        output_ids = {item.get("evidence_id") for item in render_outputs(wrong, "claim-terminal-delivery", {"fact-http-save"}, VerifiedProofEvidence.from_bundle(wrong, "claim-terminal-delivery", {"fact-http-save"}), "supported", set())}
        self.assertNotIn("fact-http-save", output_ids)

    def test_proof_certificate_trust_boundary(self):
        bundle = load_fixture(ROOT / "01-correlated-positive.json")
        active = {record.id for record in bundle.evidence}
        with self.assertRaises(TypeError):
            render_outputs(bundle, "claim-terminal-delivery", active, active, "supported", set())
        wrong_claim = VerifiedProofEvidence.from_bundle(bundle, "claim-terminal-delivery", {"fact-http-save"})
        with self.assertRaises(ValueError):
            render_outputs(bundle, "other-claim", active, wrong_claim, "supported", set())
        with self.assertRaises(ValueError):
            render_outputs(bundle, "claim-terminal-delivery", active, VerifiedProofEvidence("claim-terminal-delivery", frozenset({"unknown"})), "supported", set())

    def test_rendered_output_shapes_match_all_four_expected_payload_classes(self):
        expected = json.loads((ROOT / "expected.json").read_text())["cases"]
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            bundle = load_fixture(path)
            active = {record.id for record in bundle.evidence}
            for claim_id, table in expected[path.stem]["claims"].items():
                rendered = render_outputs(bundle, claim_id, active, VerifiedProofEvidence.from_bundle(bundle, claim_id, active), table["semantic_verdict"], {item["relation"] for item in table["missing_premises"]})
                observed = {item["evidence_id"] for item in rendered if item["kind"] == "observed"}
                forbidden = {item["evidence_id"] for item in rendered if item["kind"] == "forbidden"}
                discrepancies = [item.get("fields", {}) for item in rendered if item["kind"] == "discrepancy"]
                missing = [{**item.get("fields", {}), "relation": item.get("relation")} for item in rendered if item["kind"] == "missing_premise"]
                self.assertEqual(observed, set(table["observed_leaves"]), path.stem)
                self.assertEqual(forbidden, set(table["forbidden_leaves"]), path.stem)
                self.assertEqual(discrepancies, table["discrepancies"], path.stem)
                self.assertEqual(missing, table["missing_premises"], path.stem)

    def test_renamed_equivalent_program_preserves_rendered_shape(self):
        fixture = json.loads((ROOT / "02-surface-mismatch.json").read_text())
        original_payload = bundle_payload(fixture)
        renamed = json.loads(json.dumps(original_payload))
        replacements = {
            "claim-effect": "claim-renamed",
            "fact-route-a-static": "proof-alpha",
            "fact-route-b-runtime": "proof-beta",
            "static_route_exists": "renamed_static_route",
            "runtime_route_observed": "renamed_runtime_route",
        }
        def rewrite(value):
            if isinstance(value, dict): return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list): return [rewrite(item) for item in value]
            return replacements.get(value, value)
        renamed = rewrite(renamed)
        first = bundle_from_json(original_payload, validate=True)
        second = bundle_from_json(renamed, validate=True)
        active_first = {record.id for record in first.evidence}
        active_second = {record.id for record in second.evidence}
        shape = lambda values: tuple((item["kind"], tuple(sorted(item.get("fields", {}).keys()))) for item in values)
        self.assertEqual(shape(render_outputs(first, "claim-effect", active_first, VerifiedProofEvidence.from_bundle(first, "claim-effect", active_first), "unresolved", {"same_surface"})), shape(render_outputs(second, "claim-renamed", active_second, VerifiedProofEvidence.from_bundle(second, "claim-renamed", active_second), "unresolved", {"same_surface"})))
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["outputs"] = [{"kind": "missing_premise", "claim_id": "claim-terminal-delivery", "relation": "smtp_accepted", "fields": {"x": {"source": "constant", "type": "boolean", "value": "not-bool"}}}]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["outputs"] = [{"kind": "discrepancy", "claim_id": "claim-terminal-delivery", "fields": {"x": {"source": "evidence", "evidence_id": "fact-http-save", "column": "missing", "type": "symbol"}}}]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)

    def test_mixed_history_false_rows_are_observation_discrepancies(self):
        bundle = load_fixture(ROOT / "11-compatible-history-sets.json")
        self.assertTrue(all(mapping.effect == EvidenceEffect.OBSERVATION for mapping in bundle.mappings if mapping.evidence_relation == "compatible_history"))
        effects = {diagnostic.effect for diagnostic in bundle.diagnostics if diagnostic.trigger_relation == "compatible_history"}
        self.assertEqual({EvidenceEffect.OBSERVATION}, effects)

    def test_a_comparison_in_a_rule_body_is_not_mistaken_for_an_atom(self):
        # relevant_evidence_ids scans the bodies of the rules that conclude the
        # claim's relation; a Comparison names no relation, and reading one as an
        # atom used to raise AttributeError on the first claim stated over a rule
        # with an arithmetic guard.
        observation = RelationDecl("status_seen", (Column("run", "symbol", True), Column("status", "unsigned")),
                                   modality="observation", binding="runtime", primitive=True,
                                   context_indices=("run",), producer_classes=("harness",))
        derived = RelationDecl("late_status", (Column("run", "symbol", True), Column("status", "unsigned")),
                               modality="claim", binding="runtime", primitive=False, context_indices=("run",))
        rule = Rule(Atom("late_status", (Variable("Run"), Variable("St"))),
                    (Atom("status_seen", (Variable("Run"), Variable("St"))),
                     Comparison(Variable("St"), ">", Constant(399, "unsigned"))),
                    name="late_status")
        atom = Atom("status_seen", (Constant("run-1", "symbol"), Constant(404, "unsigned")))
        evidence = Evidence("harness:1:status_seen:1", atom, Context.from_mapping({"run": "run-1"}), "harness x")
        claim = Claim("late_status", (Constant("run-1", "symbol"), Constant(404, "unsigned")),
                      Context.from_mapping({"run": "run-1"}), id="claim-late")
        bundle = Bundle((observation, derived), rules=(rule,), facts=(atom,), evidence=(evidence,), claims=(claim,))
        assert_valid(bundle)
        self.assertEqual(relevant_evidence_ids(bundle, "claim-late", {evidence.id}, {evidence.id}), {evidence.id})
        # nothing else makes it relevant: without the proof it is not in scope
        self.assertEqual(relevant_evidence_ids(bundle, "claim-late", {evidence.id}), set())
        # and a rule whose body names another relation does not pull it in
        other = Rule(Atom("late_status", (Variable("Run"), Variable("St"))),
                     (Atom("late_status", (Variable("Run"), Variable("St"))),
                      Comparison(Variable("St"), ">", Constant(399, "unsigned"))), name="other")
        unrelated = Bundle((observation, derived), rules=(other,), facts=(atom,), evidence=(evidence,), claims=(claim,))
        self.assertEqual(relevant_evidence_ids(unrelated, "claim-late", {evidence.id}, {evidence.id}), set())

    def test_malformed_evidence_policy_is_rejected(self):
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["evidence"][0]["depends_on"] = ["missing-id"]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)

    def test_claim_ids_are_required_only_for_evidence_policy_bundles(self):
        relation = RelationDecl("legacy_claim", (Column("value", "symbol"),), modality="claim")
        assert_valid(Bundle((relation,), claims=(Claim("legacy_claim", (Constant("v", "symbol"),)),)))
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["diagnostics"] = [{"trigger_relation": "smtp_accepted", "effect": "support", "operational_status": "not-a-status"}]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["claims"][0].pop("id")
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["mappings"][0]["bindings"] = [["not-a-column", "tenant"]]
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["evidence"][0]["context"]["tenant"] = "wrong-tenant"
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "01-correlated-positive.json").read_text()))
        payload["diagnostic_policy"]["revocation"] = "not-an-effect"
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "12-unexpected-runtime-surface.json").read_text()))
        payload["diagnostics"][0]["predicate"]["column"] = "missing"
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)
        payload = bundle_payload(json.loads((ROOT / "12-unexpected-runtime-surface.json").read_text()))
        payload["diagnostics"][0]["predicate"]["value"] = "route-known"
        with self.assertRaises(ValueError): bundle_from_json(payload, validate=True)


if __name__ == "__main__":
    unittest.main()
