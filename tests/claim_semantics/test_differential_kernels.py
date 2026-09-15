from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from capcov.claims import (Atom, Claim, Column, Constant, Context, DiagnosticRule,
                           Evidence, EvidenceMapping, RelationDecl, Rule, Variable,
                           bundle_from_json, canonical_json, digest, validate_bundle)
from tests.claim_fixtures import Bundle, with_facts  # attributed fixture bundles
from capcov.claims.differential import (COMPARABLE_CLAIM_FIELDS, DifferentialMismatch,
                                       KernelClaim, KernelReport, _missing, compare,
                                       reports_match, run_python, run_souffle)

from .adapter import load_fixture

ROOT = Path(__file__).parent / "corpus"


def R(name, *columns, **kwargs):
    return RelationDecl(name, tuple(Column(*column) for column in columns), **kwargs)


def mixed_mapping_bundle(*, include_witness=True, witness_effect="support",
                         witness_fact=True, witness_targets=None,
                         witness_payload=("index", "run"),
                         static_bindings=(("index", "index"), ("value", "value")),
                         runtime_bindings=(("run", "run"), ("value", "value")),
                         witness_bindings=(("index", "index"), ("run", "run"))):
    claimed = R("mixed_claim", ("index", "digest"), ("run", "symbol"),
                ("value", "symbol"), modality="claim")
    static = R("static_seen", ("index", "digest", True), ("value", "symbol"),
               binding="static", context_indices=("index",))
    runtime = R("runtime_seen", ("run", "symbol"), ("value", "symbol"))
    decoy = R("decoy_seen", ("value", "symbol"))
    compatibility = R(
        "index_describes_run", ("index", "digest"), ("run", "symbol"),
        modality="compatibility",
        compatibility_targets=(witness_targets or ("static_seen", "runtime_seen")),
        compatibility_context_indices=witness_payload,
    )
    static_atom = Atom("static_seen", (Constant("index-a", "digest"), Constant("v")))
    runtime_atom = Atom("runtime_seen", (Constant("run-a"), Constant("v")))
    witness_atom = Atom("index_describes_run",
                        (Constant("index-a", "digest"), Constant("run-a")))
    mappings = [
        EvidenceMapping("mixed_claim", "static_seen", "support",
                        bindings=static_bindings, claim_id="mixed"),
        EvidenceMapping("mixed_claim", "runtime_seen", "support",
                        bindings=runtime_bindings, claim_id="mixed"),
    ]
    if include_witness:
        mappings.append(EvidenceMapping("mixed_claim", "index_describes_run",
                                        witness_effect, bindings=witness_bindings,
                                        claim_id="mixed"))
    facts = [static_atom, runtime_atom]
    if witness_fact:
        facts.append(witness_atom)
    claim = Claim("mixed_claim",
                  (Constant("index-a", "digest"), Constant("run-a"), Constant("v")),
                  id="mixed")
    return Bundle((claimed, static, runtime, decoy, compatibility), facts=tuple(facts),
                  claims=(claim,), mappings=tuple(mappings))


def alternative_overflow_bundle():
    rejected = R("rejected", ("x", "symbol"), modality="assumption")
    observed = R("observed_overflow", ("x", "symbol"))
    claimed = R("claimed_overflow", ("x", "symbol"),
                modality="claim", primitive=False)
    trigger = Atom("rejected", (Constant("v"),))
    seen = Atom("observed_overflow", (Constant("v"),))
    evidence = (
        Evidence("blocked", trigger, source="test"),
        *(Evidence(f"a-tainted-{i:03d}", seen, source="test",
                   depends_on=("blocked",)) for i in range(64)),
        Evidence("z-independent", seen, source="test"),
    )
    return Bundle(
        (rejected, observed, claimed), facts=(trigger, seen),
        evidence=evidence,
        rules=(Rule(Atom("claimed_overflow", (Variable("x"),)),
                    (Atom("observed_overflow", (Variable("x"),)),),
                    "derive"),),
        claims=(Claim("claimed_overflow", (Constant("v"),), id="claim"),),
        diagnostics=(DiagnosticRule(
            "rejected", "forbidden", claim_id="claim"),))


