"""Strict, evaluator-independent corpus and authoritative IR checks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from .adapter import bundle_payload, canonical_metadata, load_fixture
except ImportError:  # unittest discover -s imports this directory as top-level
    from adapter import bundle_payload, canonical_metadata, load_fixture

ROOT = Path(__file__).parent
CORPUS = ROOT / "corpus"
FIXTURES = sorted(CORPUS.glob("[0-9][0-9]-*.json"))
VERDICTS = {"supported", "refuted", "unresolved", "conflicting"}
OPERATIONAL = {"complete", "invalid-input", "inconsistent-premises", "resource-exhausted", "unsupported-construct", "stale", "out-of-scope"}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class CorpusSchemaTests(unittest.TestCase):
    def test_has_exactly_fourteen_numbered_fixtures(self) -> None:
        self.assertEqual([f"{i:02d}" for i in range(1, 15)], [p.name[:2] for p in FIXTURES])

    def test_schema_has_canonical_columns_types_context_modality_and_polarity(self) -> None:
        schema = load(CORPUS / "schema-v1.json")
        self.assertEqual(1, schema["schema_version"])
        self.assertEqual(1, schema["program_schema_version"])
        self.assertTrue(schema["context_indices"])
        self.assertTrue(schema["types"])
        for name, relation in schema["relations"].items():
            with self.subTest(relation=name):
                self.assertEqual(relation["arg_order"], [c["name"] for c in relation["columns"]])
                self.assertIn(relation["modality"], {"observation", "assumption", "compatibility", "completeness", "claim", "derived"})
                self.assertIn(relation["polarity"], {"positive", "negative"})
                self.assertEqual(set(relation["context_indices"]), {c["name"] for c in relation["columns"] if c["context"]})
                self.assertTrue(set(relation["arg_order"]) <= set(schema["types"]))

    def test_fixture_entries_use_only_declared_relations_and_ordered_args(self) -> None:
        schema = load(CORPUS / "schema-v1.json")
        for path in FIXTURES:
            fixture = load(path)
            with self.subTest(path=path.name):
                self.assertEqual("schema-v1", fixture["schema_ref"])
                self.assertEqual(1, fixture["program_schema_version"])
                self.assertIn("rules", fixture)
                for section in ("claims", "facts", "assumptions"):
                    ids = set()
                    for entry in fixture[section]:
                        self.assertNotIn("predicate", entry)
                        self.assertNotIn(entry["id"], ids)
                        ids.add(entry["id"])
                        self.assertIn(entry["relation"], schema["relations"])
                        declaration = schema["relations"][entry["relation"]]
                        self.assertEqual(declaration["arg_order"], entry["arg_order"])
                        self.assertEqual(entry["args"], [entry["arguments"].get(k) for k in entry["arg_order"]])
                        self.assertEqual(set(fixture["context"]), set(entry["context"]))
                        self.assertIsInstance(entry["provenance"]["depends_on"], list)
                        if section == "assumptions":
                            self.assertRegex(entry["relation"], r"__(accepted|rejected|revoked|inconsistent)$")
                            self.assertNotIn("status", entry)
                        else:
                            self.assertEqual(section == "claims", declaration["modality"] == "claim")
                        if section == "claims":
                            self.assertIn("mappings", entry)
                            self.assertIn("diagnostics", entry)

    def test_adapter_rejects_extra_arguments_and_noncanonical_arg_order(self) -> None:
        fixture = load(CORPUS / "01-correlated-positive.json")
        extra = json.loads(json.dumps(fixture))
        extra["facts"][0]["args"].append("silently-discarded")
        with self.assertRaisesRegex(ValueError, "arity"):
            bundle_payload(extra)
        reordered = json.loads(json.dumps(fixture))
        reordered["facts"][0]["arg_order"] = list(reversed(reordered["facts"][0]["arg_order"]))
        with self.assertRaisesRegex(ValueError, "arg_order"):
            bundle_payload(reordered)

    def test_each_claim_is_explicitly_quantified_and_expectations_are_polarity_complete(self) -> None:
        for path in FIXTURES:
            fixture = load(path)
            leaves = {entry["id"] for entry in fixture["facts"] + fixture["assumptions"]}
            expected = fixture["expected"]["claims"]
            with self.subTest(path=path.name):
                self.assertEqual({entry["id"] for entry in fixture["claims"]}, set(expected))
                for claim in fixture["claims"]:
                    self.assertIn(claim["quantifier"], {"exists", "forall"})
                    if claim["quantifier"] == "forall":
                        self.assertIsNotNone(claim["domain"])
                for outcome in expected.values():
                    self.assertIn(outcome["semantic_verdict"], VERDICTS)
                    self.assertIn(outcome["operational_status"], OPERATIONAL)
                    for key in ("support_leaves", "refutation_leaves", "observed_leaves", "forbidden_leaves", "discrepancies", "missing_premises"):
                        self.assertIn(key, outcome)
                    for key in ("support_leaves", "refutation_leaves", "observed_leaves", "forbidden_leaves"):
                        self.assertTrue(set(outcome[key]) <= leaves)
                    self.assertTrue(set(outcome["support_leaves"]).isdisjoint(outcome["refutation_leaves"]))
                    if outcome["semantic_verdict"] == "supported":
                        self.assertTrue(outcome["support_leaves"] and not outcome["refutation_leaves"])
                    elif outcome["semantic_verdict"] == "refuted":
                        self.assertTrue(outcome["refutation_leaves"] and not outcome["support_leaves"])
                    elif outcome["semantic_verdict"] == "conflicting":
                        self.assertTrue(outcome["support_leaves"] and outcome["refutation_leaves"])
                    else:
                        self.assertFalse(outcome["support_leaves"] or outcome["refutation_leaves"])

    def test_all_fourteen_fixtures_ingest_and_validate_with_real_claims_parser(self) -> None:
        for path in FIXTURES:
            with self.subTest(path=path.name):
                bundle = load_fixture(path)
                self.assertEqual(1, bundle.schema_version)
                self.assertTrue(bundle.relations)

    def test_history_is_relational_and_bounded_interval_is_explicit(self) -> None:
        history = load(CORPUS / "11-compatible-history-sets.json")
        self.assertEqual(2, sum(fact["relation"] == "compatible_history" for fact in history["facts"]))
        bounded = load(CORPUS / "14-bounded-no-resend.json")
        interval = next(f for f in bounded["facts"] if f["id"] == "fact-no-resend-window")
        self.assertIn("interval", interval["arg_order"])
        self.assertIn("assumption-window-closed", bounded["expected"]["claims"]["claim-bounded"]["support_leaves"])

    def test_strict_bundle_round_trip_retains_leaf_identity_and_provenance(self) -> None:
        for path in FIXTURES:
            fixture = load(path)
            bundle = load_fixture(path)
            retained = canonical_metadata(bundle)["semantic_inputs"]
            with self.subTest(path=path.name):
                self.assertEqual(fixture["id"], retained["fixture_id"])
                self.assertEqual(fixture["context"], retained["context"])
                for section in ("facts", "assumptions", "claims"):
                    self.assertEqual(fixture[section], retained[section])
                self.assertNotIn("expected", retained)

    def test_shared_false_assumption_is_an_explicit_dependency_of_both_producers(self) -> None:
        fixture = load(CORPUS / "07-shared-mistaken-assumption.json")
        entries = {entry["id"]: entry for entry in fixture["facts"]}
        self.assertIn("assumption-false", entries["fact-static"]["provenance"]["depends_on"])
        self.assertIn("assumption-false", entries["fact-runtime"]["provenance"]["depends_on"])
        discrepancies = fixture["expected"]["claims"]["claim-delivered"]["discrepancies"]
        self.assertIn({"kind": "shared-assumption", "assumption": "assumption-false"}, discrepancies)


if __name__ == "__main__":
    unittest.main()
