"""Evaluator-independent checks for the replay rule pack and its review cases (Phase 4, judge side)."""
from __future__ import annotations

import hashlib
import json
import re
import unittest

try:
    from .replay_rules import cases as case_builder
    from .replay_rules.adapter import (CASES_DIR, EXPECTED_PATH, FROZEN_PRIMITIVES, PACK_PATH, REJECTED_DIR,
                                       REJECTED_PATH, bundle_payload, canonical_metadata, case_paths,
                                       load_case, load_pack, pack_bundle, pack_relations, read_json)
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules import cases as case_builder
    from replay_rules.adapter import (CASES_DIR, EXPECTED_PATH, FROZEN_PRIMITIVES, PACK_PATH, REJECTED_DIR,
                                      REJECTED_PATH, bundle_payload, canonical_metadata, case_paths,
                                      load_case, load_pack, pack_bundle, pack_relations, read_json)

from capcov.claims import BundleIngestionError, canonical_json, validate_bundle
from capcov.claims.replay import replay_facts

VERDICTS = {"supported", "refuted", "unresolved", "conflicting"}
OPERATIONAL = {"complete", "invalid-input", "inconsistent-premises", "resource-exhausted",
               "unsupported-construct", "stale", "out-of-scope"}
LEAF_KEYS = ("support_leaves", "refutation_leaves", "observed_leaves", "forbidden_leaves")
EXPECTED_KEYS = (*LEAF_KEYS, "semantic_verdict", "operational_status", "discrepancies", "missing_premises")
STUBS = {"mutant_killed_in", "op_qualified_rt"}
DERIVED = {
    "replay_run_current", "replay_run_stale", "requested", "requested_closed", "replayed",
    "php_model_agree", "php_model_disagree", "go_model_agree", "go_model_disagree", "op_exercised",
    "php_observed", "go_observed", "php_observed_closed", "go_observed_closed", "post_state_gap",
    "post_state_any", "post_state_gap_closed", "kill_closure_gap_any", "kill_gap_closed",
    "undeclared_write", "surviving_mutant", "op_has_surviving_mutant", "op_surviving_closed",
    "corpus_constrains", "kill_closure_gap", "php_disagree_any", "go_disagree_any", "undeclared_any",
    "php_disagreement_closed", "go_disagreement_closed", "undeclared_writes_closed", "op_qualified",
    "model_scope_excluded", "model_scope_excluded_closed", "exclusion_applied",
    # ordering, repeat-delete and cross-run stability (v1 ordering addendum)
    # the learn campaign (v1 learn addendum)
    "learn_predicted", "learn_counterexample", "learn_counterexample_any",
    "learn_counterexample_closed", "learn_consistent", "learn_unmodeled_any",
    "learn_unmodeled_gate_closed",
    "effect_order_violation", "effect_order_respected", "effect_order_any", "effect_order_exercised",
    "effect_order_closed", "delete_target", "earlier_delete", "earlier_delete_closed",
    "first_delete_committed", "repeat_delete",
    "repeat_delete_has_effect", "repeat_delete_effects_closed", "repeat_delete_not_found",
    "repeat_delete_violation", "repeat_delete_any", "repeat_delete_closed",
    "oracle_unstable", "oracle_unstable_closed", "oracle_stable",
}
COMPLETENESS = {"requested_closed": "requested", "op_surviving_closed": "op_has_surviving_mutant",
                "php_disagreement_closed": "php_disagree_any", "go_disagreement_closed": "go_disagree_any",
                "undeclared_writes_closed": "undeclared_any", "php_observed_closed": "php_observed",
                "go_observed_closed": "go_observed", "post_state_gap_closed": "post_state_any",
                "kill_gap_closed": "kill_closure_gap_any", "model_scope_excluded_closed": "model_scope_excluded",
                "effect_order_closed": "effect_order_any", "repeat_delete_effects_closed": "repeat_delete_has_effect",
                "repeat_delete_closed": "repeat_delete_any", "oracle_unstable_closed": "oracle_unstable",
                "earlier_delete_closed": "earlier_delete",
                # the learn campaign (v1 learn addendum)
                "learn_counterexample_closed": "learn_counterexample_any",
                "learn_unmodeled_gate_closed": "learn_unmodeled_any"}
EVIDENCE_ID = re.compile(
    r"^(replay|php|go|shen|mut|reviewer|modelcheck):([0-9a-f]{12}|claim-time):([a-z_]+):([0-9a-f]{12})$")