def revoked_bundle(*, mapped_refutation=False):
    rejected = R("source__rejected", ("x", "symbol"), modality="assumption")
    observed = R("observed", ("x", "symbol"))
    claimed = R("claimed", ("x", "symbol"), modality="claim", primitive=not mapped_refutation)
    trigger = Atom("source__rejected", (Constant("v"),))
    observation = Atom("observed", (Constant("v"),))
    evidence = (Evidence("blocked-assumption", trigger, source="test"),
                Evidence("a-tainted", observation, source="test", depends_on=("blocked-assumption",)),
                Evidence("z-independent", observation, source="test"))
    claim = Claim("claimed", (Constant("v"),), id="claim")
    diagnostics = (DiagnosticRule("source__rejected", "forbidden", claim_id="claim"),)
    if mapped_refutation:
        mappings = (EvidenceMapping("claimed", "observed", "refutation",
                                    bindings=(("x", "x"),), claim_id="claim"),)
        return Bundle((rejected, observed, claimed), facts=(trigger, observation), evidence=evidence,
                      claims=(claim,), mappings=mappings, diagnostics=diagnostics)
    rule = Rule(Atom("claimed", (Variable("x"),)), (Atom("observed", (Variable("x"),)),), "derive")
    return Bundle((rejected, observed, claimed), facts=(trigger, observation), evidence=evidence,
                  rules=(rule,), claims=(claim,), diagnostics=diagnostics)


class DifferentialBoundaryTests(unittest.TestCase):
    def test_missing_binary_is_named_blocks_and_persists_replay(self):
        bundle = Bundle((R("seen", ("x", "symbol")),))
        with tempfile.TemporaryDirectory() as directory, patch("capcov.claims.souffle.shutil.which", return_value=None):
            report = run_souffle(bundle)
            self.assertEqual(report.operational_failure, "souffle-unavailable")
            with self.assertRaises(DifferentialMismatch) as caught:
                compare(bundle, replay_root=directory)
            self.assertEqual(caught.exception.result.souffle.operational_failure, "souffle-unavailable")
            self.assertTrue(Path(caught.exception.result.replay_path).is_file())
            self.assertFalse(caught.exception.result.shrink_truncated)

    def test_translation_failure_is_named_blocks_and_persists_replay(self):
        bundle = Bundle((R("seen", ("x", "symbol")),))
        with tempfile.TemporaryDirectory() as directory, \
             patch("capcov.claims.differential.run_bundle", side_effect=ValueError("bad translation")):
            report = run_souffle(bundle)
            self.assertEqual(report.operational_failure, "souffle-translation-or-output-invalid")
            with self.assertRaises(DifferentialMismatch) as caught:
                compare(bundle, replay_root=directory)
            self.assertTrue(Path(caught.exception.result.replay_path).is_file())

    def test_report_normalization_recursion_is_named_at_both_boundaries(self):
        bundle = Bundle((R("seen", ("x", "symbol")),))
        with patch("capcov.claims.differential._rows",
                   side_effect=RecursionError("normalization recursion")):
            python = run_python(bundle)
            with patch(
                    "capcov.claims.differential.run_bundle",
                    return_value=SimpleNamespace(relations={"seen": ()}, claims=())):
                souffle = run_souffle(bundle)
        self.assertEqual(
            (python.operational_failure, souffle.operational_failure),
            ("resource-exhausted", "resource-exhausted"))

    def test_differential_claim_contract_names_every_compared_field(self):
        self.assertEqual(COMPARABLE_CLAIM_FIELDS,
                         ("semantic", "operational", "basis", "missing_premises"))
        self.assertEqual(tuple(field.name for field in fields(KernelClaim)),
                         ("key", "index", *COMPARABLE_CLAIM_FIELDS))
        base = KernelClaim("claim", 0, "supported", "complete", "derivational", ())
        left = KernelReport("python", (), (base,))
        for field_name in COMPARABLE_CLAIM_FIELDS:
            changed_value = {"semantic": "refuted", "operational": "stale",
                             "basis": "bounded-history-model",
                             "missing_premises": ("missing",)}[field_name]
            right = KernelReport("souffle", (), (replace(base, **{field_name: changed_value}),))
            with self.subTest(field=field_name):
                self.assertFalse(reports_match(left, right))

    def test_missing_premise_normalization_preserves_json_types(self):
        structured = _missing(({"relation": "r"},))
        literal = _missing(('{"relation":"r"}',))
        self.assertNotEqual(structured, literal)
        self.assertEqual(structured, ('{"relation":"r"}',))
        self.assertEqual(literal, ('"{\\"relation\\":\\"r\\"}"',))

    def test_same_operational_failure_on_both_sides_is_not_agreement(self):
        left = KernelReport("python", (), (), "resource-exhausted", "left")
        right = KernelReport("souffle", (), (), "resource-exhausted", "right")
        self.assertFalse(reports_match(left, right))
        bundle = Bundle((R("seen", ("x", "symbol")),))
        with tempfile.TemporaryDirectory() as directory, \
             self.assertRaises(DifferentialMismatch) as caught:
            compare(bundle, python_runner=lambda _: left,
                    souffle_runner=lambda _: right, replay_root=directory)
        self.assertFalse(caught.exception.result.matched)
        self.assertTrue(caught.exception.result.replay_reproduced)

    def test_differential_contract_detects_each_empty_or_populated_relation_mutation(self):
        relations = (("empty", ()), ("populated", (("v",),)))
        left = KernelReport("python", relations, ())
        mutants = (
            KernelReport("souffle", (("empty", (("added",),)), relations[1]), ()),
            KernelReport("souffle", (relations[0], ("populated", ())), ()),
            KernelReport("souffle", (*relations, ("right_only", ())), ()),
        )
        for mutant in mutants:
            with self.subTest(relations=mutant.relations):
                self.assertFalse(reports_match(left, mutant))


