"""Evaluator-independent checks for the static rule pack and its review cases."""
from __future__ import annotations

import hashlib
import json
import re
import unittest

try:
    from .static_rules.adapter import (CASES_DIR, EXPECTED_PATH, FROZEN_PRIMITIVES, PACK_PATH,
                                       bundle_payload, canonical_metadata, case_paths, load_case,
                                       load_pack, pack_bundle, pack_relations, read_json)
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import (CASES_DIR, EXPECTED_PATH, FROZEN_PRIMITIVES, PACK_PATH,
                                      bundle_payload, canonical_metadata, case_paths, load_case,
                                      load_pack, pack_bundle, pack_relations, read_json)

from capcov.claims import canonical_json, validate_bundle

VERDICTS = {"supported", "refuted", "unresolved", "conflicting"}
OPERATIONAL = {"complete", "invalid-input", "inconsistent-premises", "resource-exhausted",
               "unsupported-construct", "stale", "out-of-scope"}
LEAF_KEYS = ("support_leaves", "refutation_leaves", "observed_leaves", "forbidden_leaves")
EXPECTED_KEYS = (*LEAF_KEYS, "semantic_verdict", "operational_status", "discrepancies", "missing_premises")
SECTION_29_DERIVED = {
    "static_edge", "static_root", "static_reaches", "static_reaches_eq", "static_route_handler",
    "static_route_declared_surface", "static_op_owner", "static_path_to_storage", "static_capability_op",
    "static_capability", "static_index_current", "scip_index_stale", "static_file_unindexed",
    "scip_duplicate_definition", "change_reaches", "affected_capability", "runtime_route_without_static",
    "static_route_authorized", "static_route_authorization_gap", "static_route_authorized_closed",
}
COMPLETENESS_PROJECTIONS = {"scip_document_path", "scip_definition_site_at"}
RUNTIME_DERIVED = {"runtime_route_reaches_sql_on_index"}
EVIDENCE_ID = re.compile(r"^(scip|static|runtime):([0-9a-f]{12}|claim-time|run-[0-9]+):([a-z_]+):([0-9a-f]{12})$")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StaticRulePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()

    def test_primitives_are_digest_equal_to_the_frozen_schema(self) -> None:
        frozen = read_json(FROZEN_PRIMITIVES)["relations"]
        self.assertEqual(frozen, self.pack["primitives"])
        self.assertEqual(sha256(canonical(frozen)), sha256(canonical(self.pack["primitives"])))
        # The pack file stores the same pretty-printed bytes as the frozen file.
        frozen_text = json.dumps(frozen, indent=2, sort_keys=True)
        pack_text = json.dumps(self.pack["primitives"], indent=2, sort_keys=True)
        self.assertEqual(frozen_text, pack_text)
        self.assertIn(frozen_text.replace("\n", "\n  ")[1:], PACK_PATH.read_text(encoding="utf-8"))
        self.assertTrue(all(item["primitive"] for item in self.pack["primitives"]))

    def test_derived_relations_cover_section_29_and_the_completeness_projections(self) -> None:
        derived = {item["name"]: item for item in self.pack["derived"]}
        self.assertEqual(set(derived), SECTION_29_DERIVED | COMPLETENESS_PROJECTIONS | RUNTIME_DERIVED)
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        for name in ("scip_documents_closed", "scip_definitions_closed", "static_route_inventory_closed",
                     "static_reachability_closed"):
            self.assertIn(frozen[name]["completes"], derived, name)
        for name, item in derived.items():
            with self.subTest(relation=name):
                self.assertFalse(item["primitive"])
                if name in {"runtime_route_without_static", "runtime_route_reaches_sql_on_index"}:
                    self.assertEqual((item["modality"], item["polarity"], item["binding"]),
                                     (("claim", "negative", "runtime") if name == "runtime_route_without_static"
                                      else ("derived", "positive", "runtime")))
                    self.assertEqual(item["context_indices"],
                                     (["tenant", "surface", "run", "index"]
                                      if name == "runtime_route_without_static"
                                      else ["index", "run", "request", "surface", "tx"]))
                else:
                    self.assertEqual(item["binding"], "static")
                    self.assertEqual(item["context_indices"], ["index"])
        self.assertEqual((derived["static_route_declared_surface"]["finite"],
                          derived["static_route_declared_surface"]["nonempty"]), (True, True))
        self.assertEqual(derived["static_route_authorized_closed"]["modality"], "completeness")
        self.assertEqual(derived["static_route_authorized_closed"]["completes"], "static_route_authorized")
        self.assertEqual({item["modality"] for item in derived.values()} - {"derived", "completeness"}, {"claim"})
        self.assertEqual({name for name, item in derived.items() if item["modality"] == "claim"},
                         {"static_capability", "affected_capability", "runtime_route_without_static",
                          "static_route_authorized"})

    def test_duplicate_definition_rules_are_guarded_by_the_symbol_category(self) -> None:
        # Package symbols (descriptor ending in "/") are defined in every file of
        # a Go package and `local N` symbols are file-scoped; both have category
        # "other", the same class the exporter excludes when deciding whether to
        # withhold scip_definitions_closed / scip_references_closed.
        rules = [rule for rule in self.pack["rules"] if rule["head"]["relation"] == "scip_duplicate_definition"]
        self.assertEqual(len(rules), 2)
        for rule in rules:
            with self.subTest(rule=rule["name"]):
                atoms = [atom for atom in rule["body"] if "relation" in atom]
                self.assertEqual([atom["relation"] for atom in atoms],
                                 ["scip_definition_site", "scip_definition_site", "scip_symbol"])
                symbol_var = atoms[0]["terms"][3]["variable"]
                self.assertEqual(atoms[1]["terms"][3], {"variable": symbol_var})
                self.assertEqual(atoms[2]["terms"][1], {"variable": symbol_var})
                category_var = atoms[2]["terms"][3]["variable"]
                guards = [atom["comparison"] for atom in rule["body"] if "comparison" in atom
                          and atom["comparison"]["left"] == {"variable": category_var}]
                self.assertEqual(guards, [{"left": {"variable": category_var}, "operator": "!=",
                                           "right": {"type": "symbol", "value": "other"}}])

    def test_pack_validates_alone_with_no_issues(self) -> None:
        bundle = pack_bundle(self.pack)
        self.assertEqual(validate_bundle(bundle), ())
        self.assertEqual(len(bundle.rules), len(self.pack["rules"]))
        names = [rule["name"] for rule in self.pack["rules"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(rule.get("aggregation") is None for rule in self.pack["rules"]))

    def test_recursion_is_positive_and_negation_goes_through_witnessed_projections(self) -> None:
        heads = {}
        for rule in self.pack["rules"]:
            heads.setdefault(rule["head"]["relation"], []).append(rule)
        recursive = {name for name, rules in heads.items()
                     if any(atom.get("relation") == name for rule in rules for atom in rule["body"] if "relation" in atom)}
        self.assertEqual(recursive, {"static_reaches", "change_reaches"})
        for name in recursive:
            for rule in heads[name]:
                self.assertFalse(any(atom.get("negated") for atom in rule["body"] if "relation" in atom))
        negated = {(rule["head"]["relation"], atom["relation"]) for rule in self.pack["rules"]
                   for atom in rule["body"] if atom.get("negated")}
        self.assertEqual(negated, {("static_file_unindexed", "scip_document_path"),
                                   ("runtime_route_without_static", "static_route_declared_surface"),
                                   ("static_route_authorization_gap", "static_route_authorized")})
        declarations = {item["name"]: item for item in pack_relations(self.pack)}
        witnesses = {item["completes"] for item in declarations.values() if item["modality"] == "completeness"}
        self.assertTrue({target for _, target in negated} <= witnesses)


class StaticCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()
        self.declarations = {item["name"]: item for item in pack_relations(self.pack)}
        self.paths = case_paths()

    def test_case_numbers_cover_the_control_and_the_nine_adversarial_shapes(self) -> None:
        numbers = sorted({path.name[:2] for path in self.paths})
        self.assertEqual(numbers, [f"{i:02d}" for i in range(0, 10)])
        variants = {path.stem for path in self.paths}
        self.assertIn("02-incomplete-indexer-lying-witness", variants)
        self.assertIn("04-generated-code-accepted", variants)
        self.assertIn("04-generated-code-rejected", variants)
        self.assertIn("09-duplicate-definitions-package-symbol", variants)

    def test_case_conventions(self) -> None:
        for path in self.paths:
            case = read_json(path)
            with self.subTest(case=path.name):
                self.assertEqual(path.stem, case["id"])
                self.assertEqual(case["rule_pack"], "rules-static-v1")
                self.assertEqual(case["provenance"]["kind"], "synthetic")
                self.assertIn("seeded_fault", case["provenance"])
                self.assertTrue(case["review_notes"])
                index = case["context"]["index"]
                self.assertRegex(index, r"^[0-9a-f]{64}$")
                ids = set()
                for kind, section in (("fact", "facts"), ("assumption", "assumptions")):
                    for entry in case[section]:
                        self.assertEqual(entry["kind"], kind)
                        self.assertNotIn(entry["id"], ids)
                        ids.add(entry["id"])
                        declaration = self.declarations[entry["relation"]]
                        self.assertTrue(declaration["primitive"], entry["relation"])
                        self.assertNotEqual(declaration["modality"], "claim")
                        self.assertEqual(entry["arg_order"], [c["name"] for c in declaration["columns"]])
                        values = dict(zip(entry["arg_order"], entry["args"]))
                        self.assertEqual(entry["context"], {name: values[name] for name in declaration["context_indices"]})
                        if "index" in values:
                            self.assertEqual(values["index"], index)
                        self.assertTrue(entry["source"])
                        if kind == "assumption":
                            self.assertRegex(entry["relation"], r"__(accepted|rejected|revoked|inconsistent)$")
                            self.assertEqual(declaration["modality"], "assumption")
                for claim in case["claims"]:
                    self.assertIn(claim["quantifier"], {"exists", "forall"})
                    self.assertEqual(claim["domain"] is not None, claim["quantifier"] == "forall")
                    self.assertIn("mappings", claim)
                    self.assertIn("diagnostics", claim)
                    declaration = self.declarations[claim["relation"]]
                    self.assertEqual(claim["arg_order"], [c["name"] for c in declaration["columns"]])
                    values = {name: value for name, value in zip(claim["arg_order"], claim["args"]) if not isinstance(value, dict)}
                    self.assertEqual(claim["context"], {name: values[name] for name in declaration["context_indices"]})

    def test_evidence_ids_follow_the_section_29_scheme_and_recompute_from_their_rows(self) -> None:
        for path in self.paths:
            case = read_json(path)
            index12 = case["context"]["index"][:12]
            with self.subTest(case=path.name):
                for entry in [*case["facts"], *case["assumptions"]]:
                    match = EVIDENCE_ID.match(entry["id"])
                    self.assertIsNotNone(match, entry["id"])
                    scheme, segment, relation, row12 = match.groups()
                    self.assertEqual(relation, entry["relation"])
                    self.assertEqual(row12, sha256(canonical_json([relation, entry["args"]]))[:12])
                    if self.declarations[relation]["binding"] == "runtime":
                        self.assertEqual(scheme, "runtime")
                        self.assertEqual(segment, dict(zip(entry["arg_order"], entry["args"]))["run"])
                    elif relation.startswith("scip_"):
                        self.assertEqual((scheme, segment), ("scip", index12))
                    elif "index" in entry["arg_order"]:
                        self.assertEqual((scheme, segment), ("static", index12))
                    else:
                        self.assertEqual((scheme, segment), ("static", "claim-time"))

    def test_no_orphan_dependencies_and_every_expected_leaf_is_an_evidence_id(self) -> None:
        for path in self.paths:
            case = read_json(path)
            ids = {entry["id"] for entry in [*case["facts"], *case["assumptions"]]}
            with self.subTest(case=path.name):
                for entry in [*case["facts"], *case["assumptions"]]:
                    for dependency in entry["provenance"]["depends_on"]:
                        self.assertTrue(dependency in ids or dependency.startswith("external:"), dependency)
                for output in case["outputs"]:
                    for key in ("requires_all_evidence", "requires_any_evidence", "excludes_evidence"):
                        self.assertTrue(set(output.get(key, ())) <= ids)
                    for field in output.get("fields", {}).values():
                        if field.get("source") == "evidence":
                            self.assertIn(field["evidence_id"], ids)
                expected = case["expected"]["claims"]
                self.assertEqual(set(expected), {claim["id"] for claim in case["claims"]})
                for claim_id, outcome in expected.items():
                    self.assertEqual(set(outcome), set(EXPECTED_KEYS), claim_id)
                    self.assertIn(outcome["semantic_verdict"], VERDICTS)
                    self.assertIn(outcome["operational_status"], OPERATIONAL)
                    for key in LEAF_KEYS:
                        self.assertTrue(set(outcome[key]) <= ids, (claim_id, key))
                        self.assertEqual(outcome[key], sorted(outcome[key]))
                    self.assertTrue(set(outcome["support_leaves"]).isdisjoint(outcome["refutation_leaves"]))
                    self.assertTrue(set(outcome["support_leaves"]) | set(outcome["refutation_leaves"])
                                    <= set(outcome["observed_leaves"]))
                    verdict = outcome["semantic_verdict"]
                    if verdict == "supported":
                        self.assertTrue(outcome["support_leaves"] and not outcome["refutation_leaves"])
                    elif verdict == "refuted":
                        self.assertTrue(outcome["refutation_leaves"] and not outcome["support_leaves"])
                    elif verdict == "conflicting":
                        self.assertTrue(outcome["support_leaves"] and outcome["refutation_leaves"])
                    else:
                        self.assertFalse(outcome["support_leaves"] or outcome["refutation_leaves"])
                        self.assertTrue(outcome["missing_premises"], claim_id)
                    for item in outcome["missing_premises"]:
                        self.assertIn(item["relation"], self.declarations)

    def test_support_that_uses_forbidden_evidence_is_only_admitted_as_a_labelled_seeded_fault(self) -> None:
        for path in self.paths:
            case = read_json(path)
            with self.subTest(case=path.name):
                overlap = any(set(outcome["support_leaves"]) & set(outcome["forbidden_leaves"])
                              for outcome in case["expected"]["claims"].values())
                if overlap:
                    self.assertIsNotNone(case["provenance"]["seeded_fault"])
                if case["provenance"]["seeded_fault"] is None:
                    self.assertFalse(overlap)
        lying = read_json(CASES_DIR / "02-incomplete-indexer-lying-witness.json")
        gap = lying["expected"]["claims"]["claim-authz-gap"]
        self.assertEqual(gap["semantic_verdict"], "supported")
        self.assertTrue(set(gap["support_leaves"]) & set(gap["forbidden_leaves"]))
        honest = read_json(CASES_DIR / "02-incomplete-indexer.json")
        self.assertEqual(honest["expected"]["claims"]["claim-authz-gap"]["semantic_verdict"], "unresolved")

    def test_expected_table_duplicates_each_case(self) -> None:
        table = read_json(EXPECTED_PATH)
        self.assertEqual(table["schema_version"], 1)
        self.assertEqual(table["rule_pack"], "rules-static-v1")
        self.assertEqual({"exists": "derivational", "forall": "bounded-history-model"},
                         table["evaluation_basis_by_quantifier"])
        self.assertEqual(set(table["cases"]), {path.stem for path in self.paths})
        for path in self.paths:
            case = read_json(path)
            with self.subTest(case=path.name):
                self.assertEqual(case["expected"], table["cases"][case["id"]])

    def test_every_case_ingests_with_validation_and_retains_its_inputs(self) -> None:
        for path in self.paths:
            case = read_json(path)
            with self.subTest(case=path.name):
                bundle = load_case(path, self.pack)
                self.assertEqual(validate_bundle(bundle), ())
                self.assertEqual(len(bundle.rules), len(self.pack["rules"]))
                retained = canonical_metadata(bundle)["semantic_inputs"]
                self.assertEqual(case["id"], retained["case_id"])
                for section in ("facts", "assumptions", "claims"):
                    self.assertEqual(case[section], retained[section])
                self.assertNotIn("expected", retained)

    def test_adapter_rejects_extra_arguments_and_wrong_context(self) -> None:
        case = read_json(CASES_DIR / "00-go-app-control.json")
        extra = json.loads(json.dumps(case))
        extra["facts"][0]["args"].append("silently-discarded")
        with self.assertRaisesRegex(ValueError, "arity"):
            bundle_payload(extra, self.pack)
        wrong = json.loads(json.dumps(case))
        wrong["facts"][0]["context"] = {"index": "0" * 64}
        with self.assertRaisesRegex(ValueError, "context"):
            bundle_payload(wrong, self.pack)

    def test_each_case_names_a_discriminating_semantic_control(self) -> None:
        table = read_json(EXPECTED_PATH)["cases"]
        control = table["00-go-app-control"]["claims"]
        self.assertEqual({claim: control[claim]["semantic_verdict"] for claim in control}, {
            "claim-cap-jobs-read": "supported", "claim-cap-audit-create": "supported",
            "claim-route-authorized": "supported", "claim-all-routes-authorized": "supported",
            "claim-affected-by-repo-get": "supported", "claim-runtime-route-gap": "refuted"})
        self.assertEqual(table["01-unresolved-symbol"]["claims"]["claim-callee-node"]["missing_premises"][0]["relation"], "scip_symbol_node")
        self.assertEqual(table["03-dynamic-dispatch"]["claims"]["claim-cap-jobs-read"]["discrepancies"][0]["kind"], "blind-spot-on-path")
        self.assertEqual(table["04-generated-code"]["claims"]["claim-all-routes-authorized"]["discrepancies"][0]["kind"], "handler-in-generated-code")
        self.assertEqual(table["04-generated-code-accepted"]["claims"]["claim-all-routes-authorized"]["discrepancies"], [])
        self.assertEqual(table["04-generated-code-rejected"]["claims"]["claim-all-routes-authorized"]["operational_status"], "out-of-scope")
        self.assertEqual(table["05-stale-index"]["claims"]["claim-cap-jobs-read"]["operational_status"], "stale")
        self.assertEqual(table["06-synthesized-enclosing"]["claims"]["claim-cap-jobs-read"]["discrepancies"][0]["kind"], "caller-attribution-synthesized")
        self.assertEqual(table["07-constructor-as-type-reference"]["claims"]["claim-reaches-job-type"]["semantic_verdict"], "unresolved")
        self.assertEqual(table["08-module-scope-reference"]["claims"]["claim-init-reaches-storage"]["semantic_verdict"], "unresolved")
        self.assertEqual(table["09-duplicate-definitions"]["claims"]["claim-cap-jobs-read"]["discrepancies"][0]["kind"], "ambiguous-definition")
        variant = table["09-duplicate-definitions-package-symbol"]["claims"]
        self.assertEqual(variant["claim-duplicate"]["semantic_verdict"], "unresolved")
        self.assertEqual(variant["claim-duplicate"]["missing_premises"][0]["relation"], "scip_symbol")
        self.assertEqual(variant["claim-cap-jobs-read"]["discrepancies"], [])
        self.assertEqual(variant["claim-authz-gap"]["semantic_verdict"], "supported")


if __name__ == "__main__":
    unittest.main()
