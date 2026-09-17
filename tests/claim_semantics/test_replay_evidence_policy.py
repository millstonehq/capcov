"""Why-not for the replay judge: an unresolved op_qualified names the absent leaf (Phase 4, judge side).

Mirrors ``test_static_evidence_policy``'s ``MISSING_RUNTIME`` pattern over the
exporter's own output for ``fixtures/replay_receipt_min`` combined with the
replay rule pack.  Without the reviewer's ``snapshot_observed`` row the claim
``op_qualified(index, run, issues.close)`` is unresolved with
``replay_run_current`` as the declared missing premise; adding that one fact
flips it to supported with leaves spanning every producer class, the
certificates re-derived from the Python and the Soufflé closures are identical
and each rechecks against the other's rows.  The same join without the
``index_describes_replay`` witness does not validate (``mixed-binding-join``),
and the combiner refuses a stub that disagrees with the pack.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from capcov.claims import (Atom, Bundle, Claim, Column, Constant, Context, DiagnosticRule, OutputTemplate,
                           RelationDecl, Rule, TemplateValue, Variable, VerifiedProofEvidence, bundle_from_json,
                           render_outputs, validate_bundle)
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.replay import replay_facts
from capcov.claims.static import certificate
from capcov.claims.static.combine import CombineError, combine

try:
    from .replay_rules import cases
    from .replay_rules.adapter import (CASES_DIR, bundle_payload, load_case, load_pack, pack_bundle,
                                       read_json)
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules import cases
    from replay_rules.adapter import (CASES_DIR, bundle_payload, load_case, load_pack, pack_bundle,
                                      read_json)

CLAIM_ID = "claim-close-qualified"
REASON = "the run's snapshot digest was not observed, so replay_run_current does not hold"
MISSING_CURRENT = OutputTemplate(
    "missing_premise", CLAIM_ID, relation="replay_run_current",
    fields=(("reason", TemplateValue("constant", "", "symbol", REASON)),),
    when_claim="unresolved")
WITHOUT_WITNESS = Rule(
    Atom("op_qualified", (Variable("IX"), Variable("Run"), Variable("Op"))),
    (Atom("op_declared", (Variable("IX"), Variable("Op"))),
     Atom("op_qualified_rt", (Variable("IX"), Variable("Run"), Variable("Op")))),
    name="op_qualified_unwitnessed")


def _claim() -> Claim:
    return Claim("op_qualified", (Constant(cases.INDEX, "digest"), Constant(cases.RUN, "symbol"),
                                  Constant(cases.CLOSE, "symbol")),
                 Context.from_mapping({"index": cases.INDEX, "run": cases.RUN}), id=CLAIM_ID)


def _build(*, snapshot: bool, pack: Bundle | None = None, validate: bool = True):
    exported = replay_facts.export_bundle(cases.FIXTURE, run=cases.RUN, describes_indexes=(cases.INDEX,))
    assert exported.status == replay_facts.STATUS_COMPLETE, exported.messages
    pack = pack or pack_bundle()
    decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
    entries = cases.reviewer_facts(snapshot=cases.SNAPSHOT if snapshot else None) + cases.census_facts()
    additions = cases.ir_facts(entries, decls)
    diagnostics = [DiagnosticRule("snapshot_observed", "observation", "complete", (), claim_id=CLAIM_ID),
                   DiagnosticRule("model_describes_run", "observation", "complete", ("run",), claim_id=CLAIM_ID)]
    outputs = [MISSING_CURRENT]
    if snapshot:
        snapshot_id = next(record.id for _, record in additions if record.atom.relation == "snapshot_observed")
        outputs.append(OutputTemplate("observed", CLAIM_ID, evidence_id=snapshot_id))
        describes = next(record.id for record in exported.bundle.evidence
                         if record.atom.relation == "model_describes_run")
        outputs.append(OutputTemplate("observed", CLAIM_ID, evidence_id=describes))
    bundle = combine(exported.bundle, pack, facts=[atom for atom, _ in additions],
                     evidence=[record for _, record in additions], claims=[_claim()],
                     diagnostics=diagnostics, outputs=outputs, validate=validate)
    return bundle


class WithheldClosureFlipsBackTest(unittest.TestCase):
    """Case 21's why-not names ``php_effect_seqs_closed``; putting that one leaf
    back flips all three op_qualified claims to supported.  The corpus pins the
    unresolved half; without this the "and adding it flips the claim" half of the
    policy is only pinned for ``snapshot_observed`` (above)."""

    def _case_with_the_witness(self):
        case = read_json(CASES_DIR / "21-missing-effect-seq-closure.json")
        control = read_json(CASES_DIR / "00-positive-control.json")
        donor = next(f for f in control["facts"] if f["relation"] == "php_effect_seqs_closed")
        identity = next(f["id"].split(":")[1] for f in case["facts"] if f["relation"] == "go_effect_seqs_closed")
        run_row = next(f["id"] for f in case["facts"] if f["relation"] == "replay_run")
        restored = dict(donor, id=f"replay:{identity}:php_effect_seqs_closed:{donor['id'].split(':')[3]}",
                        provenance={"depends_on": [run_row]})
        case = dict(case, facts=[*case["facts"], restored])
        return case, restored["id"]

    def test_the_named_leaf_is_the_one_that_flips_every_claim(self) -> None:
        pack = load_pack()
        withheld = evaluate(load_case(CASES_DIR / "21-missing-effect-seq-closure.json", pack))
        self.assertEqual(withheld.status.value, "complete", withheld.message)
        for entry in withheld.claims:
            self.assertEqual(entry.result.semantic.value, "unresolved", entry.claim.id)
            self.assertEqual([item["relation"] for item in entry.result.missing_premises],
                             ["php_effect_seqs_closed"], entry.claim.id)
        self.assertEqual(withheld.relation_rows("op_qualified"), ())

        case, restored_id = self._case_with_the_witness()
        restored = evaluate(bundle_from_json(bundle_payload(case, pack), validate=True))
        self.assertEqual(restored.status.value, "complete", restored.message)
        self.assertEqual(len(restored.relation_rows("op_qualified")), 3)
        for entry in restored.claims:
            self.assertEqual(entry.result.semantic.value, "supported", entry.claim.id)
            self.assertEqual(entry.result.missing_premises, (), entry.claim.id)
            self.assertIn(restored_id, entry.result.support,
                          "the restored witness is a leaf of every certificate it unblocked")


class ReplayEvidencePolicyTest(unittest.TestCase):
    def test_without_the_snapshot_witness_the_claim_is_unresolved_naming_replay_run_current(self) -> None:
        bundle = _build(snapshot=False)
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "complete", report.message)
        [entry] = report.claims
        self.assertEqual(entry.result.semantic.value, "unresolved")
        self.assertEqual(entry.result.operational.value, "complete")
        self.assertEqual(entry.result.support, ())
        self.assertEqual(entry.result.missing_premises, ({"relation": "replay_run_current", "reason": REASON},))
        self.assertEqual(report.relation_rows("replay_run_current"), ())
        self.assertEqual(report.relation_rows("op_qualified"), ())
        self.assertEqual(report.relation_rows("snapshot_observed"), ())
        # every other input to qualification is present: the closures are the only gap
        self.assertEqual(len(report.relation_rows("php_model_agree")), 5)
        self.assertEqual(set(report.relation_rows("corpus_constrains")),
                         {(cases.RUN, op) for op in (cases.CREATE, cases.CLOSE, cases.DELETE)})
        self.assertEqual(report.relation_rows("php_disagreement_closed"), ())
        active = {record.id for record in bundle.evidence}
        rendered = render_outputs(bundle, CLAIM_ID, active, None, "unresolved", {"replay_run_current"})
        self.assertEqual(rendered, ({"kind": "missing_premise", "claim_id": CLAIM_ID,
                                     "relation": "replay_run_current", "fields": {"reason": REASON}},))

    def test_adding_the_snapshot_witness_supports_the_claim_with_leaves_from_every_producer(self) -> None:
        bundle = _build(snapshot=True)
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "complete", report.message)
        [entry] = report.claims
        self.assertEqual(entry.result.semantic.value, "supported")
        self.assertEqual(entry.result.operational.value, "complete")
        self.assertEqual(entry.result.missing_premises, ())
        leaves = set(entry.result.support)
        self.assertEqual({leaf.split(":")[0] for leaf in leaves},
                         {"replay", "php", "go", "shen", "mut", "reviewer", "modelcheck"})
        by_id = {record.id: record for record in bundle.evidence}
        self.assertTrue(leaves <= set(by_id))
        classes = {by_id[leaf].source.split(" ", 1)[0] for leaf in leaves}
        self.assertTrue({"replay", "php", "go", "shen", "mut", "reviewer", "php-census", "modelcheck"} <= classes)
        self.assertIn("snapshot_observed", {leaf.split(":")[2] for leaf in leaves})
        self.assertEqual(set(report.relation_rows("op_qualified")),
                         {(cases.INDEX, cases.RUN, op) for op in (cases.CREATE, cases.CLOSE, cases.DELETE)})
        proof = VerifiedProofEvidence.from_bundle(bundle, CLAIM_ID, leaves)
        rendered = render_outputs(bundle, CLAIM_ID, set(by_id), proof, "supported")
        observed = {item["evidence_id"] for item in rendered if item["kind"] == "observed"}
        self.assertEqual({item.split(":")[2] for item in observed}, {"snapshot_observed", "model_describes_run"})
        self.assertTrue(observed <= leaves)
        self.assertFalse(any(item["kind"] == "missing_premise" for item in rendered))
        # unlike the static shape, the two witnesses are relevant through the
        # claim's observation diagnostics (what lets excludes_evidence suppress a
        # why-not template), so they render without a proof as well; nothing else does
        without_proof = render_outputs(bundle, CLAIM_ID, set(by_id), None, "supported")
        self.assertEqual({item["evidence_id"] for item in without_proof}, observed)
        self.assertEqual({item["kind"] for item in without_proof}, {"observed"})
        with self.assertRaises(ValueError):
            render_outputs(bundle, "other-claim", set(by_id), proof, "supported")

    def test_both_kernels_agree_on_both_shapes_and_certify_the_flip_identically(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        replay_root = tempfile.mkdtemp(prefix="capcov-replay-evidence-policy-")
        try:
            for snapshot in (False, True):
                bundle = _build(snapshot=snapshot)
                with self.subTest(snapshot=snapshot):
                    try:
                        result = compare(bundle, replay_root=replay_root)
                    except DifferentialMismatch as exc:
                        self.fail(f"kernels disagree; replay bundle: {exc.result.replay_path}")
                    self.assertTrue(result.matched)
                    [python_claim] = result.python.claims
                    [souffle_claim] = result.souffle.claims
                    self.assertEqual(python_claim.semantic, "supported" if snapshot else "unresolved")
                    self.assertEqual(souffle_claim.semantic, python_claim.semantic)
                    self.assertEqual(souffle_claim.missing_premises, python_claim.missing_premises)
                    [claim] = bundle.claims
                    rows = certificate.claim_conclusions(bundle, result.python.relations, claim)
                    self.assertEqual(rows, certificate.claim_conclusions(bundle, result.souffle.relations, claim))
                    self.assertEqual(len(rows), 1 if snapshot else 0)
                    for row in rows:
                        from_python = certificate.certify(bundle, result.python.relations, claim.relation, row)
                        from_souffle = certificate.certify(bundle, result.souffle.relations, claim.relation, row)
                        self.assertEqual(from_python, from_souffle)
                        self.assertFalse(from_python["truncated"])
                        self.assertTrue(certificate.recheck(bundle, from_python, result.souffle.relations).ok)
                        self.assertTrue(certificate.recheck(bundle, from_souffle, result.python.relations).ok)
                        self.assertIn("snapshot_observed", {leaf.split(":")[2] for leaf in from_python["leaves"]})
        finally:
            shutil.rmtree(replay_root, ignore_errors=True)

    def test_the_join_without_index_describes_replay_fails_validation(self) -> None:
        pack = pack_bundle()
        unwitnessed = Bundle(pack.relations, rules=tuple(rule for rule in pack.rules if rule.name != "op_qualified")
                             + (WITHOUT_WITNESS,), diagnostic_policy=pack.diagnostic_policy)
        bundle = _build(snapshot=True, pack=unwitnessed, validate=False)
        codes = {issue.code for issue in validate_bundle(bundle)}
        self.assertIn("mixed-binding-join", codes)
        with self.assertRaises(ValueError):
            _build(snapshot=True, pack=unwitnessed)

    def test_combiner_refuses_a_stub_that_disagrees_with_the_pack(self) -> None:
        exported = replay_facts.export_bundle(cases.FIXTURE, run=cases.RUN)
        pack = pack_bundle()
        widened = RelationDecl("op_qualified_rt",
                               (Column("index", "digest", True), Column("run", "symbol", True),
                                Column("op", "symbol"), Column("req", "symbol")),
                               modality="derived", binding="runtime", primitive=False,
                               context_indices=("index", "run"))
        with self.assertRaises(CombineError):
            combine(exported.bundle, pack, Bundle((widened,)))
        merged = combine(exported.bundle, pack)
        self.assertEqual(len({decl.name for decl in merged.relations}), len(merged.relations))
        self.assertEqual({decl.name for decl in exported.bundle.relations} & {d.name for d in pack.relations}
                         - {d.name for d in pack.relations if d.primitive}, set(replay_facts.STUB_RELATIONS))


if __name__ == "__main__":
    unittest.main()