# rows a judge always adds at claim time (never exported from a receipt)
CLAIM_TIME_RELATIONS = {"run_nonce_observed", "snapshot_observed", "model_observed", "op_declared"}
# a checker certificate normally rides in the receipt, but one naming another model can
# only reach the judge at claim time (the exporter calls it stale): case 28
CLAIM_TIME_ADMITTED = CLAIM_TIME_RELATIONS | {"model_well_formed"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atoms(rule):
    return [atom for atom in rule["body"] if "relation" in atom]


class ReplayRulePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()
        self.declarations = {item["name"]: item for item in pack_relations(self.pack)}

    def test_primitives_are_digest_equal_to_the_frozen_schema(self) -> None:
        frozen = read_json(FROZEN_PRIMITIVES)["relations"]
        self.assertEqual(frozen, self.pack["primitives"])
        self.assertEqual(sha256(canonical(frozen)), sha256(canonical(self.pack["primitives"])))
        frozen_text = json.dumps(frozen, indent=2, sort_keys=True)
        self.assertEqual(frozen_text, json.dumps(self.pack["primitives"], indent=2, sort_keys=True))
        self.assertIn(frozen_text.replace("\n", "\n  ")[1:], PACK_PATH.read_text(encoding="utf-8"))
        self.assertTrue(all(item["primitive"] for item in self.pack["primitives"]))
        self.assertEqual(self.pack["supplementary_primitives"], [])
        self.assertEqual(self.pack["primitives_source"],
                         "packages/capabilities/src/capcov/claims/replay/schema_replay_v1.json")

    def test_stubs_are_byte_identical_to_the_exporter_declarations(self) -> None:
        by_name = {decl.name: decl for decl in replay_facts._DERIVED_TARGET_DECLS}
        self.assertEqual(set(by_name), STUBS)
        self.assertEqual(set(replay_facts.STUB_RELATIONS), STUBS)
        for item in self.pack["derived"]:
            if item["name"] in STUBS:
                self.assertEqual(replay_facts._relation_from_json(item), by_name[item["name"]])
        # index_describes_replay is a frozen primitive: identical by the primitives check above
        exported = {decl.name: decl for decl in replay_facts.replay_relations()}
        for name, raw in self.declarations.items():
            if name in exported:
                self.assertEqual(replay_facts._relation_from_json(raw), exported[name], name)
        self.assertEqual(self.pack["derived"][:2], [item for item in self.pack["derived"] if item["name"] in STUBS])

    def test_derived_relations_and_their_shapes(self) -> None:
        derived = {item["name"]: item for item in self.pack["derived"]}
        self.assertEqual(set(derived), DERIVED | STUBS)
        for name, item in derived.items():
            with self.subTest(relation=name):
                self.assertFalse(item["primitive"])
                self.assertEqual(item["binding"], "runtime")
                self.assertEqual(item["producer_classes"], [])
                if name in {"op_qualified", "op_qualified_rt"}:
                    self.assertEqual(item["context_indices"], ["index", "run"])
                elif name in {"model_scope_excluded", "model_scope_excluded_closed"}:
                    self.assertEqual(item["context_indices"], ["model"])
                else:
                    self.assertEqual(item["context_indices"], ["run"])
        self.assertEqual({name for name, item in derived.items() if item["modality"] == "claim"},
                         {"op_qualified", "repeat_delete_not_found", "learn_consistent"})
        self.assertEqual({name: item["completes"] for name, item in derived.items()
                          if item["modality"] == "completeness"}, COMPLETENESS)
        self.assertEqual({item["modality"] for item in derived.values()}, {"derived", "completeness", "claim"})
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        self.assertEqual(frozen["mutant_kills_closed"]["completes"], "mutant_killed_in")
        self.assertIn("op_qualified_rt", frozen["index_describes_replay"]["compatibility_targets"])

    def test_pack_validates_alone_with_no_issues(self) -> None:
        bundle = pack_bundle(self.pack)
        self.assertEqual(validate_bundle(bundle), ())
        self.assertEqual(len(bundle.rules), len(self.pack["rules"]))
        names = [rule["name"] for rule in self.pack["rules"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(rule.get("aggregation") is None for rule in self.pack["rules"]))
        heads = {rule["head"]["relation"] for rule in self.pack["rules"]}
        self.assertEqual(heads, DERIVED | STUBS)

    def test_every_model_and_run_join_carries_a_positive_model_describes_run_binding_both(self) -> None:
        for rule in self.pack["rules"]:
            model_terms, run_terms, witnesses = set(), set(), []
            for atom in _atoms(rule):
                decl = self.declarations[atom["relation"]]
                names = [column["name"] for column in decl["columns"]]
                if atom["relation"] == "model_describes_run":
                    if not atom.get("negated"):
                        witnesses.append((canonical(atom["terms"][0]), canonical(atom["terms"][1])))
                    continue
                if "model" in decl["context_indices"]:
                    model_terms.add(canonical(atom["terms"][names.index("model")]))
                if "run" in decl["context_indices"]:
                    run_terms.add(canonical(atom["terms"][names.index("run")]))
            with self.subTest(rule=rule["name"]):
                if model_terms and run_terms:
                    self.assertEqual(len(model_terms), 1)
                    self.assertEqual(len(run_terms), 1)
                    self.assertIn((next(iter(model_terms)), next(iter(run_terms))), witnesses)
        self.assertTrue(any(rule["name"] == "surviving_mutant" for rule in self.pack["rules"]))

    def test_negation_is_stratified_through_witnessed_relations_and_nothing_recurses(self) -> None:
        heads = {}
        for rule in self.pack["rules"]:
            heads.setdefault(rule["head"]["relation"], []).append(rule)
        recursive = {name for name, rules in heads.items()
                     if any(atom["relation"] == name for rule in rules for atom in _atoms(rule))}
        self.assertEqual(recursive, set())
        negated = {(rule["head"]["relation"], atom["relation"]) for rule in self.pack["rules"]
                   for atom in _atoms(rule) if atom.get("negated")}
        self.assertEqual(negated, {
            ("php_model_disagree", "model_admissible"), ("go_model_disagree", "model_admissible"),
            ("undeclared_write", "model_writes"), ("surviving_mutant", "mutant_killed_in"),
            ("corpus_constrains", "op_has_surviving_mutant"), ("kill_closure_gap", "requested"),
            ("op_qualified_rt", "php_disagree_any"), ("op_qualified_rt", "go_disagree_any"),
            ("op_qualified_rt", "undeclared_any"), ("op_qualified_rt", "post_state_any"),
            ("op_qualified_rt", "kill_closure_gap_any"), ("undeclared_write", "model_scope_excluded"),
            ("post_state_gap", "php_observed"), ("post_state_gap", "go_observed"),
            ("op_qualified_rt", "effect_order_any"), ("op_qualified_rt", "repeat_delete_any"),
            ("op_qualified_rt", "learn_unmodeled_any"), ("learn_consistent", "learn_counterexample_any"),
            ("repeat_delete_not_found", "repeat_delete_has_effect"),
            ("first_delete_committed", "earlier_delete"),
            ("repeat_delete_has_effect", "model_scope_excluded"), ("oracle_stable", "oracle_unstable")})
        witnesses = {item["completes"] for item in self.declarations.values() if item["modality"] == "completeness"}
        self.assertTrue({target for _, target in negated} <= witnesses)

    def test_the_learn_campaign_downgrades_and_never_grants(self) -> None:
        """The learn gate is a downgrade, and absence of a campaign is not a premise.

        ``learn_unmodeled_any`` is positive in every one of its inputs -- the campaign
        must be bound to the run (``learn_run``), to the model
        (``model_describes_run``, ``learn_describes_model``) and must have *closed* its
        unmodelled list -- so nothing derives without a campaign.  Its completeness
        ``learn_unmodeled_gate_closed`` therefore reads only the replay run: the claim it
        licenses is "the downgrades this bundle carries are all of them", never "the
        model covers every op", which no absence could evidence.  That asymmetry is what
        makes the gate safe to add to a receipt that has no learn files.
        """
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        bodies = {name: [atom["relation"] for atom in _atoms(rules[name])] for name in rules}
        gate = [atom for atom in _atoms(rules["op_qualified_rt"]) if atom["relation"] == "learn_unmodeled_any"]
        self.assertEqual([atom.get("negated") for atom in gate], [True])
        self.assertIn("learn_unmodeled_gate_closed", bodies["op_qualified_rt"])
        for witness in ("learn_run", "model_describes_run", "learn_describes_model", "learn_unmodeled_closed",
                        "learn_unmodeled"):
            self.assertIn(witness, bodies["learn_unmodeled_any"], witness)
        self.assertEqual([atom.get("negated") for atom in _atoms(rules["learn_unmodeled_any"])],
                         [None] * 5, "every input of the downgrade is positive")
        self.assertEqual(bodies["learn_unmodeled_gate_closed"], ["replayed", "replay_run_current"])
        self.assertEqual(self.declarations["learn_unmodeled_gate_closed"]["completes"], "learn_unmodeled_any")
        # the consistency claim negates only under its own closure, which lists both
        # producer closures and the two bindings
        for witness in ("learn_predicted", "learn_run", "model_describes_run", "learn_describes_model",
                        "learn_predictions_closed", "learn_observations_closed"):
            self.assertIn(witness, bodies["learn_counterexample_closed"], witness)
        self.assertEqual(self.declarations["learn_counterexample_closed"]["completes"], "learn_counterexample_any")
        self.assertEqual(self.declarations["learn_consistent"]["modality"], "claim")
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        # producer authority: the oracle answers, the model host predicts, the harness ran it
        self.assertEqual(frozen["learn_observation"]["producer_classes"], ["php"])
        self.assertEqual(frozen["learn_prediction"]["producer_classes"], ["shen"])
        self.assertEqual(frozen["learn_unmodeled"]["producer_classes"], ["shen"])
        self.assertEqual(frozen["learn_run"]["producer_classes"], ["replay"])
        self.assertEqual(frozen["learn_observations_closed"]["producer_classes"], ["replay"])
        self.assertEqual(frozen["learn_describes_model"]["modality"], "compatibility")

    def test_the_repeat_delete_gate_is_on_and_its_closure_lists_every_positive_input(self) -> None:
        """The cross-request claim is load-bearing, and its negation is licensed.

        ``!repeat_delete_any`` may only be read under ``repeat_delete_closed``, whose
        body must list the closure of *every* positive input of the chain it negates:
        the requests and their tape order (``repeat_delete`` reads
        ``replay_request_seq``), both response tables (the status half of
        ``repeat_delete_violation`` and of ``first_delete_committed``), both effect
        tables (its effect half and ``repeat_delete_has_effect``) and -- because
        ``repeat_delete_has_effect`` itself negates ``model_scope_excluded`` -- the
        reviewer's closed exclusion set for the model that describes the run.
        """
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        bodies = {name: [atom["relation"] for atom in _atoms(rules[name])] for name in rules}
        gate = [atom for atom in _atoms(rules["op_qualified_rt"]) if atom["relation"] == "repeat_delete_any"]
        self.assertEqual([atom.get("negated") for atom in gate], [True])
        self.assertIn("repeat_delete_closed", bodies["op_qualified_rt"])
        # the gate is per op: repeat_delete_any joins the violating request's own op
        any_rule = rules["repeat_delete_any"]
        request = next(atom for atom in _atoms(any_rule) if atom["relation"] == "replay_request")
        violation = next(atom for atom in _atoms(any_rule) if atom["relation"] == "repeat_delete_violation")
        self.assertEqual(request["terms"][1], violation["terms"][1], "joined on the request")
        self.assertEqual(request["terms"][4], any_rule["head"]["terms"][1], "and the op is that request's")
        for witness in ("replay_run_current", "replay_requests_closed", "replay_request_seqs_closed",
                        "php_responses_closed", "go_responses_closed", "php_effects_closed", "go_effects_closed",
                        "model_describes_run", "model_scope_exclusions_closed"):
            self.assertIn(witness, bodies["repeat_delete_closed"], witness)
        self.assertEqual(self.declarations["repeat_delete_closed"]["completes"], "repeat_delete_any")

    def test_every_closure_lists_the_post_state_witnesses_and_the_gate_is_on(self) -> None:
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        bodies = {name: [atom["relation"] for atom in _atoms(rules[name])] for name in rules}
        self.assertIn("php_post_states_closed", bodies["php_disagreement_closed"])
        self.assertIn("go_post_states_closed", bodies["go_disagreement_closed"])
        for witness in ("replay_requests_closed", "php_post_states_closed", "go_post_states_closed"):
            self.assertIn(witness, bodies["post_state_gap_closed"])
        self.assertEqual(bodies["php_observed_closed"], ["php_post_states_closed"])
        self.assertEqual(bodies["go_observed_closed"], ["go_post_states_closed"])
        gate = [atom for atom in _atoms(rules["op_qualified_rt"]) if atom["relation"] == "post_state_any"]
        self.assertEqual([atom.get("negated") for atom in gate], [True])
        self.assertIn("post_state_gap_closed", bodies["op_qualified_rt"])
        for side in ("php", "go"):
            gap = rules[f"post_state_gap_{side}"]
            self.assertEqual(gap["head"]["terms"][2], {"type": "symbol", "value": side})
            self.assertIn("replay_requests_closed", bodies[f"post_state_gap_{side}"])
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        self.assertEqual(frozen["php_post_states_closed"]["completes"], "php_post_state")
        self.assertEqual(frozen["go_post_states_closed"]["completes"], "go_post_state")

    def test_every_witness_and_compatibility_relation_is_owned_by_the_class_that_vouches_for_it(self) -> None:
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        owners = {"replay_requests_closed": "replay", "php_effects_closed": "replay", "go_effects_closed": "replay",
                  "php_post_states_closed": "replay", "go_post_states_closed": "replay", "mutant_kills_closed": "replay",
                  "model_admissible_closed": "shen", "model_writes_closed": "shen", "model_describes_run": "shen",
                  "mutants_closed": "mut", "index_describes_replay": "reviewer",
                  "model_scope_exclusion": "reviewer", "model_scope_exclusions_closed": "reviewer",
                  # the model host may not certify its own model, and the reviewer may not
                  # wave the check through: the checker owns the certificate, the reviewer
                  # owns only which checker versions are admitted
                  "model_well_formed": "modelcheck", "model_checker_admitted": "reviewer"}
        for name, owner in owners.items():
            self.assertEqual(frozen[name]["producer_classes"], [owner], name)
        self.assertEqual([name for name, item in frozen.items() if not item["producer_classes"]], [])
        # the contradiction gate: a lying per-run kill closure poisons every op of the run
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        self.assertEqual([atom["relation"] for atom in _atoms(rules["kill_gap_closed"])],
                         ["mutant_kills_closed", "replay_requests_closed"])
        self.assertEqual([atom["relation"] for atom in _atoms(rules["kill_closure_gap_any"])],
                         ["replayed", "kill_closure_gap"])
        gate = [atom for atom in _atoms(rules["op_qualified_rt"]) if atom["relation"] == "kill_closure_gap_any"]
        self.assertEqual([atom.get("negated") for atom in gate], [True])
        self.assertIn("kill_gap_closed", [atom["relation"] for atom in _atoms(rules["op_qualified_rt"])])

    def test_scope_exclusions_gate_the_undeclared_write_negation_explicitly(self) -> None:
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        for name in ("undeclared_write_php", "undeclared_write_go"):
            atoms = _atoms(rules[name])
            positive = [atom["relation"] for atom in atoms if not atom.get("negated")]
            negated = [atom["relation"] for atom in atoms if atom.get("negated")]
            with self.subTest(rule=name):
                self.assertIn("model_scope_exclusions_closed", positive)
                self.assertIn("model_scope_excluded_closed", positive)
                self.assertEqual(negated, ["model_writes", "model_scope_excluded"])
                excluded = next(atom for atom in atoms if atom["relation"] == "model_scope_excluded")
                effect = next(atom for atom in atoms if atom["relation"].endswith("_effect"))
                self.assertEqual(excluded["terms"][1], effect["terms"][2], "the negation names the written table")
        self.assertEqual([atom["relation"] for atom in _atoms(rules["model_scope_excluded"])], ["model_scope_exclusion"])
        self.assertEqual([atom["relation"] for atom in _atoms(rules["model_scope_excluded_closed"])],
                         ["model_scope_exclusions_closed"])
        self.assertIn("model_scope_exclusions_closed", [atom["relation"] for atom in _atoms(rules["undeclared_writes_closed"])])
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        self.assertEqual(frozen["model_scope_exclusion"]["modality"], "assumption")
        self.assertEqual(frozen["model_scope_exclusions_closed"]["completes"], "model_scope_exclusion")

    def test_model_well_formedness_is_a_positive_premise_of_qualification(self) -> None:
        rules = {rule["name"]: rule for rule in self.pack["rules"]}
        atoms = _atoms(rules["op_qualified_rt"])
        well_formed = next(atom for atom in atoms if atom["relation"] == "model_well_formed")
        admitted = next(atom for atom in atoms if atom["relation"] == "model_checker_admitted")
        binding = next(atom for atom in atoms if atom["relation"] == "model_describes_run")
        for atom in (well_formed, admitted, binding):
            self.assertIsNone(atom.get("negated"), atom["relation"])
        # the certificate is joined on the model the compatibility witness binds to this run
        self.assertEqual(well_formed["terms"][0], binding["terms"][0])
        self.assertEqual(binding["terms"][1], rules["op_qualified_rt"]["head"]["terms"][1])
        # and on the (checker, version) pair the reviewer admits
        self.assertEqual(admitted["terms"], well_formed["terms"][1:3])
        frozen = {item["name"]: item for item in self.pack["primitives"]}
        self.assertEqual(frozen["model_well_formed"]["producer_classes"], ["modelcheck"])
        self.assertNotIn("shen", frozen["model_well_formed"]["producer_classes"])
        self.assertEqual(frozen["model_well_formed"]["context_indices"], ["model"])
        self.assertEqual([column["name"] for column in frozen["model_well_formed"]["columns"]],
                         ["model", "checker", "checker_version", "certificate"])
        self.assertEqual(frozen["model_checker_admitted"]["producer_classes"], ["reviewer"])
        self.assertEqual(frozen["model_checker_admitted"]["context_indices"], [])
        # nothing negates well-formedness, so neither relation has (or needs) a closure
        self.assertEqual([name for name, item in self.declarations.items()
                          if item["completes"] in ("model_well_formed", "model_checker_admitted")], [])
        self.assertNotIn("model_well_formed", {atom["relation"] for rule in self.pack["rules"]
                                               for atom in _atoms(rule) if atom.get("negated")})

    def test_the_static_join_is_isolated_in_the_claim_rule(self) -> None:
        for rule in self.pack["rules"]:
            static = [atom["relation"] for atom in _atoms(rule)
                      if self.declarations[atom["relation"]]["binding"] == "static"]
            with self.subTest(rule=rule["name"]):
                if rule["name"] == "op_qualified":
                    self.assertEqual(static, ["op_declared"])
                    self.assertEqual([atom["relation"] for atom in _atoms(rule)],
                                     ["op_declared", "index_describes_replay", "op_qualified_rt"])
                else:
                    self.assertEqual(static, [])


class ReplayCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_pack()
        self.declarations = {item["name"]: item for item in pack_relations(self.pack)}
        self.paths = case_paths()

    def test_case_numbers_cover_the_control_and_the_adversarial_shapes(self) -> None:
        self.assertEqual([path.stem for path in self.paths], list(case_builder.BUILDERS))
        self.assertEqual(sorted({path.name[:2] for path in self.paths}),
                         ["00", "01", "02", "03", "04", "05", "06", "08", "09", "10", "11", "13", "14", "15",
                          "17", "18", "19", "20", "21", "23", "24", "25", "26", "27", "28", "29", "31"])
        self.assertEqual([path.stem for path in case_paths(REJECTED_DIR)],
                         ["07-producer-class-violation", "12-closure-producer-violation",
                          "16-exclusion-producer-violation", "22-effect-seq-producer-violation",
                          "30-well-formed-producer-violation"])
        self.assertEqual([path.stem for path in case_paths(REJECTED_DIR)], list(case_builder.REJECTED_BUILDERS))

    def test_every_case_regenerates_identically_from_the_exporter(self) -> None:
        for path in [*self.paths, *case_paths(REJECTED_DIR)]:
            case = read_json(path)
            with self.subTest(case=path.name):
                generated = case_builder.build(path.stem)
                self.assertNotIn("expected", generated)
                self.assertEqual({key: value for key, value in case.items() if key != "expected"}, generated)
                self.assertEqual(list(case), [*generated, "expected"] if "expected" in case else list(generated))

    def test_case_conventions(self) -> None:
        for path in self.paths:
            case = read_json(path)
            with self.subTest(case=path.name):
                self.assertEqual(path.stem, case["id"])
                self.assertEqual(case["rule_pack"], "rules-replay-v1")
                self.assertEqual(case["provenance"]["kind"], "synthetic")
                self.assertIn("seeded_fault", case["provenance"])
                self.assertTrue(case["review_notes"])
                context = case["context"]
                self.assertEqual(set(context), {"run", "model", "index"})
                self.assertRegex(context["model"], r"^[0-9a-f]{64}$")
                self.assertRegex(context["index"], r"^[0-9a-f]{64}$")
                for entry in case["assumptions"]:
                    self.assertEqual(entry["kind"], "assumption")
                    self.assertEqual(entry["relation"], "model_scope_exclusion")
                    self.assertEqual(entry["source"].split(" ", 1)[0], "reviewer")
                    self.assertIn(f"model:{context['model'][:12]} run:", entry["source"])
                ids = set()
                for entry in [*case["facts"], *case["assumptions"]]:
                    self.assertEqual(entry["kind"], "assumption" if entry["relation"] == "model_scope_exclusion" else "fact")
                    self.assertNotIn(entry["id"], ids)
                    ids.add(entry["id"])
                    declaration = self.declarations[entry["relation"]]
                    self.assertTrue(declaration["primitive"], entry["relation"])
                    self.assertNotEqual(declaration["modality"], "claim")
                    self.assertEqual(entry["arg_order"], [c["name"] for c in declaration["columns"]])
                    values = dict(zip(entry["arg_order"], entry["args"]))
                    self.assertEqual(entry["context"], {name: values[name] for name in declaration["context_indices"]})
                    for key in ("run", "model", "index"):
                        if key in values:
                            if (case["id"] == "28-well-formed-other-model"
                                    and entry["relation"] == "model_well_formed" and key == "model"):
                                # the planted fault: a certificate for another model, which must not join
                                self.assertNotEqual(values[key], context[key])
                                continue
                            self.assertEqual(values[key], context[key])
                    self.assertTrue(entry["source"])
                    if declaration["producer_classes"]:
                        self.assertIn(entry["source"].split(" ", 1)[0], declaration["producer_classes"])
                for claim in case["claims"]:
                    self.assertEqual(claim["quantifier"], "exists")
                    self.assertIsNone(claim["domain"])
                    declaration = self.declarations[claim["relation"]]
                    self.assertFalse(declaration["primitive"])
                    self.assertEqual(claim["arg_order"], [c["name"] for c in declaration["columns"]])
                    values = {name: value for name, value in zip(claim["arg_order"], claim["args"])
                              if not isinstance(value, dict)}
                    self.assertEqual(claim["context"], {name: values[name] for name in declaration["context_indices"]})
                    for diagnostic in claim["diagnostics"]:
                        self.assertIn(diagnostic["trigger_relation"], self.declarations)

    def test_evidence_ids_follow_the_replay_scheme_and_recompute_from_their_rows(self) -> None:
        decls = {decl.name: decl for decl in replay_facts.replay_relations()}
        for path in self.paths:
            case = read_json(path)
            segments = set()
            with self.subTest(case=path.name):
                for entry in [*case["facts"], *case["assumptions"]]:
                    match = EVIDENCE_ID.match(entry["id"])
                    self.assertIsNotNone(match, entry["id"])
                    prefix, segment, relation, row12 = match.groups()
                    self.assertEqual(relation, entry["relation"])
                    self.assertEqual(row12, replay_facts.row_digest(relation, entry["args"])[:12])
                    self.assertEqual(prefix, replay_facts.evidence_prefix(decls[relation]))
                    if relation in CLAIM_TIME_RELATIONS:
                        self.assertEqual(segment, "claim-time")
                    elif segment == "claim-time":
                        self.assertIn(relation, CLAIM_TIME_ADMITTED, entry["id"])
                    else:
                        segments.add(segment)
                        self.assertEqual(entry["id"], replay_facts.evidence_id(segment + "0" * 52, decls[relation],
                                                                               entry["args"]))
                self.assertEqual(len(segments), 1, "one exported receipt per case")

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
                    if output["kind"] == "missing_premise":
                        self.assertEqual(output["when_claim"], "unresolved")
                        self.assertIn(output["relation"], self.declarations)
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
                    else:
                        self.assertEqual(verdict, "unresolved")
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
        lying = read_json(CASES_DIR / "08-lying-closure.json")
        gap = lying["expected"]["claims"]["claim-kill-outside-corpus"]
        self.assertEqual(gap["semantic_verdict"], "supported")
        self.assertTrue(set(gap["support_leaves"]) & set(gap["forbidden_leaves"]))
        self.assertEqual({leaf.split(":")[2] for leaf in gap["forbidden_leaves"]}, {"mutant_kills_closed"})
        self.assertEqual(lying["provenance"]["seeded_fault"], "lying-completeness-witness")
        # the contradicted closure never supports a qualification
        for claim_id in ("claim-qualified-create", "claim-qualified-close"):
            outcome = lying["expected"]["claims"][claim_id]
            self.assertEqual(outcome["semantic_verdict"], "unresolved")
            self.assertEqual(outcome["support_leaves"], [])
            self.assertEqual([item["relation"] for item in outcome["missing_premises"]], ["replay_request"])

    def test_expected_table_duplicates_each_case(self) -> None:
        table = read_json(EXPECTED_PATH)
        self.assertEqual(table["schema_version"], 1)
        self.assertEqual(table["rule_pack"], "rules-replay-v1")
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
                metadata = canonical_metadata(bundle)
                self.assertEqual({metadata[key] for key in ("run", "model", "index")}, set(case["context"].values()))

    def test_adapter_rejects_extra_arguments_wrong_context_and_a_foreign_pack(self) -> None:
        case = read_json(CASES_DIR / "00-positive-control.json")
        extra = json.loads(json.dumps(case))
        extra["facts"][0]["args"].append("silently-discarded")
        with self.assertRaisesRegex(ValueError, "arity"):
            bundle_payload(extra, self.pack)
        wrong = json.loads(json.dumps(case))
        wrong["facts"][0]["context"] = {"run": "run-other"}
        with self.assertRaisesRegex(ValueError, "context"):
            bundle_payload(wrong, self.pack)
        foreign = json.loads(json.dumps(case))
        foreign["rule_pack"] = "rules-static-v1"
        with self.assertRaisesRegex(ValueError, "rule pack"):
            bundle_payload(foreign, self.pack)

    def test_each_case_names_a_discriminating_semantic_control(self) -> None:
        table = read_json(EXPECTED_PATH)["cases"]
        for stem, review in case_builder.REVIEW.items():
            for claim_id, (verdict, status, missing, discrepancies) in review.items():
                outcome = table[stem]["claims"][claim_id]
                with self.subTest(case=stem, claim=claim_id):
                    self.assertEqual(outcome["semantic_verdict"], verdict)
                    self.assertEqual(outcome["operational_status"], status)
                    self.assertEqual(sorted(item["relation"] for item in outcome["missing_premises"]), sorted(missing))
                    self.assertEqual(sorted(item["kind"] for item in outcome["discrepancies"]), sorted(discrepancies))
        control = table["00-positive-control"]["claims"]
        for claim_id in ("claim-qualified-create", "claim-qualified-close"):
            leaves = control[claim_id]["support_leaves"]
            self.assertEqual({leaf.split(":")[0] for leaf in leaves},
                             {"replay", "php", "go", "shen", "mut", "reviewer", "modelcheck"})
        facts = {entry["id"]: entry for entry in read_json(CASES_DIR / "00-positive-control.json")["facts"]}
        classes = {facts[leaf]["source"].split(" ", 1)[0] for leaf in control["claim-qualified-create"]["support_leaves"]}
        self.assertTrue({"replay", "php", "go", "shen", "mut", "reviewer", "php-census", "modelcheck"} <= classes)
        self.assertEqual(table["06-stale-replay"]["claims"]["claim-qualified-close"]["operational_status"], "stale")

    def test_each_rejected_case_is_refused_at_ingestion_by_the_producer_class_alone(self) -> None:
        table = read_json(REJECTED_PATH)
        self.assertEqual(table["rule_pack"], "rules-replay-v1")
        self.assertEqual(set(table["cases"]), {path.stem for path in case_paths(REJECTED_DIR)})
        control = read_json(CASES_DIR / "00-positive-control.json")
        for path in case_paths(REJECTED_DIR):
            case = read_json(path)
            entry = table["cases"][case["id"]]
            with self.subTest(case=path.name):
                with self.assertRaises(BundleIngestionError) as ctx:
                    load_case(path, self.pack)
                self.assertIn("evidence-producer", str(ctx.exception))
                lenient = load_case(path, self.pack, validate=False)
                self.assertEqual([issue.code for issue in validate_bundle(lenient)], entry["validation_issues"])
                relabelled = [f for f in [*case["facts"], *case["assumptions"]] if f["source"] == entry["relabelled_source"]]
                self.assertEqual({f["relation"] for f in relabelled}, {entry["relabelled_relation"]})
                self.assertEqual(len(relabelled), len(entry["validation_issues"]))
                admitted = self.declarations[entry["relabelled_relation"]]["producer_classes"]
                self.assertNotIn(entry["relabelled_source"].split(" ", 1)[0], admitted)
                differing = [(a, b) for a, b in zip([*control["facts"], *control["assumptions"]],
                                                    [*case["facts"], *case["assumptions"]]) if a != b]
                self.assertEqual(len(differing), len(entry["validation_issues"]))
                for a, b in differing:
                    self.assertEqual({**a, "source": b["source"]}, b)
                self.assertEqual(control["claims"], case["claims"])
                self.assertEqual(control["outputs"], case["outputs"])
                self.assertNotIn("expected", case)


if __name__ == "__main__":
    unittest.main()
