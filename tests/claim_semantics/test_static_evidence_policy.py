"""A static observation joined to a runtime observation through index_describes_run (section 29).

The claim ``runtime_route_statically_declared(tenant, surface, run, index)``
holds when a route observed at runtime is a declared static surface of an
index that describes that run.  Static-only input leaves it unresolved with
``runtime_route_observed`` as the declared missing premise; adding the runtime
rows and the ``index_describes_run`` witness supports it with mixed
``scip:``/``static:``/``runtime:`` leaves, which then flow through
``render_outputs`` under a ``VerifiedProofEvidence``.  The same join without
the witness does not validate (``mixed-binding-join``).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from capcov.claims import (Atom, Bundle, Claim, Column, Constant, Context, OutputTemplate,
                           RelationDecl, Rule, TemplateValue, Variable, VerifiedProofEvidence,
                           render_outputs, validate_bundle)
from capcov.claims.differential import DifferentialMismatch, compare
from capcov.claims.evaluator import evaluate
from capcov.claims.static.combine import CombineError, combine

try:
    from .static_rules import go_app
    from .static_rules.adapter import pack_bundle
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules import go_app
    from static_rules.adapter import pack_bundle

CLAIM_ID = "claim-route-exercised-at-runtime"
DECLARED = RelationDecl(
    "runtime_route_statically_declared",
    (Column("tenant", "symbol", True), Column("surface", "symbol", True),
     Column("run", "symbol", True), Column("index", "digest", True)),
    modality="claim", binding="runtime", primitive=False,
    context_indices=("tenant", "surface", "run", "index"))
HEAD = Atom("runtime_route_statically_declared",
            (Variable("T"), Variable("S"), Variable("Run"), Variable("IX")))
RUNTIME_ATOM = Atom("runtime_route_observed", (Variable("T"), Variable("S"), Variable("Event"), Variable("Run")))
WITNESS_ATOM = Atom("index_describes_run", (Variable("IX"), Variable("Run")))
STATIC_ATOM = Atom("static_route_declared_surface", (Variable("IX"), Variable("S")))
INDEX_ATOM = Atom("scip_index", (Variable("IX"), Variable("Indexer"), Variable("Version"), Variable("Lang"),
                                 Variable("Root"), Variable("Kind")))
WITH_WITNESS = Rule(HEAD, (RUNTIME_ATOM, WITNESS_ATOM, STATIC_ATOM, INDEX_ATOM), name="route_exercised_at_runtime")
WITHOUT_WITNESS = Rule(HEAD, (RUNTIME_ATOM, STATIC_ATOM, INDEX_ATOM), name="route_exercised_at_runtime_unwitnessed")


def _claim(index: str) -> Claim:
    return Claim("runtime_route_statically_declared",
                 (Constant(go_app.TENANT, "symbol"), Constant(go_app.SURFACE, "symbol"),
                  Constant(go_app.RUN, "symbol"), Constant(index, "digest")),
                 Context.from_mapping({"tenant": go_app.TENANT, "surface": go_app.SURFACE,
                                       "run": go_app.RUN, "index": index}),
                 id=CLAIM_ID)


MISSING_RUNTIME = OutputTemplate(
    "missing_premise", CLAIM_ID, relation="runtime_route_observed",
    fields=(("reason", TemplateValue("constant", "", "symbol",
                                     "no runtime observation of the surface in a run this index describes")),),
    when_claim="unresolved")


def _build(*, runtime: bool, rule: Rule = WITH_WITNESS, validate: bool = True):
    exported = go_app.export(describes_runs=(go_app.RUN,) if runtime else ())
    assert exported.status == "complete", exported.messages
    pack = pack_bundle()
    decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
    index = dict(exported.bundle.metadata)["index_digest"]
    additions = [go_app.source_tree_fact(decls, dict(dict(exported.bundle.metadata)["tree"])["digest"])]
    outputs = [MISSING_RUNTIME]
    if runtime:
        additions.extend(go_app.runtime_route_facts(decls, [(go_app.SURFACE, "evt-1")]))
        outputs.extend(OutputTemplate("observed", CLAIM_ID, evidence_id=record.id)
                       for _, record in additions[1:])
        describes = next(record.id for record in exported.bundle.evidence
                         if record.atom.relation == "index_describes_run")
        outputs.append(OutputTemplate("observed", CLAIM_ID, evidence_id=describes))
    bundle = combine(exported.bundle, pack, Bundle((DECLARED,), rules=(rule,)),
                     facts=[atom for atom, _ in additions], evidence=[record for _, record in additions],
                     claims=[_claim(index)], outputs=outputs, validate=validate)
    return bundle, index


class StaticEvidencePolicyTest(unittest.TestCase):
    def test_static_only_bundle_is_unresolved_with_the_runtime_relation_as_missing_premise(self) -> None:
        bundle, _ = _build(runtime=False)
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "complete", report.message)
        [entry] = report.claims
        self.assertEqual(entry.result.semantic.value, "unresolved")
        self.assertEqual(entry.result.operational.value, "complete")
        self.assertEqual(entry.result.support, ())
        self.assertEqual(entry.result.missing_premises, (
            {"relation": "runtime_route_observed",
             "reason": "no runtime observation of the surface in a run this index describes"},))
        self.assertEqual(report.relation_rows("runtime_route_statically_declared"), ())
        self.assertEqual(report.relation_rows("runtime_route_observed"), ())
        self.assertEqual(report.relation_rows("index_describes_run"), ())
        active = {record.id for record in bundle.evidence}
        rendered = render_outputs(bundle, CLAIM_ID, active, None, "unresolved", {"runtime_route_observed"})
        self.assertEqual(rendered, ({"kind": "missing_premise", "claim_id": CLAIM_ID,
                                     "relation": "runtime_route_observed",
                                     "fields": {"reason": "no runtime observation of the surface in a run this index describes"}},))

    def test_runtime_observation_with_the_witness_supports_the_claim_with_mixed_leaves(self) -> None:
        bundle, index = _build(runtime=True)
        report = evaluate(bundle)
        self.assertEqual(report.status.value, "complete", report.message)
        [entry] = report.claims
        self.assertEqual(entry.result.semantic.value, "supported")
        self.assertEqual(entry.result.operational.value, "complete")
        self.assertEqual(entry.result.missing_premises, ())
        leaves = set(entry.result.support)
        self.assertEqual({leaf.split(":")[0] for leaf in leaves}, {"scip", "static", "runtime"})
        self.assertEqual({leaf.split(":")[2] for leaf in leaves},
                         {"runtime_route_observed", "index_describes_run", "route_site", "scip_index"})
        known = {record.id for record in bundle.evidence}
        self.assertTrue(leaves <= known)
        self.assertEqual(set(report.relation_rows("runtime_route_statically_declared")),
                         {(go_app.TENANT, go_app.SURFACE, go_app.RUN, index)})
        # the proof flows through output rendering: observed items for the
        # runtime row and the compatibility witness render under the proof
        proof = VerifiedProofEvidence.from_bundle(bundle, CLAIM_ID, leaves)
        rendered = render_outputs(bundle, CLAIM_ID, known, proof, "supported")
        observed = {item["evidence_id"] for item in rendered if item["kind"] == "observed"}
        self.assertEqual({item.split(":")[2] for item in observed}, {"runtime_route_observed", "index_describes_run"})
        self.assertTrue(observed <= leaves)
        self.assertFalse(any(item["kind"] == "missing_premise" for item in rendered))
        # without the proof the same evidence is not rendered as observed
        self.assertEqual(render_outputs(bundle, CLAIM_ID, known, None, "supported"), ())
        # a proof for another claim is refused at the rendering boundary
        with self.assertRaises(ValueError):
            render_outputs(bundle, "other-claim", known, proof, "supported")

    def test_both_kernels_agree_on_both_shapes(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")
        replay_root = tempfile.mkdtemp(prefix="capcov-static-evidence-policy-")
        try:
            for runtime in (False, True):
                bundle, _ = _build(runtime=runtime)
                with self.subTest(runtime=runtime):
                    try:
                        result = compare(bundle, replay_root=replay_root)
                    except DifferentialMismatch as exc:
                        self.fail(f"kernels disagree; replay bundle: {exc.result.replay_path}")
                    self.assertTrue(result.matched)
                    [python_claim] = result.python.claims
                    self.assertEqual(python_claim.semantic, "supported" if runtime else "unresolved")
        finally:
            shutil.rmtree(replay_root, ignore_errors=True)

    def test_the_join_without_index_describes_run_fails_validation(self) -> None:
        bundle, _ = _build(runtime=True, rule=WITHOUT_WITNESS, validate=False)
        codes = {issue.code for issue in validate_bundle(bundle)}
        self.assertIn("mixed-binding-join", codes)
        with self.assertRaises(ValueError):
            _build(runtime=True, rule=WITHOUT_WITNESS)

    def test_combiner_refuses_a_conflicting_duplicate_declaration(self) -> None:
        exported = go_app.export()
        pack = pack_bundle()
        widened = RelationDecl("static_reaches",
                               (Column("index", "digest", True), Column("src", "symbol"),
                                Column("dst", "symbol"), Column("hops", "unsigned")),
                               modality="derived", binding="static", primitive=False,
                               context_indices=("index",))
        with self.assertRaises(CombineError):
            combine(exported.bundle, pack, Bundle((widened,)))
        merged = combine(exported.bundle, pack)
        self.assertEqual(len({decl.name for decl in merged.relations}), len(merged.relations))
        self.assertEqual({decl.name for decl in exported.bundle.relations} & {d.name for d in pack.relations}
                         - {d.name for d in pack.relations if d.primitive},
                         {"scip_document_path", "scip_definition_site_at", "static_route_declared_surface",
                          "static_reaches", "runtime_route_reaches_sql_on_index"})


if __name__ == "__main__":
    unittest.main()