@unittest.skipUnless(shutil.which("souffle"), "souffle runtime is unavailable")
class DifferentialCorpusTests(unittest.TestCase):
    def assert_invalid_bundle_blocks(self, name, bundle):
        """Require canonical semantic-invalid inputs to traverse the differential."""
        expected_bytes = (canonical_json(bundle) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DifferentialMismatch, msg=name) as caught:
                compare(bundle, replay_root=directory, max_steps=1)
            result = caught.exception.result
            self.assertEqual(
                (result.python.operational_failure,
                 result.souffle.operational_failure),
                ("invalid-input", "invalid-input"), name)
            self.assertFalse(result.matched, name)
            self.assertTrue(result.replay_reproduced, name)
            replay = Path(result.replay_path)
            self.assertTrue(replay.is_file(), name)
            self.assertEqual(replay.read_bytes(), expected_bytes, name)
            reloaded = bundle_from_json(
                replay.read_text(encoding="utf-8"), validate=False)
            self.assertEqual(digest(reloaded), digest(bundle), name)
            return result

    def test_every_relation_and_claim_agrees_for_every_fixture(self):
        expected_document = json.loads(
            (ROOT / "expected.json").read_text(encoding="utf-8"))
        expected = expected_document["cases"]
        paths = sorted(ROOT.glob("[0-9][0-9]-*.json"))
        self.assertEqual(len(paths), 14)
        for path in paths:
            with self.subTest(case=path.stem):
                bundle = load_fixture(path)
                result = compare(bundle)
                self.assertTrue(result.matched)
                expected_names = tuple(decl.name for decl in bundle.relations)
                self.assertEqual(tuple(name for name, _ in result.python.relations), expected_names)
                self.assertEqual(tuple(name for name, _ in result.souffle.relations), expected_names)
                self.assertEqual(result.python.relations, result.souffle.relations)
                self.assertEqual(result.python.canonical_digest, result.souffle.canonical_digest)
                self.assertEqual(len(result.python.claims), len(bundle.claims))
                self.assertEqual(len(result.souffle.claims), len(bundle.claims))
                for declared, left, right in zip(
                        bundle.claims, result.python.claims,
                        result.souffle.claims):
                    self.assertEqual((left.key, left.index),
                                     (right.key, right.index))
                    for field in COMPARABLE_CLAIM_FIELDS:
                        self.assertEqual(
                            getattr(left, field), getattr(right, field),
                            (path.stem, declared.id, field))
                    oracle = expected[path.stem]["claims"][declared.id]
                    self.assertEqual(left.semantic,
                                     oracle["semantic_verdict"])
                    self.assertEqual(left.operational,
                                     oracle["operational_status"])
                    self.assertEqual(
                        left.basis,
                        expected_document["evaluation_basis_by_quantifier"][
                            declared.quantifier.value])
                    self.assertEqual(
                        left.missing_premises,
                        tuple(sorted(canonical_json(item) for item in
                                     oracle["missing_premises"])))

    def test_all_four_verdict_states_agree(self):
        positive_source = R("positive_source", ("x", "symbol"))
        negative_source = R("negative_source", ("x", "symbol"))
        positive = R("positive", ("x", "symbol"), modality="claim", primitive=False)
        negative = R("negative", ("x", "symbol"), modality="claim",
                     polarity="negative", primitive=False)
        counter = R("counter", ("x", "symbol"))
        claims = (Claim("positive", (Constant("yes"),), id="supported"),
                  Claim("negative", (Constant("no"),), id="refuted"),
                  Claim("positive", (Constant("missing"),), id="unresolved"),
                  Claim("positive", (Constant("both"),), id="conflicting"))
        mapping = EvidenceMapping("positive", "counter", "refutation", bindings=(("x", "x"),),
                                  claim_id="conflicting")
        rules = (Rule(Atom("positive", (Variable("x"),)),
                      (Atom("positive_source", (Variable("x"),)),)),
                 Rule(Atom("negative", (Variable("x"),)),
                      (Atom("negative_source", (Variable("x"),)),)))
        facts = (Atom("positive_source", (Constant("yes"),)),
                 Atom("negative_source", (Constant("no"),)),
                 Atom("positive_source", (Constant("both"),)),
                 Atom("counter", (Constant("both"),)))
        bundle = Bundle((positive_source, negative_source, positive, negative, counter),
                        facts=facts, rules=rules, claims=claims, mappings=(mapping,))
        result = compare(bundle, shrink=False)
        self.assertEqual({claim.key: claim.semantic for claim in result.python.claims},
                         {"supported": "supported", "refuted": "refuted",
                          "unresolved": "unresolved", "conflicting": "conflicting"})

    def test_mixed_support_mappings_require_a_conjunctive_index_run_witness(self):
        valid = mixed_mapping_bundle()
        self.assertEqual(validate_bundle(valid), ())
        supported = compare(valid, shrink=False)
        self.assertEqual(supported.python.claims[0].semantic, "supported")
        self.assertEqual(supported.souffle.claims[0].semantic, "supported")

        missing_fact = mixed_mapping_bundle(witness_fact=False)
        self.assertEqual(validate_bundle(missing_fact), ())
        unresolved = compare(missing_fact, shrink=False)
        self.assertEqual(unresolved.python.claims[0].semantic, "unresolved")
        self.assertEqual(unresolved.souffle.claims[0].semantic, "unresolved")

        mutants = {
            "declaration-only": mixed_mapping_bundle(include_witness=False),
            "wrong-targets": mixed_mapping_bundle(
                witness_targets=("static_seen", "decoy_seen")),
            "wrong-witness-payload": mixed_mapping_bundle(
                witness_payload=("index",)),
            "static-index-unbound": mixed_mapping_bundle(
                static_bindings=(("value", "value"),)),
            "runtime-run-unbound": mixed_mapping_bundle(
                runtime_bindings=(("value", "value"),)),
            "witness-index-unbound": mixed_mapping_bundle(
                witness_bindings=(("run", "run"),)),
            "witness-run-unbound": mixed_mapping_bundle(
                witness_bindings=(("index", "index"),)),
            "swapped-witness-bindings": mixed_mapping_bundle(
                witness_bindings=(("index", "run"), ("run", "index"))),
            "observation-only-witness": mixed_mapping_bundle(
                witness_effect="observation"),
        }
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertIn("mixed-binding-join",
                              {issue.code for issue in validate_bundle(mutant)})
                result = self.assert_invalid_bundle_blocks(name, mutant)
                self.assertEqual(result.python.claims[0].semantic, "unresolved")

    def test_legacy_indexless_static_observations_are_rejected(self):
        claimed = R("legacy_claim", ("tenant", "symbol"), modality="claim")
        static = R("static_route_exists", ("tenant", "symbol", True),
                   ("surface", "symbol", True), binding="static",
                   context_indices=("tenant", "surface"))
        bundle = Bundle((claimed, static),
                        claims=(Claim("legacy_claim", (Constant("t"),), id="legacy"),))
        self.assertIn("static-context", {issue.code for issue in validate_bundle(bundle)})
        self.assert_invalid_bundle_blocks("legacy-indexless-static", bundle)

    def test_corpus_mismatch_and_shared_revocation_have_discriminating_controls(self):
        surface = load_fixture(ROOT / "02-surface-mismatch.json")
        mismatch = compare(surface, shrink=False)
        self.assertEqual(mismatch.python.claims[0].semantic, "unresolved")

        def matching(atom):
            if atom.relation != "runtime_route_observed":
                return atom
            terms = list(atom.terms)
            terms[1] = Constant("route-a", terms[1].type)
            return replace(atom, terms=tuple(terms))

        surface_match = replace(
            surface,
            facts=tuple(matching(atom) for atom in surface.facts),
            evidence=tuple(
                replace(record, atom=matching(record.atom),
                        context=Context.from_mapping({**record.context.as_dict(),
                                                      "surface": "route-a"}))
                if record.atom.relation == "runtime_route_observed"
                else record for record in surface.evidence))
        self.assertEqual(validate_bundle(surface_match), ())
        self.assertEqual(compare(surface_match, shrink=False).python.claims[0].semantic,
                         "supported")
        no_witness_ids = {record.id for record in surface_match.evidence
                          if record.atom.relation == "index_describes_run"}
        no_witness = replace(
            surface_match,
            facts=tuple(atom for atom in surface_match.facts
                        if atom.relation != "index_describes_run"),
            evidence=tuple(record for record in surface_match.evidence
                           if record.id not in no_witness_ids))
        self.assertEqual(validate_bundle(no_witness), ())
        self.assertEqual(compare(no_witness, shrink=False).python.claims[0].semantic,
                         "unresolved")

        shared = load_fixture(ROOT / "07-shared-mistaken-assumption.json")
        self.assertEqual(compare(shared, shrink=False).python.claims[0].semantic,
                         "unresolved")
        independent = replace(
            shared,
            evidence=tuple(replace(record, depends_on=())
                           if record.id in {"fact-static", "fact-runtime"}
                           else record for record in shared.evidence))
        self.assertEqual(validate_bundle(independent), ())
        self.assertEqual(compare(independent, shrink=False).python.claims[0].semantic,
                         "supported")

    def test_variable_valued_mapping_conjunction_is_a_real_relational_join(self):
        unrelated = mixed_mapping_bundle()
        unrelated = with_facts(
            replace(unrelated,
                    claims=(Claim("mixed_claim",
                                  (Variable("index"), Variable("run"), Constant("v")),
                                  id="mixed"),)),
            (Atom("static_seen", (Constant("index-a", "digest"), Constant("v"))),
             Atom("runtime_seen", (Constant("run-b"), Constant("v"))),
             Atom("index_describes_run",
                  (Constant("index-c", "digest"), Constant("run-d")))))
        self.assertEqual(validate_bundle(unrelated), ())
        result = compare(unrelated, shrink=False)
        self.assertEqual(result.python.claims[0].semantic, "unresolved")
        self.assertEqual(result.souffle.claims[0].semantic, "unresolved")

        joined = with_facts(
            unrelated,
            (*unrelated.facts,
             Atom("index_describes_run",
                  (Constant("index-a", "digest"), Constant("run-b")))))
        self.assertEqual(compare(joined, shrink=False).python.claims[0].semantic,
                         "supported")

    def test_mapping_relation_cannot_be_a_decoy_for_the_named_claim(self):
        actual = R("actual_claim", ("x", "symbol"), ("event", "symbol"),
                   modality="claim")
        decoy = R("decoy_claim", ("x", "symbol"), modality="claim")
        observed = R("observed_event", ("x", "symbol"), ("event", "symbol"))
        mapping = EvidenceMapping("decoy_claim", "observed_event", "support",
                                  bindings=(("x", "x"),), claim_id="actual")
        bundle = Bundle(
            (actual, decoy, observed),
            facts=(Atom("observed_event", (Constant("v"), Constant("event-b"))),),
            claims=(Claim("actual_claim", (Constant("v"), Constant("event-a")),
                          id="actual"),), mappings=(mapping,))
        self.assertIn("mapping-claim-relation",
                      {issue.code for issue in validate_bundle(bundle)})
        result = self.assert_invalid_bundle_blocks(
            "mapping-relation-decoy", bundle)
        self.assertEqual(result.python.claims[0].semantic, "unresolved")

    def test_tsv_control_characters_are_reversibly_encoded(self):
        seen = R("seen_tsv", ("x", "symbol"))
        bundle = Bundle((seen,), facts=(Atom("seen_tsv", (Constant("line\tbreak\nnext"),)),))
        result = compare(bundle, shrink=False)
        self.assertTrue(result.matched)
        self.assertEqual(dict(result.souffle.relations)["seen_tsv"],
                         (("line\tbreak\nnext",),))

    def test_json_metadata_runs_in_python_and_remains_replayable_on_a_defect(self):
        metadata = R("metadata", ("payload", "json-metadata-only"))
        atom = Atom("metadata", (Constant({"nested": [1, {"x": True}]},
                                          "json-metadata-only"),))
        bundle = Bundle((metadata,), facts=(atom,),
                        evidence=(Evidence("metadata-evidence", atom, source="test"),))
        self.assertIsNone(run_python(bundle).operational_failure)
        self.assertTrue(compare(bundle, shrink=False).matched)

        def defective(candidate):
            report = run_souffle(candidate)
            if candidate.evidence:
                return replace(report, relations=(*report.relations, ("right_only", (("bad",),))))
            return report

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DifferentialMismatch) as caught:
                compare(bundle, souffle_runner=defective, replay_root=directory)
            replay = Path(caught.exception.result.replay_path)
            self.assertTrue(replay.is_file())
            retained = bundle_from_json(replay.read_text(encoding="utf-8"), validate=True)
            self.assertEqual(tuple(record.id for record in retained.evidence),
                             ("metadata-evidence",))

    def test_nonprimitive_facts_are_rejected_by_both_kernel_boundaries(self):
        derived = R("derived", ("x", "symbol"), primitive=False)
        bundle = Bundle((derived,), facts=(Atom("derived", (Constant("x"),)),),
                        claims=(Claim("derived", (Constant("x"),), id="claim"),))
        self.assertEqual(run_python(bundle).operational_failure, "invalid-input")
        self.assertEqual(run_souffle(bundle).operational_failure, "invalid-input")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DifferentialMismatch):
            compare(bundle, shrink=False, replay_root=directory)

    def test_repeated_claim_variables_are_unified_in_both_kernels(self):
        source = R("pair_source", ("left", "symbol"), ("right", "symbol"))
        pair = R("pair", ("left", "symbol"), ("right", "symbol"),
                 modality="claim", primitive=False)
        rule = Rule(Atom("pair", (Variable("left"), Variable("right"))),
                    (Atom("pair_source", (Variable("left"), Variable("right"))),))
        unequal = Bundle((source, pair),
                         facts=(Atom("pair_source", (Constant("a"), Constant("b"))),),
                         rules=(rule,),
                         claims=(Claim("pair", (Variable("x"), Variable("x")), id="same"),))
        result = compare(unequal, shrink=False)
        self.assertEqual(result.python.claims[0].semantic, "unresolved")
        equal = with_facts(unequal, (Atom("pair_source", (Constant("a"), Constant("a"))),))
        self.assertEqual(compare(equal, shrink=False).python.claims[0].semantic, "supported")

    def test_repeated_variables_are_unified_through_evidence_mapping(self):
        pair = R("mapped_pair", ("left", "symbol"), ("right", "symbol"), modality="claim")
        source = R("mapped_source", ("source_left", "symbol"), ("source_right", "symbol"))
        mapping = EvidenceMapping("mapped_pair", "mapped_source", "support",
                                  bindings=(("left", "source_left"),
                                            ("right", "source_right")), claim_id="same")
        claim = Claim("mapped_pair", (Variable("x"), Variable("x")), id="same")
        unequal = Bundle((pair, source),
                         facts=(Atom("mapped_source", (Constant("a"), Constant("b"))),),
                         claims=(claim,), mappings=(mapping,))
        self.assertEqual(compare(unequal, shrink=False).python.claims[0].semantic,
                         "unresolved")
        equal = with_facts(unequal, (Atom("mapped_source", (Constant("a"), Constant("a"))),))
        self.assertEqual(compare(equal, shrink=False).python.claims[0].semantic,
                         "supported")

    def test_revoked_path_preserves_independent_support_and_blocks_tainted_refutation(self):
        supported = compare(revoked_bundle(), shrink=False)
        self.assertEqual(supported.python.claims[0].semantic, "supported")
        refuted = compare(revoked_bundle(mapped_refutation=True), shrink=False)
        self.assertEqual(refuted.python.claims[0].semantic, "refuted")
        tainted_only = revoked_bundle(mapped_refutation=True)
        tainted_only = replace(tainted_only,
                               evidence=tuple(record for record in tainted_only.evidence
                                              if record.id != "z-independent"))
        comparison = compare(tainted_only, shrink=False)
        self.assertEqual(comparison.python.claims[0].semantic, "unresolved")

    def test_revocation_is_scoped_to_the_matching_assumption_row_and_context(self):
        rejected = R("source__rejected", ("tenant", "symbol", True), ("name", "symbol"),
                     modality="assumption", context_indices=("tenant",))
        observed = R("observed", ("tenant", "symbol", True), ("x", "symbol"),
                     context_indices=("tenant",))
        claimed = R("claimed", ("tenant", "symbol", True), ("x", "symbol"),
                    modality="claim", primitive=False, context_indices=("tenant",))
        blocked = Atom("source__rejected", (Constant("t1"), Constant("revoked")))
        same_context_other = Atom("source__rejected", (Constant("t1"), Constant("other")))
        other_context = Atom("source__rejected", (Constant("t2"), Constant("revoked")))
        seen = Atom("observed", (Constant("t1"), Constant("v")))
        evidence = (
            Evidence("blocked", blocked, Context.from_mapping({"tenant": "t1"}), source="test"),
            Evidence("same-context-other", same_context_other,
                     Context.from_mapping({"tenant": "t1"}), source="test"),
            Evidence("other-context", other_context,
                     Context.from_mapping({"tenant": "t2"}), source="test"),
            Evidence("a-tainted", seen, Context.from_mapping({"tenant": "t1"}),
                     source="test", depends_on=("blocked",)),
            Evidence("z-surviving", seen, Context.from_mapping({"tenant": "t1"}),
                     source="test", depends_on=("same-context-other", "other-context")),
        )
        diagnostic = DiagnosticRule("source__rejected", "forbidden", context_indices=("tenant",),
                                    claim_id="claim",
                                    predicate=(("column", "name"), ("operator", "="),
                                               ("value", "revoked")))
        rule = Rule(Atom("claimed", (Variable("tenant"), Variable("x"))),
                    (Atom("observed", (Variable("tenant"), Variable("x"))),), "derive")
        bundle = Bundle((rejected, observed, claimed),
                        facts=(blocked, same_context_other, other_context, seen), evidence=evidence,
                        rules=(rule,), claims=(Claim("claimed", (Variable("tenant"), Constant("v")),
                                                     Context.from_mapping({"tenant": "t1"}), id="claim"),),
                        diagnostics=(diagnostic,))
        result = compare(bundle, shrink=False)
        self.assertEqual(result.python.claims[0].semantic, "supported")
        tainted_only = replace(bundle, evidence=tuple(record for record in evidence
                                                       if record.id != "z-surviving"))
        self.assertEqual(compare(tainted_only, shrink=False).python.claims[0].semantic, "unresolved")

    def test_alternative_provenance_overflow_fails_closed_and_blocks_admission(self):
        bundle = alternative_overflow_bundle()
        python = run_python(bundle); souffle = run_souffle(bundle)
        self.assertEqual(python.operational_failure, "resource-exhausted")
        self.assertEqual(souffle.claims[0].semantic, "supported")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DifferentialMismatch):
            compare(bundle, shrink=False, replay_root=directory)

    def test_raw_fixture_fact_and_evidence_permutations_normalize_identically(self):
        path = ROOT / "01-correlated-positive.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        permuted = dict(raw)
        for key in ("facts", "assumptions", "claims", "rules", "outputs"):
            permuted[key] = list(reversed(raw[key]))
        from .adapter import bundle_payload
        from capcov.claims import bundle_from_json
        original = load_fixture(path)
        shuffled = bundle_from_json(bundle_payload(permuted), validate=True)
        # Fixture metadata intentionally retains producer order for audit.  The
        # executable semantic bundle (metadata excluded) and both kernels are
        # invariant to raw fact/evidence/rule ordering.
        self.assertEqual(digest(replace(original, metadata=())),
                         digest(replace(shuffled, metadata=())))
        self.assertEqual(run_python(original).canonical_digest, run_python(shuffled).canonical_digest)
        self.assertEqual(run_souffle(original).canonical_digest, run_souffle(shuffled).canonical_digest)

    def test_reversed_inputs_have_identical_bundle_relation_and_report_digests(self):
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            bundle = load_fixture(path)
            reversed_bundle = replace(bundle, relations=tuple(reversed(bundle.relations)),
                                      facts=tuple(reversed(bundle.facts)), rules=tuple(reversed(bundle.rules)),
                                      claims=tuple(reversed(bundle.claims)), evidence=tuple(reversed(bundle.evidence)),
                                      mappings=tuple(reversed(bundle.mappings)), diagnostics=tuple(reversed(bundle.diagnostics)))
            with self.subTest(case=path.stem):
                self.assertEqual(digest(bundle), digest(reversed_bundle))
                py_a, py_b = run_python(bundle), run_python(reversed_bundle)
                sf_a, sf_b = run_souffle(bundle), run_souffle(reversed_bundle)
                self.assertEqual(py_a.relations, py_b.relations)
                self.assertEqual(sf_a.relations, sf_b.relations)
                self.assertEqual(py_a.canonical_digest, py_b.canonical_digest)
                self.assertEqual(sf_a.canonical_digest, sf_b.canonical_digest)


if __name__ == "__main__": unittest.main()
