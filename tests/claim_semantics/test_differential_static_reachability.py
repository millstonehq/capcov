"""go_app golden index + rule pack: identical closure and verdicts in both kernels (section 29).

The bundle is ``static_rules.go_app.go_app_bundle()``: the exporter over the
committed golden ``scip_go_app_index.json`` (``normalize_scip_json(golden,
retain=True)``), a hand-built tree-sitter side for the route and the two op
sites, the reviewed rule pack, the claim-time ``source_tree_observed`` row,
two runtime route observations with ``index_describes_run``, the hop-count
cross-check relations and seven claims.  Soufflé is required, not optional:
a missing kernel fails the test, it does not skip it (the gate rejects
"skipped" output).
"""
from __future__ import annotations

import copy
import random
import shutil
import tempfile
import unittest

from capcov.claims import digest
from capcov.claims.differential import DifferentialMismatch, compare, run_python
from capcov.claims.evaluator import evaluate
from capcov.claims.static import scip_facts

try:
    from .static_rules import go_app
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules import go_app

# sha256 of the canonical exported bundle of the golden (facts, evidence,
# declarations, metadata minus the run receipts; no claims, no rules).  It is
# reproducible across machines because scip_index.project_root is canonicalized
# (item 3), the golden carries no host path, and the index identity is the
# static-relations-v1 content digest; a change here is a change of exporter
# output or of the golden and must be explained in section 29.
# Digest of the exported go_app bundle's canonical JSON.  It changed once at the
# merge of the kernel line (8a54a04): Context is now serialised as a flat object
# instead of the {"values": [...]} dataclass wrapper, which alters every evidence
# record's canonical bytes.  The static index identity (GOLDEN_INDEX, derived
# from the exported relation contract, not from IR serialisation) changes when
# that contract changes; the sibling tests assert facts, evidence ids and closure.
# It changed a second time at the publication rewrite (b9353b7): the producer
# class admitted by the exporter's stub declarations for the causal-trace
# primitives was renamed, and every exported bundle carries those declarations.
# Facts, evidence ids, closure and the static identity are unchanged.
EXPORTED_BUNDLE_DIGEST = "2ce5f5c33d3bed224de0187a658e6e431e1ae9d0f80b9d786bfd2bf1a541a536"
EXPORTED_FACT_COUNT = 210
# The golden's static-relations-v1 identity (metadata index_digest, every
# fact's index column, the <index12> of every evidence id).
GOLDEN_INDEX = "f0ef037bd20151829fc8b59fb5cbbabf138017546aacab9f574b8ea049c18b23"

IX = go_app.index_digest()
EXPECTED_REACHES = {(IX, go_app.GET_JOB, dst) for dst in (
    go_app.FETCH, go_app.REPO_GET, go_app.REPO_WRITE, go_app.DB_FIRST, go_app.DB_CREATE)}
EXPECTED_CAPABILITY_OPS = {(IX, go_app.SURFACE, "jobs", "read"), (IX, go_app.SURFACE, "audit_logs", "create")}
EXPECTED_VERDICTS = {
    "claim-cap-jobs-read": ("supported", "complete", "derivational"),
    "claim-cap-audit-create": ("supported", "complete", "derivational"),
    "claim-reaches-repo-get": ("supported", "complete", "derivational"),
    "claim-reaches-repo-write": ("supported", "complete", "derivational"),
    # Register references GetJob (function as value), not the other way round;
    # nothing reaches Register from the one route root.
    "claim-reaches-register": ("unresolved", "complete", "derivational"),
    # go_app has no authorization symbol, so the universal stays unresolved with
    # the domain closed (inventory witness present) and one member unproven.
    "claim-all-routes-authorized": ("unresolved", "complete", "bounded-history-model"),
    # POST /jobs was observed at runtime, is absent from the closed route
    # inventory, and the index describes run-1: the negative claim derives.
    "claim-runtime-route-gap": ("refuted", "complete", "derivational"),
}


class GoAppDifferentialTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle, cls.exported = go_app.go_app_bundle()
        cls.replay_root = tempfile.mkdtemp(prefix="capcov-go-app-differential-")
        cls.result = None
        cls.mismatch = None
        try:
            cls.result = compare(cls.bundle, replay_root=cls.replay_root)
        except DifferentialMismatch as exc:
            cls.mismatch = exc.result

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.mismatch is None:
            shutil.rmtree(cls.replay_root, ignore_errors=True)

    def result_or_fail(self):
        if self.mismatch is not None:
            r = self.mismatch
            self.fail(f"kernels disagree on go_app; replay bundle: {r.replay_path}; "
                      f"python={r.python.operational_failure!r} souffle={r.souffle.operational_failure!r} "
                      f"{r.souffle.message[:600]}")
        return self.result

    def test_souffle_is_present_not_skipped(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")

    def test_export_is_complete_and_its_digest_is_the_committed_one(self) -> None:
        self.assertEqual(self.exported.status, scip_facts.STATUS_COMPLETE, self.exported.messages)
        self.assertEqual(len(self.exported.bundle.facts), EXPORTED_FACT_COUNT)
        self.assertEqual(scip_facts.bundle_digest(self.exported.bundle), EXPORTED_BUNDLE_DIGEST)
        meta = dict(self.exported.bundle.metadata)
        self.assertEqual(meta["index_digest"], IX)
        self.assertEqual(IX, GOLDEN_INDEX)
        self.assertEqual(meta["index_digest_kind"], scip_facts.INDEX_IDENTITY)
        # the file receipt is carried, reported, and is not the identity
        self.assertEqual(meta["index_file_digest"], go_app.json_file_digest())
        self.assertEqual(meta["index_file_digest_kind"], "json")
        self.assertNotEqual(meta["index_file_digest"], IX)
        self.assertTrue(any(meta["index_file_digest"] in m and "receipt" in m for m in self.exported.messages))
        self.assertEqual(meta["line_frame"], "1-based")
        # the golden has no project_root (canonicalize.jq drops it); the fact is empty, not a host path
        [[_, _, _, project_root, kind]] = [[t.value for t in f.terms][1:] for f in self.exported.bundle.facts
                                          if f.relation == "scip_index"]
        self.assertEqual((project_root, kind), ("", scip_facts.INDEX_IDENTITY))
        self.assertTrue(all(record.id.split(":")[1] == IX[:12] for record in self.exported.bundle.evidence))

    def test_kernels_agree_on_every_relation_and_claim(self) -> None:
        result = self.result_or_fail()
        self.assertTrue(result.matched)
        self.assertIsNone(result.python.operational_failure)
        self.assertIsNone(result.souffle.operational_failure)
        self.assertEqual(result.python.canonical_digest, result.souffle.canonical_digest)
        self.assertEqual(dict(result.python.relations), dict(result.souffle.relations))
        self.assertEqual(result.python.claims, result.souffle.claims)

    def test_static_reaches_is_the_two_hop_chain_from_the_route_handler(self) -> None:
        relations = dict(self.result_or_fail().python.relations)
        self.assertEqual(set(relations["static_reaches"]), EXPECTED_REACHES)
        self.assertEqual(set(relations["static_root"]), {(IX, go_app.GET_JOB)})
        self.assertEqual(set(relations["static_route_handler"]), {(IX, go_app.SURFACE, go_app.GET_JOB)})
        self.assertEqual(set(relations["static_edge"]), {
            (IX, go_app.GET_JOB, go_app.FETCH), (IX, go_app.FETCH, go_app.REPO_GET),
            (IX, go_app.FETCH, go_app.REPO_WRITE), (IX, go_app.REPO_GET, go_app.DB_FIRST),
            (IX, go_app.REPO_WRITE, go_app.DB_CREATE), (IX, go_app.REGISTER, go_app.GET_JOB),
            (IX, go_app.REGISTER, go_app.HANDLE)})
        hops = {(root, dst): hop for _, root, dst, hop in relations["static_reaches_within"]}
        self.assertEqual(hops, {(go_app.GET_JOB, go_app.FETCH): 1, (go_app.GET_JOB, go_app.REPO_GET): 2,
                                (go_app.GET_JOB, go_app.REPO_WRITE): 2, (go_app.GET_JOB, go_app.DB_FIRST): 3,
                                (go_app.GET_JOB, go_app.DB_CREATE): 3})

    def test_static_capability_op_rows(self) -> None:
        relations = dict(self.result_or_fail().python.relations)
        self.assertEqual(set(relations["static_capability_op"]), EXPECTED_CAPABILITY_OPS)
        self.assertEqual(set(relations["static_capability"]), EXPECTED_CAPABILITY_OPS)
        self.assertEqual(set(relations["runtime_route_without_static"]),
                         {(go_app.TENANT, go_app.UNDECLARED_SURFACE, go_app.RUN, IX)})
        self.assertEqual(relations["static_route_authorized"], ())
        self.assertEqual(relations["scip_index_stale"], ())
        self.assertEqual(set(relations["static_index_current"]), {(IX,)})

    def test_no_duplicate_definition_is_derived_on_go_app(self) -> None:
        # scip-go emits a definition of the package symbol in every file of a
        # package and `local N` symbols are file-scoped, so both repeat across
        # documents without being ambiguous.  The guarded rules (scip_symbol
        # category != "other") derive nothing on go_app (22 bogus rows before the
        # guard), and every document keeps its scip_references_closed witness.
        result = self.result_or_fail()
        for report in (result.python, result.souffle):
            relations = dict(report.relations)
            with self.subTest(kernel=report.backend):
                self.assertEqual(relations["scip_duplicate_definition"], ())
                self.assertEqual(len(relations["scip_references_closed"]), 5)
                self.assertEqual(len(relations["static_reachability_closed"]), 1)
        repeated = {}
        for fact in self.exported.bundle.facts:
            if fact.relation == "scip_definition_site":
                _, path, _, symbol = (t.value for t in fact.terms)
                repeated.setdefault(symbol, set()).add(path)
        repeated = {s for s, paths in repeated.items() if len(paths) > 1}
        self.assertTrue(repeated and all(s.startswith("local ") or s.endswith("/") for s in repeated), sorted(repeated))

    def test_claim_verdicts_in_both_kernels(self) -> None:
        result = self.result_or_fail()
        for report in (result.python, result.souffle):
            with self.subTest(kernel=report.backend):
                verdicts = {claim.key: (claim.semantic, claim.operational, claim.basis) for claim in report.claims}
                self.assertEqual(verdicts, EXPECTED_VERDICTS)

    def test_every_proof_leaf_is_an_exported_or_claim_time_evidence_id(self) -> None:
        report = evaluate(self.bundle)
        self.assertEqual(report.status.value, "complete", report.message)
        known = {record.id for record in self.bundle.evidence}
        exported = {record.id for record in self.exported.bundle.evidence}
        by_id = {entry.claim.id: entry.result for entry in report.claims}
        for claim_id in ("claim-cap-jobs-read", "claim-cap-audit-create",
                         "claim-reaches-repo-get", "claim-reaches-repo-write"):
            with self.subTest(claim=claim_id):
                leaves = set(by_id[claim_id].support)
                self.assertTrue(leaves)
                self.assertTrue(leaves <= known)
                self.assertTrue(all(leaf.startswith(("scip:", "static:")) for leaf in leaves), sorted(leaves))
                claim_time = {leaf for leaf in leaves if leaf.startswith("static:claim-time:")}
                self.assertTrue(leaves - claim_time <= exported, sorted(leaves - exported))
        capability = set(by_id["claim-cap-jobs-read"].support)
        self.assertEqual({leaf.split(":")[2] for leaf in capability}, {
            "scip_definition_site", "scip_index", "scip_index_tree", "scip_may_reference", "scip_symbol",
            "route_handler_location", "static_op_site", "static_site_owner", "source_tree_observed"})
        gap = set(by_id["claim-runtime-route-gap"].refutation)
        self.assertEqual({leaf.split(":")[0] for leaf in gap}, {"runtime", "static"})
        self.assertTrue(gap <= known)

    def test_closure_and_digests_are_stable_under_shuffled_input(self) -> None:
        result = self.result_or_fail()
        for seed in (1, 7):
            raw = copy.deepcopy(go_app.golden_raw())
            rng = random.Random(seed)
            rng.shuffle(raw["documents"])
            for document in raw["documents"]:
                rng.shuffle(document["occurrences"])
                rng.shuffle(document["symbols"])
            ast = go_app.ast_raw()
            for key in ("surfaces", "entities", "op_sites"):
                ast[key] = list(reversed(ast[key]))
            with self.subTest(seed=seed):
                # The permuted raw index is a different JSON document (a
                # different file receipt); the identity, the evidence ids and
                # the bundle digest come from the exported relations and agree.
                bundle, exported = go_app.go_app_bundle(raw, ast=ast, file_digest="f" * 64)
                self.assertEqual(exported.status, scip_facts.STATUS_COMPLETE, exported.messages)
                meta = dict(exported.bundle.metadata)
                self.assertEqual((meta["index_digest"], meta["index_file_digest"]), (IX, "f" * 64))
                self.assertEqual({r.id for r in exported.bundle.evidence}, {r.id for r in self.exported.bundle.evidence})
                self.assertEqual(scip_facts.bundle_digest(exported.bundle), EXPORTED_BUNDLE_DIGEST)
                self.assertEqual(scip_facts.bundle_digest(bundle), scip_facts.bundle_digest(self.bundle))
                self.assertEqual(run_python(bundle).canonical_digest, result.python.canonical_digest)


if __name__ == "__main__":
    unittest.main()
