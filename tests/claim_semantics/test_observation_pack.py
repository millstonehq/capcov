"""The observation rule pack, checked as a pack: shape, producer classes, lane hygiene.

Nothing here reads a receipt.  These are the properties the pack must have
before any receipt is worth exporting: it validates on its own, it declares only
the two producer classes a Lane A receipt can carry, it shares no relation name
with the replay pack, no rule body mentions a model or replay relation by name,
``observations_agree`` is only ever a head, and every negated atom is paired in
its own body with an exactly-scoped completeness witness.

The model-freedom test is by NAME against the forbidden list of the spec's
section 4, not by "we did not import a model module": a relation the pack never
declares cannot be a premise, and that is what makes the claim model-free by
construction rather than by omission.
"""
from __future__ import annotations

import unittest

from capcov.claims import validate_bundle
from capcov.claims.observation import load_observation_schema
from capcov.claims.observation.pack import (PACK_ID, RELATION_FIELDS, load_pack, pack_bundle,
                                            pack_relations)
from capcov.claims.replay.pack import pack_bundle as replay_pack_bundle

PRODUCER_CLASSES = {"observe", "reviewer"}
CLAIM_HEAD = "observations_agree"

#: Spec section 4: the premises the body of ``observations_agree`` -- and, by the
#: stronger reading this test takes, no rule in the pack at all -- may mention.
#: Every one belongs to Lane B (model, mutation, census, replay) or to the op
#: keying this claim deliberately does not have.
FORBIDDEN_PREMISES = (
    "model_describes_run", "model_observed", "model_admissible", "model_admissible_closed",
    "model_effect", "model_effect_seq", "model_effect_seqs_closed", "model_writes",
    "model_writes_closed", "model_scope_exclusion", "model_scope_exclusions_closed",
    "model_well_formed", "model_checker_admitted", "op_exercised", "op_declared",
    "index_describes_replay", "corpus_constrains", "mutant", "mutants_closed",
    "mutant_killed", "mutant_kills_closed", "surviving_mutant", "kill_gap_closed",
    "kill_closure_gap_any", "php_model_agree", "go_model_agree", "php_disagree_any",
    "go_disagree_any", "php_disagreement_closed", "go_disagreement_closed",
    "undeclared_write", "undeclared_any", "undeclared_writes_closed",
    "repeat_delete_any", "repeat_delete_closed", "replay_run_current", "op_qualified",
    "op_qualified_rt",
)
#: Prefix families from the same list (``effect_order_*``, ``learn_*``).
FORBIDDEN_PREFIXES = ("effect_order_", "learn_", "op_qualified")


def _atoms(rule: dict) -> list[dict]:
    """Every relation-naming atom of a rule; a comparison atom names none."""
    return [atom for atom in (rule["head"], *rule["body"]) if "relation" in atom]


class ObservationPackShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = load_pack()
        cls.relations = pack_relations(cls.pack)
        cls.bundle = pack_bundle()
        cls.by_name = {relation["name"]: relation for relation in cls.relations}

    def test_pack_validates_on_its_own(self) -> None:
        self.assertEqual(validate_bundle(self.bundle), ())
        self.assertEqual(self.pack["id"], PACK_ID)
        self.assertEqual(self.pack["supplementary_primitives"], [])

    def test_every_relation_carries_exactly_the_declaration_fields(self) -> None:
        for relation in self.relations:
            self.assertEqual(set(relation), RELATION_FIELDS, relation["name"])

    def test_primitives_are_byte_identical_to_the_schema_document(self) -> None:
        self.assertEqual(self.pack["primitives"], load_observation_schema()["relations"])

    def test_the_schema_declares_the_thirty_six_primitives(self) -> None:
        """36, not the spec's 38: ``observation_effect`` and its closure are gone.

        The unit of effect comparison is ``effects_digest``; the per-effect rows
        are the receipt's evidence for it, recomputed at ingest, and no rule
        ever read them as relations.  A declared primitive nothing reads is
        coverage-shaped dead weight, so they were deleted rather than given a
        rule that would only restate ``effects_disagree`` at finer grain.
        """
        primitives = [r for r in self.relations if r["primitive"]]
        self.assertEqual(len(primitives), 36)
        names = {r["name"] for r in primitives}
        self.assertNotIn("observation_effect", names)
        self.assertNotIn("observation_effect_rows_closed", names)
        # Conflict C4: no compatibility witness anywhere, because nothing is
        # statically bound.  This is structural, not an omission.
        for relation in self.relations:
            self.assertEqual(relation["binding"], "runtime", relation["name"])
            self.assertEqual(relation["compatibility_targets"], [], relation["name"])
            self.assertEqual(relation["compatibility_context_indices"], [], relation["name"])

    def test_every_primitive_is_read_by_some_rule(self) -> None:
        """No declared primitive is dead weight that looks like coverage."""
        read = set()
        for rule in self.pack["rules"]:
            for atom in _atoms(rule)[1:]:
                read.add(atom["relation"])
        unread = sorted(r["name"] for r in self.relations if r["primitive"]
                        and r["name"] not in read)
        self.assertEqual(unread, [])

    def test_only_observe_and_reviewer_may_produce_anything(self) -> None:
        for relation in self.relations:
            self.assertLessEqual(set(relation["producer_classes"]), PRODUCER_CLASSES,
                                 relation["name"])
        for relation in self.relations:
            if relation["primitive"]:
                self.assertTrue(relation["producer_classes"], relation["name"])
            else:
                self.assertEqual(relation["producer_classes"], [], relation["name"])

    def test_the_pack_shares_no_relation_name_with_the_replay_pack(self) -> None:
        replay = {decl.name for decl in replay_pack_bundle().relations}
        self.assertEqual(sorted(set(self.by_name) & replay), [])

    def test_no_rule_mentions_a_model_mutation_census_or_replay_relation(self) -> None:
        for rule in self.pack["rules"]:
            for atom in _atoms(rule):
                name = atom["relation"]
                self.assertNotIn(name, FORBIDDEN_PREMISES,
                                 f"rule {rule['name']} mentions {name}")
                for prefix in FORBIDDEN_PREFIXES:
                    self.assertFalse(name.startswith(prefix),
                                     f"rule {rule['name']} mentions {name}")

    def test_the_only_model_named_relation_is_the_unassessed_premise(self) -> None:
        """``model_conformance_unassessed`` is the pack's one ``model_``-prefixed name.

        It is not a model: it is a DERIVED projection of the receipt's own
        ``unassessed`` rows, whose whole content is "no model described this
        run".  The test pins that there is exactly one such name, that it is
        derived with no producer of its own, and that the rule deriving it reads
        nothing but ``observation_*`` relations -- so no model row can reach it.
        """
        model_named = sorted(name for name in self.by_name if name.startswith("model_"))
        self.assertEqual(model_named, ["model_conformance_unassessed"])
        decl = self.by_name["model_conformance_unassessed"]
        self.assertFalse(decl["primitive"])
        self.assertEqual(decl["producer_classes"], [])
        rules = [r for r in self.pack["rules"]
                 if r["head"]["relation"] == "model_conformance_unassessed"]
        self.assertTrue(rules)
        for rule in rules:
            for atom in _atoms(rule)[1:]:
                self.assertTrue(atom["relation"].startswith("observation_"),
                                f"{rule['name']} reads {atom['relation']}")
        for name in self.by_name:
            self.assertNotIn(name, FORBIDDEN_PREMISES, name)

    def test_the_claim_head_is_only_ever_a_head(self) -> None:
        heads = [rule for rule in self.pack["rules"] if rule["head"]["relation"] == CLAIM_HEAD]
        self.assertEqual(len(heads), 1)
        for rule in self.pack["rules"]:
            for atom in _atoms(rule)[1:]:
                self.assertNotEqual(atom["relation"], CLAIM_HEAD, rule["name"])

    def test_the_claim_body_carries_the_positive_model_free_premise(self) -> None:
        rule = next(r for r in self.pack["rules"] if r["head"]["relation"] == CLAIM_HEAD)
        body = _atoms(rule)[1:]
        positive = {a["relation"] for a in body if not a.get("negated")}
        negated = {a["relation"] for a in body if a.get("negated")}
        # A receipt that assessed a model cannot reach the claim: the premise is
        # POSITIVE and closed, so silence about a model is not the same as saying
        # no model was assessed.
        self.assertIn("model_conformance_unassessed", positive)
        self.assertIn("observation_run_current", positive)
        self.assertIn("policy_bound", positive)
        self.assertIn("set_bound", positive)
        self.assertIn("set_exercised", positive)
        self.assertIn("observation_disagree_any", negated)
        self.assertIn("observation_gap_any", negated)
        self.assertIn("observation_masked_unadmitted", negated)
        self.assertIn("observation_degenerate_any", negated)

    def test_the_claim_head_is_keyed_on_run_check_set_and_policy(self) -> None:
        decl = self.by_name[CLAIM_HEAD]
        self.assertEqual([column["name"] for column in decl["columns"]],
                         ["run", "check", "scenario_set", "policy"])
        self.assertEqual(decl["context_indices"], ["run"])

    def test_every_negated_atom_has_an_exactly_scoped_completeness_witness(self) -> None:
        for rule in self.pack["rules"]:
            body = _atoms(rule)[1:]
            for atom in body:
                if not atom.get("negated"):
                    continue
                target = atom["relation"]
                witnesses = [other for other in body
                             if not other.get("negated")
                             and self.by_name[other["relation"]]["completes"] == target]
                self.assertTrue(witnesses,
                                f"rule {rule['name']} negates {target} with no witness")
                target_decl = self.by_name[target]
                for witness in witnesses:
                    witness_decl = self.by_name[witness["relation"]]
                    self.assertEqual(witness_decl["modality"], "completeness")
                    self.assertEqual(witness_decl["context_indices"],
                                     target_decl["context_indices"],
                                     f"{witness['relation']} does not share "
                                     f"{target}'s context")


if __name__ == "__main__":
    unittest.main()
